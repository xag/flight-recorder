"""The pytest plugin (issue #5): a directory of pinned recordings becomes a non-regression
suite — one test per recorded call, passing on bit-for-bit reproduction and failing with the
divergence report the CLI prints. Exercised by running pytest inside pytest."""

import json
from pathlib import Path

import pytest

import flight_recorder as fr
from tests import toy_tools
from tests.test_roundtrip import make_boundary

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

INI = """
[pytest]
flight_recordings = recordings
flight_adapter = tests.test_roundtrip:ToyAdapter
"""


@pytest.fixture
def pinned(tmp_path):
    """A real recording of one greet() call, pinned as a fixture would be."""
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        toy_tools.greet("t@example.com", count=2)
        return fr.session_path().read_text(encoding="utf-8")
    finally:
        fr.uninstall()


def _lay_out(pytester, recording: str, ini: str = INI) -> Path:
    pytester.makefile(".ini", pytest=ini.strip())
    rec_dir = pytester.path / "recordings"
    rec_dir.mkdir()
    path = rec_dir / "pinned.jsonl"
    path.write_text(recording, encoding="utf-8")
    pytester.syspathinsert(REPO_ROOT)
    return path


def test_a_pinned_recording_becomes_a_passing_test(pytester, pinned):
    _lay_out(pytester, pinned)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*pinned.jsonl::call0::greet*PASSED*"])


def test_each_recorded_call_is_its_own_test(pytester, tmp_path):
    import asyncio
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        toy_tools.greet("t@example.com", count=2)
        asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))
        recording = fr.session_path().read_text(encoding="utf-8")
    finally:
        fr.uninstall()

    _lay_out(pytester, recording)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*call0::greet*", "*call1::remote_sum*"])


def test_a_tampered_recording_fails_with_the_divergence_report(pytester, pinned):
    lines = pinned.splitlines()
    call = json.loads(lines[1])
    # Rewrite a recorded boundary answer that feeds only the output, so the failure lands
    # as a result mismatch rather than a path divergence. Every row, not just the first:
    # greet() random.sample()s two of the three, and the recorded draw decides which.
    for ev in call["events"]:
        if ev.get("k") == "db" and ev.get("op") == "stream":
            for row in ev["res"]:
                row["data"]["name"] = "TAMPERED"
    tampered = lines[0] + "\n" + json.dumps(call, ensure_ascii=False) + "\n"

    _lay_out(pytester, tampered)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*pinned.jsonl no longer reproduces*", "*DIVERGED*"])


def test_a_recording_of_changed_code_names_the_boundary_divergence(pytester, pinned):
    # Drop the recording's first boundary event: the code now asks a question the recording
    # cannot answer at that position. That is a path divergence, and it must be named.
    lines = pinned.splitlines()
    call = json.loads(lines[1])
    call["events"] = call["events"][1:]
    broken = lines[0] + "\n" + json.dumps(call, ensure_ascii=False) + "\n"

    _lay_out(pytester, broken)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*divergence*"])


def test_flight_trace_writes_a_state_trace_per_call(pytester, pinned):
    ini = INI + "\nflight_trace = traces\n"
    _lay_out(pytester, pinned, ini)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)

    traces = list((pytester.path / "traces").glob("*.trace.jsonl"))
    assert len(traces) == 1
    events = [json.loads(l) for l in traces[0].read_text(encoding="utf-8").splitlines()]
    assert any(e["e"] == "L" and e.get("d") for e in events)


def test_the_plugin_is_inert_without_flight_recordings(pytester, pinned):
    # A project that merely depends on flight-recorder must not have its .jsonl files
    # collected as tests.
    pytester.makefile(".ini", pytest="[pytest]\n")
    rec_dir = pytester.path / "recordings"
    rec_dir.mkdir()
    (rec_dir / "pinned.jsonl").write_text(pinned, encoding="utf-8")
    pytester.makepyfile(test_ordinary="def test_ordinary(): assert True")

    result = pytester.runpytest()
    result.assert_outcomes(passed=1)  # the ordinary test, and nothing from the recording


def test_recordings_without_an_adapter_fails_at_startup_not_per_test(pytester, pinned):
    _lay_out(pytester, pinned, "[pytest]\nflight_recordings = recordings\n")
    result = pytester.runpytest()
    # A usage error aborts before collection, so there is no terminal summary at all —
    # the recording never became a (failing) test.
    result.stderr.fnmatch_lines(["*flight_adapter*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_a_malformed_adapter_spec_is_a_usage_error(pytester, pinned):
    _lay_out(pytester, pinned,
             "[pytest]\nflight_recordings = recordings\nflight_adapter = app.replay\n")
    result = pytester.runpytest()
    result.stderr.fnmatch_lines(["*module:Attribute*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_an_unimportable_adapter_fails_once_at_startup(pytester, pinned):
    # Well-formed spec, but the module doesn't import. This must abort before collection —
    # not raise the same error once per recorded call.
    _lay_out(pytester, pinned, "[pytest]\nflight_recordings = recordings\n"
                               "flight_adapter = no.such.module:Adapter\n")
    result = pytester.runpytest()
    result.stderr.fnmatch_lines(["*not importable*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_a_corrupt_recording_is_named_rather_than_crashing_collection(pytester, pinned):
    _lay_out(pytester, pinned)
    (pytester.path / "recordings" / "truncated.jsonl").write_text(
        '{"ev": "session"} \n{"ev": "call", "fn": "gre',  # died mid-write
        encoding="utf-8")

    result = pytester.runpytest()
    # The good recording still runs — a corrupt one must not abort collection for the run.
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*truncated.jsonl is under flight_recordings*"])
