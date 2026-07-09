"""Invariants as a correctness oracle (issue #2).

The point of this file is the difference between two questions a recording can be asked:
- "does the code still do what it did?" — replay answers it, and a bug replays perfectly;
- "is what it does right?" — only an invariant answers it, and it can condemn the very
  first recording of a bug.

`toy_tools.study_status(level=0)` is the second case: its output is internally consistent,
it replays bit-for-bit forever, and it is wrong.
"""

import json
from pathlib import Path

import pytest

import flight_recorder as fr
from tests import toy_tools
from tests.test_roundtrip import ToyAdapter, make_boundary


@pytest.fixture
def record(tmp_path):
    """Record one call and hand back its session path. The tool is resolved by name *after*
    install(), because install patches the module attribute — grabbing the function first
    would call the unwrapped original and record nothing."""
    def _record(tool: str, *args, **kwargs):
        fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
        try:
            getattr(toy_tools, tool)(*args, **kwargs)
            return fr.session_path()
        finally:
            fr.uninstall()
    return _record


# --- the claims -------------------------------------------------------------------------

@fr.invariant("never claims the corpus is finished while items remain unstudied")
def _done_only_when_empty(t: fr.Trajectory):
    assert not (t.result["done"] and t.result["corpus"] - t.result["deck"] > 0), (
        f"done=True with {t.result['corpus'] - t.result['deck']} of "
        f"{t.result['corpus']} items left")


@fr.invariant("level never excludes the whole corpus")
def _level_selects_something(t: fr.Trajectory):
    for obs in t.trace.values("level"):
        assert obs.value > 0, f"level={obs.value} at {obs.at}"


@fr.invariant("the deck never exceeds the corpus")
def _deck_within_corpus(t: fr.Trajectory):
    assert t.result["deck"] <= t.result["corpus"]


HEALTHY = [_done_only_when_empty, _deck_within_corpus]


# --- the trace is data, not reprs --------------------------------------------------------

def test_traced_numbers_are_numbers(record, tmp_path):
    session = record("study_status", "t@example.com", level=2)
    trace = tmp_path / "t.jsonl"
    fr.replay_call(session, 0, ToyAdapter(), trace)

    level = fr.Trace.load(trace).first("level")
    assert level is not None and level.value == 2 and isinstance(level.value, int)


def test_traced_documents_are_readable_not_addresses(record, tmp_path):
    session = record("study_status", "t@example.com", level=3)
    trace = tmp_path / "t.jsonl"
    fr.replay_call(session, 0, ToyAdapter(), trace)

    corpus = fr.Trace.load(trace).final("corpus")
    assert corpus is not None
    assert [c["name"] for c in corpus.value] == ["alpha", "beta", "gamma"]

    # And the raw Snap objects unwrap to the surface a consumer reads, not to <object at 0x…>
    rows = fr.Trace.load(trace).final("rows")
    assert rows is not None and rows.value[0]["data"]["name"] == "alpha"


def test_a_trace_of_the_same_execution_is_stable(record, tmp_path):
    """Reprs carried memory addresses, so no two traces were ever equal."""
    session = record("study_status", "t@example.com", level=2)
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    fr.replay_call(session, 0, ToyAdapter(), a)
    fr.replay_call(session, 0, ToyAdapter(), b)
    assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")


def test_long_sequences_are_truncated_but_still_know_their_length():
    encoded = fr.trace_jsonable(list(range(5000)))
    revived = fr.from_trace_jsonable(encoded)
    assert len(revived) == 5000            # len() tells the truth
    assert revived[0] == 0 and revived[99] == 99
    assert list.__len__(revived) == 100    # only a prefix is actually carried


def test_a_user_dict_shaped_like_a_marker_revives_as_itself():
    """{'__seq__': True} in user code must not be mistaken for the tracer's own marker."""
    for tricky in ({"__seq__": True}, {"__opaque__": "note"},
                   {"__dt__": "hello"}, {"__str__": [1, 2]}, {"__esc__": 0}):
        assert fr.from_trace_jsonable(fr.trace_jsonable(tricky)) == tricky


def test_sets_are_traced_in_a_deterministic_order():
    """Hash order varies per process (PYTHONHASHSEED); a byte-identical trace cannot."""
    encoded = fr.trace_jsonable({"gamma", "alpha", "beta"})
    assert encoded == ["alpha", "beta", "gamma"]


