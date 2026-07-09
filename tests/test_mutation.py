"""Mutation replay (issue #8): hostile boundary states authored as data, judged by
invariants under probe mode.

The point of this file is what a recording cannot do alone: ToyDB always answers three
rows, so no recording of study_status can ever hold an empty corpus — its ZeroDivision on
`len(deck) / len(corpus)` is unreachable by record-and-replay. One edited event reaches it.
"""

import asyncio
import inspect
import json

import pytest

import flight_recorder as fr
from tests import toy_tools
from tests.test_roundtrip import ToyAdapter, make_boundary


@pytest.fixture
def record(tmp_path):
    def _record(tool: str, *args, **kwargs):
        fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
        try:
            fn = getattr(toy_tools, tool)
            if inspect.iscoroutinefunction(getattr(fn, "__flight_wrapped__", fn)):
                asyncio.run(fn(*args, **kwargs))
            else:
                fn(*args, **kwargs)
            return fr.session_path()
        finally:
            fr.uninstall()
    return _record


@fr.invariant("never crashes on any boundary state")
def _never_crashes(t: fr.Trajectory):
    assert not t.raised, f"raised: {t.error}"


@fr.invariant("the deck never exceeds the corpus")
def _deck_within_corpus(t: fr.Trajectory):
    if t.raised:
        return
    assert t.result["deck"] <= t.result["corpus"]


# --- the blocker, resolved ----------------------------------------------------------------

def test_the_mutation_strict_replay_rejects_is_answerable_under_probe(record):
    """Verified in the #7 design probe: mutating a fetch result changes maybe_fail's
    argument, and strict replay refuses before the code finishes. Probe mode answers."""
    session = record("remote_sum", "t@example.com", "ab", "cd")

    rec = fr.Recording.load(session)
    rec.call(0).effect("fetch_remote").result = {"key": "ab", "v": 1000}

    # Strict: the downstream question changed → divergence, exactly as before.
    strict = tmp = rec.save(session.parent / "mutated-strict-check.jsonl")
    report = fr.replay_call(tmp, 0, ToyAdapter(), None, probe=False)
    # (the saved call carries probe:true, so force-strict requires stripping the flag)
    lines = strict.read_text(encoding="utf-8").splitlines()
    call = json.loads(lines[1])
    call.pop("probe")
    stripped = session.parent / "mutated-stripped.jsonl"
    stripped.write_text(lines[0] + "\n" + json.dumps(call) + "\n", encoding="utf-8")
    strict_report = fr.replay_call(stripped, 0, ToyAdapter(), None)
    assert strict_report.divergence and "maybe_fail" in strict_report.divergence

    # Probe: the code runs to completion in the mutated world; invariants judge.
    probe = rec.call(0).check(ToyAdapter(), [_never_crashes])
    assert probe.probe and probe.ok, fr.format_invariant_report(probe)
    # The mutated answer flowed through the real CODE: sum = 1000 + 20. But maybe_fail is
    # a BOUNDARY effect — the tape still answers "fine" no matter what the code now asks
    # it. Mutation edits answers; it never re-executes effects. To make maybe_fail fail,
    # inject its error too (see test_error_injection_revives_through_the_boundary).
    assert probe.replay.replayed_result["sum"] == 1020
    assert probe.replay.replayed_result["note"] == "fine"


# --- the star: a bug no recording can reach --------------------------------------------

def test_an_edited_event_reaches_a_bug_no_recording_can(record):
    session = record("study_status", "t@example.com", level=2)

    # Every real recording holds three rows; this one now holds none.
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = []

    report = rec.call(0).check(ToyAdapter(), [_never_crashes])
    assert report.outcome == "violated"
    assert "ZeroDivisionError" in report.violations[0].detail

    # The same claim over the unmutated recording holds — the bug is mutation-only.
    clean = fr.check_invariants(session, 0, ToyAdapter(), [_never_crashes])
    assert clean.ok


def test_probe_ok_means_held_alone_since_reproduction_is_meaningless(record):
    session = record("study_status", "t@example.com", level=2)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = [{"name": "solo", "x": 1}]

    report = rec.call(0).check(ToyAdapter(), [_never_crashes, _deck_within_corpus])
    assert report.ok                       # invariants held on the mutated trajectory
    assert not report.reproduced           # the pre-mutation result obviously differs
    assert report.replay.replayed_result["corpus"] == 1


