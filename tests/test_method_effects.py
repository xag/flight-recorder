"""Method effects (issue #11's in-scope half): a client round-trip the tool awaits — an
MCP session/context method like elicit() — declared as an effect on the class, with
`{"method": True}` so `self` is identity rather than input. Recorded like any other
effect; replayed from the tape without touching the client."""

import asyncio
import json
import os

import pytest

import flight_recorder as fr
from tests import toy_effects, toy_tools


def make_boundary() -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects.ToySession, ["elicit"], {"method": True})],
    )


class ToyAdapter(fr.ReplayAdapter):
    def __init__(self):
        self.boundary = make_boundary()
        self.trace_root = os.path.dirname(toy_tools.__file__)

    def resolve(self, fn_name, feed):
        fn = getattr(toy_tools, fn_name)
        return getattr(fn, "__flight_wrapped__", fn)


def test_awaited_round_trip_records_and_replays(tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        out = asyncio.run(toy_tools.confirm_wipe("t@example.com"))
        assert out == {"email": "t@example.com", "confirmed": True, "n": 26}
        session = fr.session_path()
    finally:
        fr.uninstall()

    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    ev = next(e for e in call["events"] if e["k"] == "fx")
    assert ev["fn"] == "ToySession.elicit"
    assert ev["args"] == ["really wipe t@example.com?"]  # self skipped: identity, not input
    assert ev["res"] == {"action": "accept", "value": 26}

    # Replay serves the round-trip from the tape: a client that would now answer
    # differently — or is not there at all — is never consulted.
    original = toy_effects.ToySession.elicit

    async def refuse(self, prompt):
        raise AssertionError("the client must not be consulted on replay")

    toy_effects.ToySession.elicit = refuse
    try:
        report = fr.replay_call(session, 0, ToyAdapter(), None)
    finally:
        toy_effects.ToySession.elicit = original
    assert report.ok, (report.divergence, report.result_diff)
    assert report.replayed_result["confirmed"] is True