def test_a_hostile_object_degrades_to_opaque_instead_of_raising():
    """trace_jsonable runs inside the sys.settrace callback; an exception there is
    injected into the frame being traced, corrupting the replay itself."""
    class Evil:
        def __getattr__(self, name):
            raise RuntimeError("not initialized")

    encoded = fr.trace_jsonable(Evil())
    assert "Evil" in encoded["__opaque__"]

    class EvilDict(dict):
        def items(self):
            raise RuntimeError("nope")

    assert "__opaque__" in fr.trace_jsonable(EvilDict(a=1))


def test_an_old_trace_is_refused_loudly(tmp_path):
    """A v1 trace holds reprs; arithmetic over reprs would fail confusingly, not loudly."""
    old = tmp_path / "old.trace.jsonl"
    old.write_text('{"e": "C", "fn": "f", "at": "f.py:1", "args": {"x": "\'2\'"}}\n',
                   encoding="utf-8")
    with pytest.raises(ValueError, match="older tracer"):
        fr.Trace.load(old)


def test_opaque_values_carry_no_memory_address():
    """On 3.11, comprehension frames hold `.0` iterator locals; their reprs carry
    `at 0x…`, which would make every trace of the same execution unique. Scrubbed."""
    encoded = fr.trace_jsonable(iter([1, 2, 3]))
    assert "0x" not in encoded["__opaque__"]
    assert "list_iterator" in encoded["__opaque__"]  # still says what it was


def test_long_strings_are_truncated_but_still_know_their_length():
    revived = fr.from_trace_jsonable(fr.trace_jsonable("x" * 5000))
    assert len(revived) == 5000
    assert revived.startswith("xxx")


# --- the oracle --------------------------------------------------------------------------

def test_a_healthy_call_holds_every_invariant(record):
    session = record("study_status", "t@example.com", level=3)
    report = fr.check_invariants(session, 0, ToyAdapter(), HEALTHY)
    assert report.ok, fr.format_invariant_report(report)
    assert report.outcome == "held" and report.checked == 2


def test_the_bug_replays_perfectly_and_is_still_caught(record):
    """The whole argument for #2, in one test."""
    session = record("study_status", "t@example.com", level=0)

    # A pinned recording is happy: the code does exactly what it did.
    assert fr.replay_call(session, 0, ToyAdapter(), None).ok

    # The invariant is not.
    report = fr.check_invariants(session, 0, ToyAdapter(), HEALTHY)
    assert report.outcome == "violated"
    assert [v.invariant for v in report.violations] == [
        "never claims the corpus is finished while items remain unstudied"]
    assert "3 of 3 items left" in report.violations[0].detail


def test_an_invariant_reads_an_internal_variable_the_output_never_shows(record):
    session = record("study_status", "t@example.com", level=0)
    report = fr.check_invariants(session, 0, ToyAdapter(), [_level_selects_something])

    assert report.outcome == "violated"
    assert "level=0" in report.violations[0].detail  # `level` appears nowhere in the result


def test_a_broken_invariant_is_reported_as_broken_not_as_a_bug(record):
    @fr.invariant("this claim is itself buggy")
    def _bad(t):
        return t.result["no_such_key"]

    session = record("study_status", "t@example.com", level=3)
    report = fr.check_invariants(session, 0, ToyAdapter(), [_bad])

    assert report.outcome == "violated"
    assert report.violations[0].broke and "KeyError" in report.violations[0].detail


def test_a_diverged_replay_checks_nothing_and_says_so(record, tmp_path):
    session = record("study_status", "t@example.com", level=0)
    lines = session.read_text(encoding="utf-8").splitlines()
    call = json.loads(lines[1])
    call["events"] = []  # the recording can no longer answer the code's first question
    broken = tmp_path / "broken.jsonl"
    broken.write_text(lines[0] + "\n" + json.dumps(call) + "\n", encoding="utf-8")

    report = fr.check_invariants(broken, 0, ToyAdapter(), HEALTHY)
    assert report.outcome == "diverged" and not report.ok
    assert report.violations == [] and report.checked == 0
    assert "no invariant was checked" in fr.format_invariant_report(report)


def test_invariants_assert_on_the_replayed_result_not_the_recorded_one(record, tmp_path):
    """If they read the recording, an invariant could never contradict it."""
    session = record("study_status", "t@example.com", level=0)
    lines = session.read_text(encoding="utf-8").splitlines()
    call = json.loads(lines[1])
    call["result"] = {"corpus": 3, "deck": 3, "done": False}  # a lie in the recording
    doctored = tmp_path / "doctored.jsonl"
    doctored.write_text(lines[0] + "\n" + json.dumps(call) + "\n", encoding="utf-8")

    report = fr.check_invariants(doctored, 0, ToyAdapter(), HEALTHY)
    assert report.outcome == "violated"  # the code still did the wrong thing
    assert not report.reproduced          # and the doctored recording no longer matches it


