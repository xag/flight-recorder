"""Crash-safe recording (issue #1): events stream to an .inflight sidecar during the call,
the sidecar disappears on normal completion, and an orphaned sidecar (the process died
mid-call) is listed by the CLI as INCOMPLETE with the events captured before death."""

import json

import pytest

import flight_recorder as fr
from flight_recorder import record as record_mod
from flight_recorder.replay import _print_call_list
from tests import toy_effects, toy_tools
from tests.test_roundtrip import make_boundary


@pytest.fixture
def recording(tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    yield tmp_path
    fr.uninstall()


def test_sidecar_lives_during_the_call_and_dies_with_it(recording, tmp_path, monkeypatch):
    seen_during_call = {}

    real = toy_effects.read_config.__flight_wrapped__

    def spying(name):
        seen_during_call["inflight"] = list(tmp_path.glob("*.inflight"))
        return real(name)

    monkeypatch.setattr(toy_effects, "read_config",
                        record_mod._wrap_effect(make_boundary(),
                                                "tests.toy_effects.read_config", spying))
    import asyncio
    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))

    # mid-call: exactly one sidecar existed, already holding the earlier fx events
    assert len(seen_during_call["inflight"]) == 1
    # after the call: sidecar gone, session record present
    assert not list(tmp_path.glob("*.inflight"))
    _, calls = fr.load_session(fr.session_path())
    assert calls and calls[0]["fn"] == "remote_sum"


def test_sidecar_content_mirrors_events(recording, tmp_path):
    sink = record_mod._recorder.start_call("doomed_tool", {"x": 1})
    sink.append({"k": "fx", "fn": "a.b", "res": 1})
    sink.append({"k": "now", "v": "2026-01-01T00:00:00"})
    # no finalize: this is the crash
    sidecars = list(tmp_path.glob("*.inflight"))
    assert len(sidecars) == 1
    lines = [json.loads(l) for l in sidecars[0].read_text(encoding="utf-8").splitlines()]
    assert lines[0]["ev"] == "inflight" and lines[0]["fn"] == "doomed_tool"
    assert lines[1:] == [{"k": "fx", "fn": "a.b", "res": 1},
                         {"k": "now", "v": "2026-01-01T00:00:00"}]


def test_orphaned_sidecar_is_listed_incomplete(recording, tmp_path, capsys):
    toy_tools.greet("t@example.com", count=2)  # one completed call
    sink = record_mod._recorder.start_call("doomed_tool", {"x": 1})
    sink.append({"k": "now", "v": "2026-01-01T00:00:00"})
    session = fr.session_path()

    _print_call_list(session)
    out = capsys.readouterr().out
    assert "--call 0: greet" in out
    assert "INCOMPLETE (crashed mid-call)" in out
    assert "doomed_tool, 1 event(s) recorded before death" in out