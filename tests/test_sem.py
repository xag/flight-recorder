"""Semantic events (issue #23): the app's testimony about its own execution, recorded
in-stream next to the evidence.

The library gains no semantics from any of this, and these tests are careful to assert only
what a recorder may assert: that the claim was written down, in order, next to the raw events
it encloses, and scrubbed like every other payload. Whether the claim is TRUE is a question for
a reader, and nothing here answers it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import flight_recorder as fr
from tests import toy_effects, toy_tools


def make_boundary(redact=(), forbid=()) -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects, ["fetch_remote", "maybe_fail", "read_config",
                                "create_account"])],
        chains=[fr.ChainTarget(toy_tools, "DB")],
        clock_modules=[toy_tools],
        random_modules=[toy_tools],
        error_revivers={"ToyError": lambda args: toy_effects.ToyError(*args)},
        redact=redact,
        forbid=forbid,
    )


def record(tmp_path, run, redact=(), forbid=(), enabled=True):
    fr.install(make_boundary(redact, forbid), toy_tools, directory=str(tmp_path),
               enabled=enabled)
    try:
        run()
        return fr.session_path()
    finally:
        fr.uninstall()


def events(session) -> list:
    if session is None:
        return []
    calls = [json.loads(l) for l in session.read_text(encoding="utf-8").splitlines()
             if json.loads(l).get("ev") == "call"]
    return [e for c in calls for e in c["events"]]


def sems(session) -> list:
    return [e for e in events(session) if e.get("k") == "sem"]


# --- off is free, and silent ---------------------------------------------------------

def test_note_and_span_are_no_ops_when_nothing_is_installed():
    """Instrumentation lives in production code paths. Uninstalled, it must cost nothing and
    have no failure modes at all — a recorder that can break the app it observes is worse than
    no recorder."""
    fr.note("nothing_is_recording", n=1)
    with fr.span("still_nothing", k="v") as s:
        assert s is not None
    assert fr.session_path() is None


def test_they_are_no_ops_when_the_gate_declines(tmp_path):
    """A gate that never fires must leave no file behind — a span must not be the thing that
    conjures a session into existence."""
    session = record(tmp_path, lambda: toy_tools.greet("t@example.com", count=1),
                     enabled=lambda name, kwargs: False)
    assert session is None
    assert not list(tmp_path.glob("flight-*.jsonl"))


def test_a_span_outside_any_call_records_nothing(tmp_path):
    """Spans are call-scoped. Emitted with the recorder installed but no tool call in flight,
    there is no call to belong to, and a sem event that belongs to no call has nowhere on the
    tape to go."""
    def run():
        with fr.span("orphan"):
            fr.note("orphan_note")
        toy_tools.greet("t@example.com", count=1)   # so a session file exists at all

    session = record(tmp_path, run)
    assert [s["name"] for s in sems(session)] == []


# --- what gets written ---------------------------------------------------------------

def test_a_span_encloses_the_raw_events_it_produced(tmp_path):
    """Order IS the meaning: the raw events a span encloses are the ones between its begin and
    its end. That is the property every reader derives enclosure from — and the property that
    lets a claim be confronted with the evidence beneath it — so it is the one worth pinning."""
    session = record(tmp_path, lambda: asyncio.run(
        toy_tools.enrol("t@example.com", password="x")))

    stream = [(e.get("k"), e.get("name") or e.get("fn") or e.get("op"))
              for e in events(session)]
    kinds = [k for k, _ in stream]

    begin = kinds.index("sem")
    assert stream[begin] == ("sem", "enrol")
    assert stream[-1] == ("sem", "enrol"), "the outermost span closes last"

    # The db read the tool performed sits INSIDE the load_corpus span, in the stream.
    names = [n for k, n in stream]
    lo, hi = names.index("load_corpus"), len(names) - 1 - names[::-1].index("load_corpus")
    assert any(k == "db" for k, _ in stream[lo:hi]), \
        "the corpus read is not enclosed by the span that claims to have loaded it"


def test_begin_and_end_pair_by_sid_and_nest(tmp_path):
    session = record(tmp_path, lambda: asyncio.run(
        toy_tools.enrol("t@example.com", password="x")))
    s = sems(session)

    stack = []
    for e in s:
        if e["phase"] == "begin":
            stack.append(e["sid"])
        elif e["phase"] == "end":
            assert stack and stack.pop() == e["sid"], "spans do not nest"
    assert not stack, "a span was left open"

    sids = [e["sid"] for e in s if e["phase"] in ("begin", "point")]
    assert len(sids) == len(set(sids)), "sids are not unique within the call"


def test_a_raising_body_still_closes_its_span_and_re_raises(tmp_path):
    """A span that vanished from the tape when the code inside it failed would hide exactly the
    execution somebody came to the tape to read."""
    def run():
        with pytest.raises(toy_effects.ToyError):
            asyncio.run(toy_tools.summing("t@example.com"))

    session = record(tmp_path, run)
    ends = [e for e in sems(session) if e["phase"] == "end"]
    assert [e["outcome"] for e in ends] == ["error"]


def test_the_error_outcome_is_recorded_by_the_instrumented_tool(tmp_path):
    session = record(tmp_path, lambda: asyncio.run(
        toy_tools.enrol("t@example.com", password="x")))
    by_name = {(e["name"], e["phase"]): e for e in sems(session)}
    assert by_name[("register", "end")]["outcome"] == "error"
    assert by_name[("enrol", "end")]["outcome"] == "ok"
    assert ("registration_failed", "point") in by_name


def test_note_carries_its_data(tmp_path):
    session = record(tmp_path, lambda: asyncio.run(
        toy_tools.enrol("t@example.com", password="x")))
    point = next(e for e in sems(session) if e["name"] == "corpus_read")
    assert point["phase"] == "point"
    assert point["data"] == {"rows": 3}


def test_the_context_manager_works_in_async_code(tmp_path):
    session = record(tmp_path, lambda: asyncio.run(toy_tools.awaited("t@example.com")))
    names = [(e["name"], e["phase"]) for e in sems(session)]
    assert ("awaited", "begin") in names and ("awaited", "end") in names


# --- testimony is scrubbed exactly like evidence --------------------------------------

def test_sem_data_goes_through_redact(tmp_path):
    session = record(tmp_path, lambda: asyncio.run(
        toy_tools.enrol("t@example.com", password="hunter2")), redact={"password"})
    text = session.read_text(encoding="utf-8")
    assert "hunter2" not in text
    data = [e["data"] for e in sems(session) if "data" in e]
    assert any(d.get("password") == "[REDACTED]" for d in data)


def test_a_forbidden_value_in_span_data_trips_the_tripwire(tmp_path):
    """`redact` protects exactly the fields you thought of, and its failure mode is silent and
    open. `forbid` states the property a field-name rule cannot: THIS TAPE CARRIES NO
    CREDENTIAL. A span's `data` is a payload like any other, and an app that names a secret in
    its own testimony has leaked it precisely as hard as one that passed it to an effect.

    The secret is born inside the tool, so the call's kwargs are clean: the sem event is the
    tape's only chance to catch it, which is what makes this a test of `sem` and not of the
    call header that already guarded the kwargs.
    """
    def run():
        with pytest.raises(fr.ForbiddenValue):
            asyncio.run(toy_tools.testify("t@example.com"))

    session = record(tmp_path, run, forbid=[r"\bT{64}\b"])
    if session is not None:
        assert toy_tools.LITERAL_TOKEN not in session.read_text(encoding="utf-8")


def test_the_tripwire_names_the_event_kind_and_never_the_secret(tmp_path):
    """A tripwire that quotes the credential it caught has become the leak it was there to
    prevent — the message ends up in logs and stack traces."""
    def run():
        with pytest.raises(fr.ForbiddenValue) as e:
            asyncio.run(toy_tools.testify("t@example.com"))
        assert toy_tools.LITERAL_TOKEN not in str(e.value)
        assert "'sem'" in str(e.value), "the message should name the event kind that carried it"

    record(tmp_path, run, forbid=[r"\bT{64}\b"])
