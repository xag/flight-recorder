"""Tape v1 conformance — the freeze.

Two directions, and both matter:

  1. The real recorder's output must satisfy spec/validate.py. This is what stops the
     *spec* from drifting away from the implementation.
  2. The checked-in fixtures must satisfy it too, and they are regenerated from the real
     recorder (FR_REGEN_FIXTURES=1). This is what stops the *implementation* from drifting
     away from the spec — and it is the artifact the Node port is written against, since a
     second implementation cannot be tested against Python's internals, only against its
     tape.

The Node port carries a mirror of the checker and must validate these same fixtures. If
the two checkers ever disagree about a fixture, the tape has forked, which is the one
failure this whole arrangement exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import flight_recorder as fr
from tests import toy_effects, toy_tools

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from spec.validate import validate_tape, VERSION  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "spec" / "fixtures"


def make_boundary() -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects, ["fetch_remote", "maybe_fail", "read_config"])],
        chains=[fr.ChainTarget(toy_tools, "DB")],
        clock_modules=[toy_tools],
        random_modules=[toy_tools],
        error_revivers={"ToyError": lambda args: toy_effects.ToyError(*args)},
    )


def make_sem_boundary() -> fr.Boundary:
    """The same boundary, plus `create_account` and a redaction rule.

    `create_account` is declared because the span that claims to have registered somebody must
    ENCLOSE the evidence that it did. A fixture whose `register` span sits above an empty
    stretch of tape would illustrate the opposite of the point: testimony is checkable only
    when the thing it testifies about is recorded underneath it.

    The redaction rule is there because a semantic event's `data` is scrubbed like any other
    payload — testimony is written by the app, about the app's own values, and is exactly as
    likely to carry a credential as an effect's arguments are.
    """
    b = make_boundary()
    b.effects = [(toy_effects, ["fetch_remote", "maybe_fail", "read_config",
                                "create_account"])]
    b.redact = {"password"}
    return b


def _record_a_sem_tape(tmp_path) -> str:
    """Drive the real recorder over every shape a `sem` event can take."""
    fr.install(make_sem_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        asyncio.run(toy_tools.enrol("t@example.com", password="hunter2"))
    finally:
        fr.uninstall()

    tapes = sorted(Path(tmp_path).glob("flight-*.jsonl"))
    assert tapes, "the recorder wrote no tape"
    return tapes[-1].read_text(encoding="utf-8")


def _sems(text: str) -> list:
    return [e for line in text.splitlines() if line.strip()
            for e in (json.loads(line).get("events") or []) if e.get("k") == "sem"]


def _record_a_tape(tmp_path) -> str:
    """Drive the real recorder over every event kind the format defines."""
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        # greet: chained client read + write (db), random.sample (rand), datetime.now (now)
        toy_tools.greet("t@example.com", count=2)
        # remote_sum: effects (fx) — and maybe_fail raises ToyError, giving us the fx.err
        # branch, which is a different shape from fx.res and must be in the frozen tape.
        asyncio.run(toy_tools.remote_sum("t@example.com", "abc", "wxyz"))
    finally:
        fr.uninstall()

    tapes = sorted(Path(tmp_path).glob("flight-*.jsonl"))
    assert tapes, "the recorder wrote no tape"
    return tapes[-1].read_text(encoding="utf-8")


def test_format_version_is_frozen():
    """A bump here is a breaking change to every implementation. It must be deliberate."""
    assert fr.FORMAT_VERSION == VERSION == 1


def test_the_real_recorder_emits_a_conformant_tape(tmp_path):
    text = _record_a_tape(tmp_path)
    violations = validate_tape(text)
    assert not violations, "the recorder's own tape violates the frozen spec:\n  " + "\n  ".join(violations)


def test_recorded_tape_exercises_every_event_kind(tmp_path):
    """A spec frozen against a tape that never exercises `rand` is not frozen at all."""
    text = _record_a_tape(tmp_path)
    kinds = {
        e.get("k")
        for line in text.splitlines()
        if line.strip()
        for e in (json.loads(line).get("events") or [])
    }
    assert {"fx", "db", "now", "rand"} <= kinds, f"tape only exercised {kinds}"

    # and the fx error branch, which is a separate shape from fx.res
    errs = [
        e
        for line in text.splitlines()
        if line.strip()
        for e in (json.loads(line).get("events") or [])
        if e.get("k") == "fx" and "err" in e
    ]
    assert errs, "no fx event carrying 'err': the raising branch was never recorded"


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.jsonl")) if FIXTURES.exists() else [])
def test_fixture_is_conformant(fixture):
    violations = validate_tape(fixture.read_text(encoding="utf-8"))
    assert not violations, f"{fixture.name}:\n  " + "\n  ".join(violations)


def test_the_real_recorder_emits_a_conformant_sem_tape(tmp_path):
    text = _record_a_sem_tape(tmp_path)
    violations = validate_tape(text)
    assert not violations, "the recorder's own sem tape violates the spec:\n  " + "\n  ".join(violations)


def test_recorded_sem_tape_exercises_every_shape_a_sem_can_take(tmp_path):
    """A spec frozen against a tape whose spans all succeeded is not frozen at all — and the
    error branch is a different shape, on the event that a reader will most want to find."""
    sems = _sems(_record_a_sem_tape(tmp_path))

    assert {s["phase"] for s in sems} == {"begin", "end", "point"}

    nested = [s for s in sems if s["phase"] == "begin"]
    assert len(nested) >= 3, "the fixture must carry a span inside a span"

    outcomes = {s.get("outcome") for s in sems if s["phase"] == "end"}
    assert outcomes == {"ok", "error"}, f"both outcomes must be on the tape, got {outcomes}"

    # A value marker, and a value redaction had to reach: both live in sem `data`.
    data = [s["data"] for s in sems if "data" in s]
    assert any(isinstance(d.get("started"), dict) and "__dt__" in d["started"] for d in data), \
        "no value marker in any sem data — the encoder was never exercised"
    assert any(d.get("password") == "[REDACTED]" for d in data), \
        "a password rode a sem event to the tape in the clear"


def test_the_fixture_puts_evidence_underneath_the_testimony(tmp_path):
    """The property the whole event kind exists for. A span that claims an act must enclose the
    raw events that act consisted of — otherwise the claim is unfalsifiable, and an
    unfalsifiable claim on a tape is worse than no claim, because it reads like one."""
    text = _record_a_sem_tape(tmp_path)
    evs = [e for line in text.splitlines() if line.strip()
           for e in (json.loads(line).get("events") or [])]

    opened = next(i for i, e in enumerate(evs)
                  if e.get("k") == "sem" and e.get("name") == "register"
                  and e["phase"] == "begin")
    closed = next(i for i, e in enumerate(evs)
                  if e.get("k") == "sem" and e.get("name") == "register"
                  and e["phase"] == "end")
    inside = [e for e in evs[opened + 1:closed] if e.get("k") == "fx"]
    assert [e["fn"].rsplit(".", 1)[-1] for e in inside] == ["create_account", "maybe_fail"], \
        "the register span does not enclose the effects it claims to be made of"


def test_regenerate_fixtures(tmp_path):
    """FR_REGEN_FIXTURES=1 refreshes the golden tapes from the real recorder."""
    if not os.environ.get("FR_REGEN_FIXTURES"):
        pytest.skip("set FR_REGEN_FIXTURES=1 to regenerate")
    FIXTURES.mkdir(parents=True, exist_ok=True)

    # Two sessions, two directories. A session file is named for the second it was opened in
    # and the pid that opened it, so two installs from one process inside the same second land
    # on the same path and append — one file, two headers, and a mystifying failure.
    text = _record_a_tape(tmp_path / "plain")
    assert not validate_tape(text)
    (FIXTURES / "python-toy.jsonl").write_text(text, encoding="utf-8")

    sem_text = _record_a_sem_tape(tmp_path / "sem")
    assert not validate_tape(sem_text)
    (FIXTURES / "python-sem-toy.jsonl").write_text(sem_text, encoding="utf-8")


# --- the checker itself must be sharp, or "conformant" means nothing ------------------

def _tape(*lines: dict) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


SESSION = {"ev": "session", "version": 1, "started": "2026-07-11T10:00:00+02:00",
           "python": "3.13.0", "constants": {}}
CALL = {"ev": "call", "seq": 1, "fn": "t", "kwargs": {}, "events": [],
        "result": None, "error": None, "ts": "2026-07-11T10:00:00+02:00", "ms": 1.0}


def test_checker_accepts_a_minimal_valid_tape():
    assert validate_tape(_tape(SESSION, CALL)) == []


@pytest.mark.parametrize("mutation, expect", [
    ({"version": 2}, "version"),
    ({"started": "2026-07-11T10:00:00"}, "timezone-aware"),   # naive
    ({"python": "3.13", "node": "24"}, "exactly one runtime"),
])
def test_checker_rejects_bad_session(mutation, expect):
    bad = {**SESSION, **mutation}
    violations = validate_tape(_tape(bad, CALL))
    assert any(expect in v for v in violations), violations


def test_checker_rejects_a_missing_header():
    assert any("session header" in v for v in validate_tape(_tape(CALL)))


def test_checker_rejects_non_monotonic_seq():
    c2 = {**CALL, "seq": 5}
    assert any("monotonic" in v for v in validate_tape(_tape(SESSION, CALL, c2)))


def test_checker_rejects_fx_with_both_res_and_err():
    ev = {"k": "fx", "fn": "f", "args": [], "kwargs": {}, "res": 1,
          "err": {"type": "E", "repr": "E()", "args": []}}
    call = {**CALL, "events": [ev]}
    assert any("exactly one" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_db_read_and_write_at_once():
    ev = {"k": "db", "op": "get", "sig": "c('x')", "res": [], "args": [1]}
    call = {**CALL, "events": [ev]}
    assert any("never both" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_rand_idx_outside_the_population():
    ev = {"k": "rand", "m": "sample", "n": 3, "kk": 1, "idx": [7]}
    call = {**CALL, "events": [ev]}
    assert any("out of range" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_rand_idx_disagreeing_with_kk():
    ev = {"k": "rand", "m": "sample", "n": 5, "kk": 3, "idx": [0, 1]}
    call = {**CALL, "events": [ev]}
    assert any("kk=" in v for v in validate_tape(_tape(SESSION, call)))


def _sem_call(*sems: dict) -> dict:
    return {**CALL, "events": list(sems)}


def test_checker_accepts_well_nested_spans():
    call = _sem_call(
        {"k": "sem", "name": "outer", "phase": "begin", "sid": 1},
        {"k": "sem", "name": "inner", "phase": "begin", "sid": 2},
        {"k": "sem", "name": "mark", "phase": "point", "sid": 3, "data": {"n": 1}},
        {"k": "sem", "name": "inner", "phase": "end", "sid": 2, "outcome": "ok"},
        {"k": "sem", "name": "outer", "phase": "end", "sid": 1, "outcome": "error"},
    )
    assert validate_tape(_tape(SESSION, call)) == []


def test_checker_rejects_straddling_spans():
    """The one structural promise sem makes. Two spans that cross would put an event inside
    both and inside neither, and every reader walking the stream would build a different tree
    — which is the whole reason enclosure may be derived from order at all."""
    call = _sem_call(
        {"k": "sem", "name": "a", "phase": "begin", "sid": 1},
        {"k": "sem", "name": "b", "phase": "begin", "sid": 2},
        {"k": "sem", "name": "a", "phase": "end", "sid": 1},
        {"k": "sem", "name": "b", "phase": "end", "sid": 2},
    )
    assert any("well-nested" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_an_unclosed_span():
    call = _sem_call({"k": "sem", "name": "a", "phase": "begin", "sid": 1})
    assert any("never closed" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_an_end_with_no_begin():
    call = _sem_call({"k": "sem", "name": "a", "phase": "end", "sid": 1})
    assert any("no open span" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_a_reused_sid():
    """An `end` names its `begin` by sid. Reuse one and the pairing is guesswork."""
    call = _sem_call(
        {"k": "sem", "name": "a", "phase": "begin", "sid": 1},
        {"k": "sem", "name": "b", "phase": "point", "sid": 1},
        {"k": "sem", "name": "a", "phase": "end", "sid": 1},
    )
    assert any("reused" in v for v in validate_tape(_tape(SESSION, call)))


def test_checker_rejects_a_bad_phase_and_a_misplaced_outcome():
    bad_phase = _sem_call({"k": "sem", "name": "a", "phase": "middle", "sid": 1})
    assert any("phase" in v for v in validate_tape(_tape(SESSION, bad_phase)))

    misplaced = _sem_call({"k": "sem", "name": "a", "phase": "point", "sid": 1,
                           "outcome": "ok"})
    assert any("outcome" in v for v in validate_tape(_tape(SESSION, misplaced)))


def test_a_reader_from_before_sem_existed_still_accepts_a_sem_tape(monkeypatch):
    """The forward-compatibility claim, made concrete rather than asserted in prose.

    A v1 reader built before this event kind existed knows nothing of `sem` — so it must
    ignore it and go on calling the tape conformant. If this ever fails, adding an event kind
    became a breaking change and the format's whole versioning story ("readers ignore what they
    do not know") was a fiction.
    """
    import spec.validate as v
    monkeypatch.setattr(v, "EVENT_KINDS", {"fx", "db", "now", "perf", "rand"})

    call = _sem_call(
        {"k": "sem", "name": "outer", "phase": "begin", "sid": 1},
        {"k": "fx", "fn": "f", "args": [], "kwargs": {}, "res": 1},
        {"k": "sem", "name": "outer", "phase": "end", "sid": 1, "outcome": "ok"},
    )
    assert v.validate_tape(_tape(SESSION, call)) == []


def test_checker_tolerates_unknown_ev_and_unknown_keys():
    """Forward compatibility IS the versioning story: a reader ignores what it does not
    know. If this ever fails, adding an event kind becomes a breaking change."""
    weird = {"ev": "inflight", "fn": "t", "whatever": 1}
    call = {**CALL, "events": [{"k": "future-kind", "payload": 1}], "unknown_key": True}
    assert validate_tape(_tape(SESSION, weird, call)) == []


def test_checker_tolerates_a_torn_final_line():
    """The only corruption an append-only tape can suffer: the process died mid-write."""
    text = _tape(SESSION, CALL) + '{"ev":"call","seq":2,"fn":"t'
    assert validate_tape(text) == []


def test_checker_rejects_a_torn_line_that_is_not_last():
    text = _tape(SESSION) + '{"ev":"call","seq":1,"fn":"t\n' + json.dumps(CALL) + "\n"
    assert any("not JSON" in v for v in validate_tape(text))