def test_held_invariants_do_not_make_a_nonreproducing_replay_ok(record, tmp_path):
    """`ok` demands both verdicts. A doctored result the invariants happen to accept must
    still fail the hand-check the README documents."""
    session = record("study_status", "t@example.com", level=3)  # healthy call
    lines = session.read_text(encoding="utf-8").splitlines()
    call = json.loads(lines[1])
    call["result"] = {"corpus": 99, "deck": 1, "done": False}  # a lie, but invariant-clean
    doctored = tmp_path / "doctored2.jsonl"
    doctored.write_text(lines[0] + "\n" + json.dumps(call) + "\n", encoding="utf-8")

    report = fr.check_invariants(doctored, 0, ToyAdapter(), HEALTHY)
    assert report.outcome == "held"      # the claims hold over the real trajectory
    assert not report.reproduced         # but the recording no longer matches
    assert not report.ok                 # so the overall verdict is not ok
    assert "did NOT reproduce" in fr.format_invariant_report(report)


def test_an_error_recording_result_is_none_and_the_hint_names_t_raised(tmp_path):
    """A pinned recording of a call that raised hands result=None to invariants. An
    unguarded one must be reported as broken WITH the guidance — and a guarded one holds,
    so pinning error paths stays viable."""
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    try:
        with pytest.raises(TypeError):
            toy_tools.study_status("t@example.com", level="nope")  # int <= str, in-code
        session = fr.session_path()
    finally:
        fr.uninstall()

    @fr.invariant("unguarded: reads t.result on an error recording")
    def _unguarded(t):
        assert t.result["deck"] >= 0

    report = fr.check_invariants(session, 0, ToyAdapter(), [_unguarded])
    assert report.reproduced  # the error itself replays faithfully
    assert report.outcome == "violated"
    assert report.violations[0].broke
    assert "t.raised" in report.violations[0].detail  # blames the claim, with the fix

    @fr.invariant("guarded: skips the error path")
    def _guarded(t):
        if t.raised:
            return
        assert t.result["deck"] >= 0

    assert fr.check_invariants(session, 0, ToyAdapter(), [_guarded]).ok


def test_collect_refuses_a_bare_function_in_an_explicit_list():
    """Silently dropping it would report `held` for a claim that was never checked."""
    def naked(t):
        assert t.result

    with pytest.raises(TypeError, match="@invariant"):
        fr.collect([naked])


# --- declaring and collecting -------------------------------------------------------------

def test_collect_finds_every_invariant_in_a_module():
    import tests.test_invariants as this_module
    found = {i.description for i in fr.collect(this_module)}
    assert "level never excludes the whole corpus" in found
    assert "the deck never exceeds the corpus" in found


def test_collect_accepts_a_list_or_a_single_invariant():
    assert len(fr.collect(HEALTHY)) == 2
    assert len(fr.collect(_deck_within_corpus)) == 1


def test_the_description_is_what_a_failure_is_named_by(record):
    session = record("study_status", "t@example.com", level=0)
    text = fr.format_invariant_report(
        fr.check_invariants(session, 0, ToyAdapter(), HEALTHY))
    assert "1 of 2 invariant(s) VIOLATED" in text
    assert "never claims the corpus is finished while items remain unstudied" in text


# --- the trace query surface ---------------------------------------------------------------

def test_values_returns_the_timeline_in_execution_order(record, tmp_path):
    session = record("greet", "t@example.com", count=2)
    trace = tmp_path / "t.jsonl"
    fr.replay_call(session, 0, ToyAdapter(), trace)

    t = fr.Trace.load(trace)
    # greet() random.sample()s two of the three rows; which two is the recorded draw's business.
    names = t.final("names")
    assert names is not None
    assert len(names.value) == 2
    assert set(names.value) <= {"alpha", "beta", "gamma"}

    assert t.first("email").value == "t@example.com"
    assert t.final("nope") is None and t.values("nope") == []
    assert "email" in t.names() and "count" in t.names()


def test_calls_and_returns_are_queryable(record, tmp_path):
    session = record("study_status", "t@example.com", level=2)
    trace = tmp_path / "t.jsonl"
    fr.replay_call(session, 0, ToyAdapter(), trace)

    t = fr.Trace.load(trace)
    entry = t.calls("study_status")
    assert entry and entry[0].args["level"] == 2
    assert t.returns("study_status")[0].value["corpus"] == 3
    assert t.raised() == []
