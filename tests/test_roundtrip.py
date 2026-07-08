"""Record→replay round-trip fidelity on the toy app: sync chain+clock+random tools, async
effect tools, recorded effect exceptions revived by type, tamper detection."""

import asyncio
import json
import os

import pytest

import flight_recorder as fr
from tests import toy_effects, toy_tools


def make_boundary() -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects, ["fetch_remote", "maybe_fail", "read_config"])],
        chains=[fr.ChainTarget(toy_tools, "DB")],
        clock_modules=[toy_tools],
        random_modules=[toy_tools],
        error_revivers={"ToyError": lambda args: toy_effects.ToyError(*args)},
    )


class ToyAdapter(fr.ReplayAdapter):
    def __init__(self):
        self.boundary = make_boundary()
        self.trace_root = os.path.dirname(toy_tools.__file__)

    def resolve(self, fn_name, feed):
        fn = getattr(toy_tools, fn_name)
        return getattr(fn, "__flight_wrapped__", fn)


@pytest.fixture
def recorded(tmp_path):
    boundary = make_boundary()
    fr.install(boundary, toy_tools, directory=str(tmp_path), enabled=True)
    yield tmp_path
    fr.uninstall()


def test_sync_tool_round_trip_with_trace(recorded, tmp_path):
    out = toy_tools.greet("t@example.com", count=2)
    assert "t@example.com" in out
    session = fr.session_path()
    fr.uninstall()

    trace = tmp_path / "greet.trace.jsonl"
    report = fr.replay_call(session, 0, ToyAdapter(), trace)
    assert report.ok, (report.divergence, report.result_diff, report.write_divergences)
    lines = [json.loads(l) for l in trace.read_text(encoding="utf-8").splitlines()]
    assert any(e["e"] == "L" and e.get("d") for e in lines)


def test_async_tool_round_trip_including_revived_error(recorded, tmp_path):
    # v-sum 7 > 5 → maybe_fail raises ToyError, caught in the tool: the recording carries
    # the exception, replay revives it as a real ToyError so the except clause still fires.
    out = asyncio.run(toy_tools.remote_sum("t@example.com", "abc", "wxyz"))
    assert out["note"] == "failed: kaput n=7"
    session = fr.session_path()
    fr.uninstall()

    report = fr.replay_call(session, 0, ToyAdapter(), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_tampered_recording_is_caught(recorded, tmp_path):
    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))
    session = fr.session_path()
    fr.uninstall()

    lines = session.read_text(encoding="utf-8").splitlines()
    call = json.loads(lines[1])
    # tamper a recorded answer that feeds only the OUTPUT (read_config), so detection
    # happens at result comparison; tampering an answer that feeds a later boundary call
    # is caught even earlier, as a path divergence (see the swapped-code test).
    cfg_events = [e for e in call["events"]
                  if e["k"] == "fx" and e["fn"].endswith("read_config")]
    assert cfg_events
    cfg_events[0]["res"] = "cfg:TAMPERED"
    bad = tmp_path / "tampered.jsonl"
    bad.write_text(lines[0] + "\n" + json.dumps(call, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    report = fr.replay_call(bad, 0, ToyAdapter(), None)
    assert not report.result_match and report.result_diff


def test_changed_code_path_is_a_named_divergence(recorded, tmp_path):
    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))
    session = fr.session_path()
    fr.uninstall()

    class SwappedAdapter(ToyAdapter):
        def resolve(self, fn_name, feed):
            async def swapped(email, a, b):  # asks the boundary a different first question
                return await toy_effects.fetch_remote(b + a)
            return swapped

    report = fr.replay_call(session, 0, SwappedAdapter(), None)
    assert report.divergence and "fetch_remote" in report.divergence


def test_install_disabled_is_noop(tmp_path):
    orig = toy_tools.greet
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=False)
    assert toy_tools.greet is orig
    assert fr.session_path() is None
