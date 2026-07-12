"""Trajectory invariants, over a tape that is the model's trajectory.

`tests/fixtures/session-surface.jsonl` is a session distilled from a real recording: the same
shapes, none of the identifying detail. It carries, on purpose, every failure mode a tool surface
has and no unit test can see —

  * a BOUNCE (call 2): browse_repo on a file, answered only by "call read_file instead". The
    server is faultless and the round trip carried nothing but the model's confusion.
  * a FAILURE RETURNED AS PROSE (call 5): `error` is null, the tape says the call succeeded, and
    the result says "Couldn't create the issue". Nothing mechanical can see that without being
    told what the app's failure prose looks like — which is why `failed=` is a predicate.
  * A RETRY OF IT, UNCHANGED (call 6).
  * A LEGITIMATE REPEAT (calls 7 and 9): the same list_issues, either side of a write, returning
    different answers. It must NOT be reported, and getting this wrong is how an instrument like
    this loses its reader.
"""

from pathlib import Path

import pytest

from flight_recorder.session import (
    check_sessions, format_session_verdict, load_sessions, no_retry_after_failure,
    no_tool_bounce, no_wasted_repeats, session_invariant,
)

TAPE = Path(__file__).parent / "fixtures" / "session-surface.jsonl"

# What this app's failures and redirections look like in prose. The library cannot know, and must
# not guess.
failed = lambda st: st.raised or st.text.startswith("Couldn't")            # noqa: E731
misrouted = lambda st: "call read_file" in st.text or "call browse_repo" in st.text  # noqa: E731


@pytest.fixture(scope="module")
def session():
    sessions = load_sessions(TAPE)
    assert len(sessions) == 1
    return sessions[0]


def test_the_tape_is_the_trajectory(session):
    """The model's choices, in the order it made them. This has been on every tape since the first
    commit; nothing new was recorded to get it."""
    assert session.fns == [
        "list_projects", "browse_repo", "read_file", "find_issues", "create_github_issue",
        "create_github_issue", "list_issues", "create_github_issue", "list_issues", "list_projects",
    ]


# --- the portable claim --------------------------------------------------------------------


def test_a_repeat_is_judged_by_the_answer_not_the_call(session):
    """list_projects at 1 and 10 asked the same thing and got the same thing: waste. list_issues
    at 7 and 9 asked the same thing and got DIFFERENT things, because a write landed between them:
    not waste, and not reported.

    The first version of this claim asked "did anything write in between", reading the tape's `db`
    events — and a real recording refuted it inside a minute, because an app whose writes go
    through an `fx` effect writes without a single `db` event and the tape cannot tell an effect
    that read from one that wrote. Comparing the answers cannot be wrong about that."""
    pairs = [(a.seq, b.seq) for a, b in session.repeats()]
    assert pairs == [(5, 6), (1, 10)]  # in the order the repeat was MADE, which is how it is read
    assert not any(b == 9 for _, b in pairs), "list_issues learned something; it is not a repeat"


def test_wrote_to_store_is_about_chains_and_says_so(session):
    """It is a true fact about `db` events and it is NOT 'this call changed the world' — call 5
    reached out through an effect and changed nothing, call 8 wrote through a chain. A claim that
    needs to know whether the world moved must not lean on this."""
    assert session[7].wrote_to_store is True    # seq 8: create_github_issue, a db add
    assert session[6].wrote_to_store is False   # seq 7: list_issues, a db read


# --- the claims that need the app's own prose -----------------------------------------------


def test_a_failure_returned_as_prose_is_invisible_until_you_say_so(session):
    """The single most important thing this fixture proves. The tape says call 5 SUCCEEDED."""
    failing = session[4]
    assert failing.error is None and failing.raised is False
    assert failed(failing) is True  # only because the app told us what its failure looks like


def test_finds_the_retry_the_bounce_and_the_waste(session):
    v = check_sessions([session], [
        no_wasted_repeats(),
        no_retry_after_failure(failed),
        no_tool_bounce(misrouted),
    ])
    assert not v.ok
    found = {f.invariant: f.detail for f in v.findings}
    assert "call 6 create_github_issue re-issues call 5 unchanged" in \
        found["the model never retries a call that already failed the same way"]
    assert "call 2 browse_repo" in found["no call is answered only by a redirection to another tool"]
    assert "call 10 list_projects repeats call 1" in \
        found["the model never re-asks a question it already had the answer to"]

    # Every finding impeaches the SURFACE. There is no bug in any of these functions, and a reader
    # sent to look for one would waste their afternoon.
    assert {f.about for f in v.findings} == {"surface"}


def test_the_verdict_is_a_rate_because_the_model_is_not_deterministic(session):
    """Everywhere else in this library a claim holds or is violated. Here it holds at a RATE, and
    a fix to a tool description is judged by whether the rate moves."""
    v = check_sessions([session, session], [no_wasted_repeats()])
    assert v.sessions == 2
    assert v.rate("the model never re-asks a question it already had the answer to") == 1.0

    @session_invariant("the model reads before it writes")
    def _(s):
        assert s.fns[0] in {"list_projects", "get_status"}

    v = check_sessions([session], [_])
    assert v.ok and v.rate("the model reads before it writes") == 0.0
    assert "[ok ]" in format_session_verdict(v)
