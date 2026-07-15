"""Replay over semantic tapes (issue #24), and the span-tree reader.

Two things are being pinned here.

**Replay ignores testimony as an answer, and compares it as a claim.** A recorded `sem` event
is not something the world said, so it is never fed back: the replayed code re-runs its own
note()/span() calls and testifies afresh. The two accounts are then compared, and a difference
is reported as a THIRD signal — independent of a boundary divergence (the recording is stale)
and of an invariant violation (the code is wrong). It says: the code's story about what it was
doing has changed. That may be a refactor. The tape does not presume to know.

**A tape can be read top-down.** The span tree first; the raw JSONL only inside the span that
looks wrong.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import flight_recorder as fr
from tests import toy_effects, toy_tools


def make_boundary() -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects, ["fetch_remote", "maybe_fail", "read_config",
                                "create_account"])],
        chains=[fr.ChainTarget(toy_tools, "DB")],
        clock_modules=[toy_tools],
        random_modules=[toy_tools],
        error_revivers={"ToyError": lambda args: toy_effects.ToyError(*args)},
    )


class ToyAdapter(fr.ReplayAdapter):
    """The ordinary adapter. `as_fn` re-resolves the recorded name to a DIFFERENT function —
    which is how "somebody changed the code" is expressed here, without editing the module the
    recording was made from."""

    def __init__(self, as_fn: str | None = None):
        self.boundary = make_boundary()
        self.trace_root = os.path.dirname(toy_tools.__file__)
        self._as_fn = as_fn

    def resolve(self, fn_name, feed):
        fn = getattr(toy_tools, self._as_fn or fn_name)
        return getattr(fn, "__flight_wrapped__", fn)


def record(tmp_path, run):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        run()
        return fr.session_path()
    finally:
        fr.uninstall()


@pytest.fixture
def sem_tape(tmp_path):
    return record(tmp_path, lambda: asyncio.run(
        toy_tools.enrol("t@example.com", password="x")))


@pytest.fixture
def plain_tape(tmp_path):
    return record(tmp_path, lambda: toy_tools.greet("t@example.com", count=2))


# --- replay -----------------------------------------------------------------------------

def test_a_sem_tape_replays_green(sem_tape):
    """Unchanged code, unchanged claims: every existing signal green, and the sems consumed
    rather than mistaken for boundary answers the code failed to ask for."""
    report = fr.replay_call(sem_tape, 0, ToyAdapter())
    assert report.ok, fr.format_report(0, report)
    assert report.events_consumed == report.events_total
    assert report.sem_divergence is None
    assert report.sems_recorded == report.sems_replayed
    assert ("enrol", "begin") in report.sems_recorded


def test_a_pre_sem_tape_replays_exactly_as_before(plain_tape):
    """The regression that matters: a tape with no sems on it behaves identically to the day
    before this existed. No new fields interfere, no accounting shifts."""
    report = fr.replay_call(plain_tape, 0, ToyAdapter())
    assert report.ok, fr.format_report(0, report)
    assert report.sems_recorded == [] and report.sems_replayed == []
    assert report.sem_divergence is None


def test_a_deleted_span_is_named_but_does_not_fail_the_replay(sem_tape):
    """Same questions, same answers, same result — a different account of what they were for.

    `ok` stays True by default, and that default is load-bearing: instrumentation is grown
    incrementally, and a pinned suite that went red because somebody added a span would teach
    everyone to stop adding spans.
    """
    report = fr.replay_call(sem_tape, 0, ToyAdapter(as_fn="enrol_refactored"))

    assert report.ok, "a changed span must not fail a replay by default"
    assert report.result_match and report.divergence is None
    assert report.events_consumed == report.events_total

    assert report.sem_divergence is not None
    assert "load_corpus" in report.sem_divergence
    assert "semantic divergence at 1" in report.sem_divergence
    assert "load_corpus" in fr.format_report(0, report)


def test_sem_strict_folds_the_divergence_into_the_verdict(sem_tape):
    """Opt in, once the vocabulary has settled and a change of testimony IS a finding."""
    strict = fr.replay_call(sem_tape, 0, ToyAdapter(as_fn="enrol_refactored"),
                            sem_strict=True)
    assert not strict.ok
    assert strict.sem_divergence is not None

    # ...and it still says nothing about a tape whose claims did not change.
    unchanged = fr.replay_call(sem_tape, 0, ToyAdapter(), sem_strict=True)
    assert unchanged.ok, fr.format_report(0, unchanged)


def test_a_mutated_sem_tape_still_replays_in_probe_mode(sem_tape):
    """Probe mode ignores sems exactly as strict mode does — and does not count them as
    'skipped', which in a probe report means 'the mutation changed the path'. Testimony is no
    evidence of that."""
    rec = fr.Recording.load(sem_tape)
    call = rec.call(0)
    call.read(op="stream").result = []          # a corpus the store can never actually answer
    mutated = rec.save(sem_tape.parent / "mutated.jsonl")

    report = fr.replay_call(mutated, 0, ToyAdapter())
    assert report.probe
    assert report.unanswerable is None and report.divergence is None
    assert report.sem_divergence is None, "the code told the same story about a different world"

    skipped = [w for w in report.warnings if "skipping" in w]
    assert not skipped, f"sems were miscounted as a changed path: {skipped}"


# --- the span tree ------------------------------------------------------------------------

def test_spans_round_trip_the_nesting(sem_tape):
    tree = fr.Recording.load(sem_tape).call(0).spans()

    assert tree["phase"] == "call" and tree["name"] == "enrol"
    enrol = tree["children"][0]
    assert (enrol["name"], enrol["phase"], enrol["outcome"]) == ("enrol", "span", "ok")

    names = [(c["name"], c["phase"]) for c in enrol["children"]]
    assert names == [("load_corpus", "span"), ("corpus_read", "point"),
                     ("register", "span"), ("registration_failed", "point")]

    load = enrol["children"][0]
    assert [e["k"] for e in load["events"]] == ["db"], \
        "a span's events are the ones DIRECTLY under it"

    register = enrol["children"][2]
    assert register["outcome"] == "error"
    assert [e["fn"].rsplit(".", 1)[-1] for e in register["events"]] == \
        ["create_account", "maybe_fail"]

    # The clock read happens while the span's arguments are being evaluated — before it opens.
    # So it belongs to the call, not to the span, and the tree says so.
    assert [e["k"] for e in tree["events"]] == ["now"]


def test_render_spans_is_the_view_you_read_first(sem_tape):
    rendered = fr.Recording.load(sem_tape).call(0).render_spans()
    assert rendered == "\n".join([
        "enrol  ok  (1 now)",
        "  enrol  ok",
        "    load_corpus  ok  (1 db)",
        "    - corpus_read  rows=3",
        "    register  ERROR  (2 fx)",
        '    - registration_failed  why="kaput"',
    ]), "\n" + rendered


def test_render_spans_over_a_whole_session(sem_tape):
    rendered = fr.Recording.load(sem_tape).render_spans()
    assert rendered.startswith("call 0:\n")
    assert "register  ERROR" in rendered


def test_the_tree_of_a_tape_with_no_sems_is_just_the_call(plain_tape):
    tree = fr.Recording.load(plain_tape).call(0).spans()
    assert tree["children"] == []
    assert [e["k"] for e in tree["events"]] == ["db", "rand", "now", "db", "now"]
    assert fr.render_spans(tree) == "greet  ok  (2 db, 2 now, 1 rand)"


def test_the_tree_of_the_frozen_conformance_fixture():
    """Against the checked-in artifact, not a tape recorded a moment ago. The fixture is what
    a second implementation is written to read, so if the reader and the fixture ever disagree
    it must be here that it shows — not in a recording this test made for itself."""
    fixture = (Path(__file__).resolve().parents[1] / "spec" / "fixtures"
               / "python-sem-toy.jsonl")
    tree = fr.Recording.load(fixture).call(0).spans()

    enrol = tree["children"][0]
    assert [(c["name"], c["phase"], c["outcome"]) for c in enrol["children"]] == [
        ("load_corpus", "span", "ok"),
        ("corpus_read", "point", None),
        ("register", "span", "error"),
        ("registration_failed", "point", None),
    ]
    assert "register  ERROR  (2 fx)" in fr.render_spans(tree)


def test_an_unclosed_span_is_shown_open_rather_than_dropped():
    """A call that died mid-flight leaves its spans open in the `.inflight` sidecar, and that
    is the single most informative thing in it: the reader must see where execution stopped,
    not a tidy tree that pretends it did not."""
    rec = {"fn": "enrol", "kwargs": {}, "error": None, "events": [
        {"k": "sem", "name": "enrol", "phase": "begin", "sid": 1},
        {"k": "fx", "fn": "m.f", "args": [], "kwargs": {}, "res": 1},
    ]}
    tree = fr.mutate._span_tree(rec)
    assert tree["children"][0]["outcome"] is None
    assert fr.render_spans(tree) == "enrol  ok\n  enrol  open  (1 fx)"
