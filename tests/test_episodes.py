"""The episode miner: recurring act-sequences over a tape's call envelopes.

Arrived here from an application that had grown it in-tree, where every one of these rules
was first a bug: a drum beat of repeats drowning the deck, a session handshake appearing in
every window and distinguishing nothing, and the same conversation dealt three times because
its three orderings each had different support.
"""

from __future__ import annotations

import json

from flight_recorder import episodes


def _lines(*fns):
    header = json.dumps({"ev": "header", "version": 1})
    return [header, "not json at all"] + [json.dumps({"fn": f, "seq": i})
                                          for i, f in enumerate(fns, 1)]


def test_a_story_is_the_calls_in_order_and_survives_garbage():
    """A tape carries a header and may carry anything a writer passed through untouched."""
    assert episodes.story(_lines("a", "b", "a")) == ["a", "b", "a"]
    assert episodes.story(["", "{broken", json.dumps({"no_fn": 1})]) == []


def test_consecutive_repeats_collapse_to_one_act():
    assert episodes.collapse(["tick"] * 12 + ["balance"]) == ["tick", "balance"]


def test_mining_finds_what_recurs_and_only_what_recurs():
    got = episodes.mine({
        "r1": ["done", "done", "balance", "propose"],
        "r2": ["done", "balance", "propose", "history"],
        "r3": ["inbox"],
    })
    chains = {tuple(e["acts"]): e for e in got}
    assert ("done", "balance", "propose") in chains
    e = chains[("done", "balance", "propose")]
    assert e["support"] == 2 and e["occurrences"] == 2
    assert not any("inbox" in c for c in chains), "seen once is not a pattern"


def test_a_shorter_episode_buried_in_a_longer_one_is_not_dealt_twice():
    got = episodes.mine({"a": ["x", "y", "z"], "b": ["x", "y", "z"]})
    assert [tuple(e["acts"]) for e in got] == [("x", "y", "z")]


def test_a_loop_counts_each_pass_but_one_recording_of_support():
    got = episodes.mine({
        "r1": ["propose", "object", "propose", "object"],
        "r2": ["propose", "object"],
    })
    e = next(x for x in got if x["acts"] == ["propose", "object"])
    assert e["support"] == 2 and e["occurrences"] == 3


def test_one_line_per_ritual_however_many_ways_it_is_walked():
    """`a→b`, `b→a` and `a→b→a` are one conversation told three ways. Dealing all three is
    what makes a deck nobody reads; the strongest telling survives and its order still says
    which way the sequence is usually walked."""
    got = episodes.mine({
        "r1": ["a", "b", "a", "b"],
        "r2": ["b", "a", "b"],
        "r3": ["a", "b"],
    })
    keys = {frozenset(e["acts"]) for e in got}
    assert len(got) == len(keys), f"the same ritual was dealt more than once: {got}"


def test_a_stopword_vanishes_and_the_acts_around_it_meet():
    """A handshake that precedes nearly every call appears in every window and distinguishes
    nothing. Which acts are plumbing is a fact about an application, not a statistic, so the
    caller names them — a frequency threshold would eat the busiest real act in a heavy
    week."""
    got = episodes.mine({
        "r1": ["hello", "open", "hello", "act"],
        "r2": ["hello", "open", "hello", "act"],
    }, noise=frozenset({"hello"}))
    assert [tuple(e["acts"]) for e in got] == [("open", "act")]


def test_the_merge_puts_the_performance_above_the_rehearsal():
    scripted = episodes.mine({"s1": ["a", "b"], "s2": ["a", "b"],
                              "s3": ["e", "f"], "s4": ["e", "f"]})
    live = episodes.mine({"l1": ["c", "d"], "l2": ["c", "d"],
                          "l3": ["a", "b"], "l4": ["a", "b"]})
    merged = episodes.merge(scripted, live)
    assert [e["source"] for e in merged] == ["live", "both", "scripted"]
    both = next(e for e in merged if e["acts"] == ["a", "b"])
    assert both["live_support"] == 2 and both["support"] == 2, \
        "both counts survive — one side anticipated it, the other performed it"


def test_an_episode_id_is_stable_across_sources():
    assert episodes.episode_id(["a", "b"]) == "ep-a-b"
    a = episodes.mine({"x": ["a", "b"], "y": ["a", "b"]})[0]["id"]
    b = episodes.mine({"p": ["a", "b"], "q": ["a", "b"]})[0]["id"]
    assert a == b, "a judgment against an episode must name it wherever it was mined"


def test_successors_keep_what_mining_discards():
    """Mining keeps only what recurs, so every rare branch is thrown away by design — and the
    rare branch is the whole object of the question "why does hardly anyone go that way"."""
    got = episodes.successors({
        "r1": ["open", "act", "open", "read"],
        "r2": ["open", "act"],
        "r3": ["open", "rare"],
    })
    assert got["open"] == {"act": 2, "read": 1, "rare": 1}, "one-offs survive here"
    assert not any("rare" in e["acts"] for e in episodes.mine({
        "r1": ["open", "act"], "r2": ["open", "act"], "r3": ["open", "rare"]})), \
        "...and are correctly absent from the mined patterns"


def test_an_act_nothing_follows_is_where_journeys_end():
    """Sometimes completion, sometimes abandonment — unreadable from a tape, and worth asking
    about precisely because people arrive and go no further."""
    got = episodes.successors({"r1": ["open", "act"], "r2": ["open", "act"]})
    assert "act" not in got


def test_successors_share_the_miner_s_world():
    """Same collapse, same noise. A caller reading both is reading one world, not two."""
    stories = {"r1": ["hello", "open", "open", "hello", "act"]}
    assert episodes.successors(stories, noise=frozenset({"hello"})) == {"open": {"act": 1}}