# --- authoring surface ---------------------------------------------------------------------

def test_read_results_auto_wrap_plain_dicts_as_documents(record):
    session = record("study_status", "t@example.com", level=3)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = [{"name": "x", "x": 1}, {"name": "y", "x": 9}]

    report = rec.call(0).check(ToyAdapter(), [_deck_within_corpus])
    assert report.ok
    assert report.replay.replayed_result == {
        "corpus": 2, "deck": 1, "done": False, "coverage": 0.5}


def test_clock_reverse_makes_time_run_backwards(record):
    session = record("greet", "t@example.com", count=2)
    rec = fr.Recording.load(session)
    times_before = rec.call(0).clock.times
    assert len(times_before) == 2
    rec.call(0).clock.reverse()

    @fr.invariant("greet stamps its result with the clock's answer")
    def _stamped(t):
        assert not t.raised

    report = rec.call(0).check(ToyAdapter(), [_stamped])
    assert report.ok
    # the result's timestamp is now the recording's FIRST time, not its second
    assert times_before[0] in report.replay.replayed_result


def test_error_injection_revives_through_the_boundary(record):
    session = record("remote_sum", "t@example.com", "ab", "cd")
    rec = fr.Recording.load(session)
    # maybe_fail recorded "fine" (n=4); inject the failure the code is supposed to catch
    rec.call(0).effect("maybe_fail").error = ("ToyError", ["kaput", 42])

    @fr.invariant("a failed remote note names the failure")
    def _noted(t):
        assert not t.raised
        assert t.result["note"] == "failed: kaput n=42"

    report = rec.call(0).check(ToyAdapter(), [_noted])
    assert report.ok, fr.format_invariant_report(report)


def test_kwargs_mutation_probes_the_input_side(record):
    session = record("study_status", "t@example.com", level=2)
    rec = fr.Recording.load(session)
    rec.call(0).set_kwargs(level=0)  # the motivating production bug, via the input

    @fr.invariant("never claims the corpus is finished while items remain")
    def _done_honest(t):
        if t.raised:
            return
        assert not (t.result["done"] and t.result["corpus"] - t.result["deck"] > 0)

    report = rec.call(0).check(ToyAdapter(), [_done_honest])
    assert report.outcome == "violated"


def test_selectors_name_whats_available_when_they_miss(record):
    session = record("study_status", "t@example.com", level=2)
    call = fr.Recording.load(session).call(0)
    with pytest.raises(KeyError, match="no effect 'nope'"):
        call.effect("nope")
    with pytest.raises(KeyError, match="only 1"):
        call.read(op="stream", occurrence=5)


# --- a crash can never pass silently ---------------------------------------------------------

def test_a_crash_every_guarded_claim_waves_through_is_still_not_held(record):
    """The false-pass the review caught: guarded claims (`if t.raised: return`) would let
    a crash under mutation report `held`. It is its own outcome instead."""
    session = record("study_status", "t@example.com", level=2)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = []  # ZeroDivision territory

    report = rec.call(0).check(ToyAdapter(), [_deck_within_corpus])  # guarded claim only
    assert report.outcome == "raised"
    assert not report.ok
    assert "judges_raise" in fr.format_invariant_report(report)


def test_an_expected_raise_is_blessed_by_a_judging_claim(record):
    session = record("study_status", "t@example.com", level=2)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = []

    @fr.invariant("an empty corpus is rejected loudly", judges_raise=True)
    def _rejects(t):
        assert t.raised and "ZeroDivisionError" in t.error

    report = rec.call(0).check(ToyAdapter(), [_rejects, _deck_within_corpus])
    assert report.outcome == "held" and report.ok


def test_probe_unanswerable_cannot_be_swallowed_by_app_code():
    """It is raised inside the replayed app's own frames, where hostile-path code is
    likeliest to carry a defensive `except Exception` — so it must not be one."""
    assert issubclass(fr.ProbeUnanswerable, BaseException)
    assert not issubclass(fr.ProbeUnanswerable, Exception)


