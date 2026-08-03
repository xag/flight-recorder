"""Recurring act-sequences on a tape: what a workflow looks like, mined.

A tape is a sequence of call envelopes, each naming the act that produced it (`fn`), each
carrying the boundary events that happened inside it. Counting those events per act answers
"what did this call touch" and destroys the thing a reader usually cares about: the ORDER.
A session that reads `open, act, open, read` is a conversation; the same session as a
histogram is `{open: 2, act: 1, read: 1}`, and the conversation is gone.

This mines the order. Contiguous runs of acts that recur across recordings, with support
(how many recordings) and occurrences (how many times, counting each pass of a loop).

Deliberately not a model checker and deliberately not semantic. It reads the `fn` of each
call envelope and nothing else — no spans, no model, no vocabulary — because a workflow is
visible before anybody has drawn a model of it, and often the mined workflow is what tells
you a model is missing an act. Sequence work on plain dicts: no dependency beyond the
standard library, so a caller can hand it stories from anywhere.

Three shaping decisions, each of which was a bug before it was a rule:

  COLLAPSE consecutive repeats. Twelve ticks in a row are one act of ticking. Left alone,
  they flood the n-grams with eleven identical adjacent pairs and every real pattern drowns
  in the drum beat.

  DROP THE STOPWORDS, and let the caller name them. A session handshake that precedes nearly
  every call appears in every window and distinguishes nothing; left in, it can be most of
  the deck, as permutations of itself around whatever actually happened. Which acts are
  plumbing is a fact about an application, not a statistic — a frequency threshold would
  silently eat the busiest real act in a heavy week — so `noise` is passed in, never
  inferred.

  ONE LINE PER RITUAL. `a → b`, `b → a` and `a → b → a` are one conversation told three
  ways, and dealing all three makes a deck nobody reads. Per unordered set of acts, the
  strongest telling survives; its order still shows which way the sequence is usually walked.
"""

from __future__ import annotations

import json
from typing import Iterable, Mapping, Sequence

# Longer than this is a whole session rather than a pattern; shorter than two is an act.
MAX_LEN = 4
MIN_SUPPORT = 2


def story(lines: Iterable[str]) -> list[str]:
    """The ordered acts of one recording: each ndjson call envelope's `fn`, in order.

    Best-effort: a line that does not parse, or parses to something with no `fn`, is skipped
    rather than fatal. A tape carries a header line and may carry anything a writer passed
    through untouched, and a story has to survive both.
    """
    acts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("fn"):
            acts.append(str(rec["fn"]))
    return acts


def collapse(acts: Sequence[str]) -> list[str]:
    """Consecutive repeats become one step — see the module docstring's first rule."""
    out: list[str] = []
    for a in acts:
        if not out or out[-1] != a:
            out.append(a)
    return out


def episode_id(acts: Sequence[str]) -> str:
    """Stable across sources, so a judgment recorded against an episode mined from one set
    of recordings also names the same episode mined from another."""
    return "ep-" + "-".join(acts)


def mine(stories: Mapping[str, Sequence[str]],
         min_support: int = MIN_SUPPORT, max_len: int = MAX_LEN,
         noise: frozenset[str] = frozenset()) -> list[dict]:
    """The recurring contiguous act-sequences across the given stories.

    `stories` maps a recording's name to its acts in order (see `story`). `noise` names acts
    to drop from every story before mining — the caller's stopword list.

    Kept when support >= min_support, and kept MAXIMAL: a shorter episode that never occurs
    outside a kept longer one adds nothing and is dropped, so a sub-sequence earns its own
    line only where it happens apart from the fuller story it usually rides in.
    """
    grams: dict[tuple[str, ...], dict] = {}
    for name, acts in stories.items():
        seq = collapse([a for a in acts if a not in noise])
        for n in range(2, max_len + 1):
            for i in range(len(seq) - n + 1):
                g = tuple(seq[i:i + n])
                slot = grams.setdefault(g, {"tapes": set(), "occurrences": 0})
                slot["tapes"].add(name)
                slot["occurrences"] += 1
    kept = {g: v for g, v in grams.items() if len(v["tapes"]) >= min_support}

    def buried(g: tuple[str, ...]) -> bool:
        for h, hv in kept.items():
            if len(h) <= len(g):
                continue
            inside = any(h[i:i + len(g)] == g for i in range(len(h) - len(g) + 1))
            if inside and hv["tapes"] >= kept[g]["tapes"] \
                    and hv["occurrences"] >= kept[g]["occurrences"]:
                return True
        return False

    out = []
    for g, v in kept.items():
        if buried(g):
            continue
        out.append(dict(id=episode_id(g), acts=list(g), support=len(v["tapes"]),
                        occurrences=v["occurrences"], tapes=sorted(v["tapes"])[:3]))

    # One line per ritual — the third rule in the module docstring.
    best: dict[frozenset, dict] = {}
    for e in out:
        key = frozenset(e["acts"])
        cur = best.get(key)
        if cur is None or (e["support"], e["occurrences"], -len(e["acts"])) > \
                          (cur["support"], cur["occurrences"], -len(cur["acts"])):
            best[key] = e
    out = list(best.values())
    out.sort(key=lambda e: (-e["support"], -len(e["acts"]), -e["occurrences"], e["id"]))
    return out


def merge(scripted: list[dict], live: list[dict]) -> list[dict]:
    """One deck from two sources, the live one on top.

    The distinction this exists for: recordings driven by an authored scenario are
    REHEARSAL, and recordings of real use are the performance. Both are real executions, so
    both mine correctly and neither is fake — but their counts mean different things, and a
    reader who cannot tell them apart will read a scenario's repetitions as usage. An episode
    in both keeps one line and both counts; an episode only the scripted side contains ranks
    below everything live, because it is a pattern somebody anticipated rather than one
    anybody performed.
    """
    by_id: dict[str, dict] = {}
    for e in scripted:
        by_id[e["id"]] = dict(e, source="scripted")
    for e in live:
        if e["id"] in by_id:
            s = by_id[e["id"]]
            s.update(source="both", live_support=e["support"],
                     live_occurrences=e["occurrences"])
        else:
            by_id[e["id"]] = dict(e, source="live")
    rank = {"live": 0, "both": 1, "scripted": 2}
    return sorted(by_id.values(),
                  key=lambda e: (rank[e["source"]], -e["support"], -len(e["acts"]), e["id"]))