# --- writes are trajectory --------------------------------------------------------------------

def test_writes_under_mutation_are_visible_to_invariants(record):
    session = record("greet", "t@example.com", count=2)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = [{"name": "only", "x": 1}]
    rec.call(0).rand().idx = [0]

    @fr.invariant("greeting one user writes exactly one greeted_at")
    def _writes_once(t):
        assert [w["op"] for w in t.writes] == ["set"]

    report = rec.call(0).check(ToyAdapter(), [_writes_once])
    assert report.ok, fr.format_invariant_report(report)


# --- authoring mistakes fail at the mutation site ---------------------------------------------

def test_malformed_mutations_are_rejected_where_they_are_written(record):
    session = record("greet", "t@example.com", count=2)
    call = fr.Recording.load(session).call(0)

    with pytest.raises(ValueError, match="ISO datetime"):
        call.clock.times = ["hello", "world"]
    with pytest.raises(ValueError, match="non-negative"):
        call.rand().idx = [-1]
    with pytest.raises(ValueError, match="document dict"):
        call.read(op="stream").result = ["a", "b"]


def test_a_partial_snapshot_dict_is_normalized_not_double_wrapped(record):
    session = record("study_status", "t@example.com", level=3)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = [{"id": "r1", "data": {"name": "z", "x": 1}}]

    report = rec.call(0).check(ToyAdapter(), [_deck_within_corpus])
    assert report.ok
    assert report.replay.replayed_result["corpus"] == 1  # data read as the document


# --- answers must not cross wires --------------------------------------------------------------

def test_probe_db_matching_respects_the_chain_shape():
    """Two reads share an op but not a chain shape; the tape must not answer one with
    the other's rows."""
    users_ev = {"k": "db", "op": "stream", "sig": 'collection(users).where("x", ">", 0)',
                "res": []}
    feed = fr.Feed([users_ev], probe=True)
    got = feed.pop_expect("db", sig='collection(users).where("y", "<", 9)', op="stream")
    assert got is users_ev  # same shape, different content: answerable

    feed2 = fr.Feed([users_ev], probe=True)
    with pytest.raises(fr.ProbeUnanswerable):
        feed2.pop_expect("db", sig="collection(config).get()", op="stream")


# --- limits, honestly ------------------------------------------------------------------------

def test_a_mutation_that_redirects_past_the_tape_is_unanswerable(record):
    session = record("greet", "t@example.com", count=2)
    rec = fr.Recording.load(session)
    # Empty the docs: random.sample's recorded draw no longer fits the population.
    rec.call(0).read(op="stream").result = []

    report = rec.call(0).check(ToyAdapter(), [_never_crashes])
    assert report.outcome == "unanswerable"
    assert not report.ok
    assert report.violations == []  # impeaches neither code nor claim
    assert "rand" in fr.format_invariant_report(report) or \
           "idx" in fr.format_invariant_report(report)


def test_fixing_the_draw_makes_the_same_mutation_answerable(record):
    session = record("greet", "t@example.com", count=2)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = [{"name": "only", "x": 1}]
    rec.call(0).rand().idx = [0]  # one row, one pick

    report = rec.call(0).check(ToyAdapter(), [_never_crashes])
    assert report.ok, fr.format_invariant_report(report)
    assert "only" in report.replay.replayed_result


# --- pinning --------------------------------------------------------------------------------

def test_a_saved_mutation_carries_the_probe_flag_and_replays_as_probe(record, tmp_path):
    session = record("study_status", "t@example.com", level=2)
    rec = fr.Recording.load(session)
    rec.call(0).read(op="stream").result = [{"name": "x", "x": 1}]
    pinned = rec.save(tmp_path / "pinned-mutation.jsonl")

    reloaded = fr.Recording.load(pinned)
    assert reloaded.calls[0]["probe"] is True

    # replay_call flips to probe by itself — a pinned mutation can't be mistaken
    # for a strict fixture even if invoked without probe=True.
    report = fr.replay_call(pinned, 0, ToyAdapter(), None)
    assert report.probe and report.divergence is None and report.unanswerable is None
