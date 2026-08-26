"""Reading traces back: the other direction from the sink.

Arrived from an application that had grown it in-tree to mine its own usage. What is pinned
here is the contract that made it safe to call from inside a recorded execution: never
raises, says how much history it did not return, and caches inside the call rather than
around it.
"""

from __future__ import annotations

import json

import pytest

from flight_sink import traces


def envelopes(lines):
    """A minimal call-envelope reader — what flight_recorder.episodes.story does, spelled
    out here so these tests need no dependency this package does not have."""
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("fn"):
            out.append(str(rec["fn"]))
    return out


@pytest.fixture(autouse=True)
def _clear_cache():
    traces._cache.clear()
    yield
    traces._cache.clear()


def _fake_store(monkeypatch, listing, bodies, calls=None):
    def _get(url, token, timeout=5.0):
        if calls is not None:
            calls.append(url)
        if url.endswith("/traces/app"):
            return json.dumps(listing).encode()
        return bodies[url.rsplit("/", 1)[-1]].encode()
    monkeypatch.setattr(traces, "_get", _get)


def test_it_returns_the_stories_and_says_how_much_it_left_out(monkeypatch):
    """The count is the store's whole history, not the page returned: a rolling window that
    silently shrinks is worse than one that says how far it reaches."""
    listing = {"traces": [{"name": f"t{i}.jsonl"} for i in range(5)]}
    bodies = {f"t{i}.jsonl": json.dumps({"fn": "act", "seq": 1}) for i in range(5)}
    _fake_store(monkeypatch, listing, bodies)

    out = traces.fetch("https://store/", "app", "tok", envelopes, limit=2)
    assert list(out["stories"]) == ["t0.jsonl", "t1.jsonl"]
    assert out["traces"] == 5, "the cap must not hide how much history exists"
    assert out["stories"]["t0.jsonl"] == ["act"]


def test_unconfigured_is_empty_and_never_an_error():
    assert traces.fetch("", "app", "tok", envelopes) == {"stories": {}, "traces": 0}
    assert traces.fetch("https://store", "app", "", envelopes) == {"stories": {}, "traces": 0}


def test_a_store_that_fails_costs_the_caller_nothing(monkeypatch):
    """A miner is a diagnostic. It must not be able to take down the surface that called it."""
    def _boom(url, token, timeout=5.0):
        raise OSError("connection refused")
    monkeypatch.setattr(traces, "_get", _boom)
    assert traces.fetch("https://store", "app", "tok", envelopes) == {"stories": {}, "traces": 0}


def test_a_trace_that_yields_no_acts_is_left_out(monkeypatch):
    listing = {"traces": [{"name": "empty.jsonl"}, {"name": "full.jsonl"}]}
    bodies = {"empty.jsonl": "not json at all",
              "full.jsonl": json.dumps({"fn": "act", "seq": 1})}
    _fake_store(monkeypatch, listing, bodies)
    out = traces.fetch("https://store", "app", "tok", envelopes)
    assert list(out["stories"]) == ["full.jsonl"]
    assert out["traces"] == 2


def test_the_cache_sits_inside_the_call(monkeypatch):
    """What a recorded execution captures should be what the caller actually got — cached or
    not — so the cache cannot live around this function."""
    calls: list[str] = []
    listing = {"traces": [{"name": "t.jsonl"}]}
    _fake_store(monkeypatch, listing, {"t.jsonl": json.dumps({"fn": "a"})}, calls)

    first = traces.fetch("https://store", "app", "tok", envelopes)
    n = len(calls)
    second = traces.fetch("https://store", "app", "tok", envelopes)
    assert second == first and len(calls) == n, "a cache hit still returns the same answer"

    third = traces.fetch("https://store", "app", "tok", envelopes, ttl=0)
    assert third == first and len(calls) > n, "an expired entry goes back to the store"


def test_a_custom_reader_replaces_the_default(monkeypatch):
    """The reader is the caller's: this package knows transport, not tape format."""
    listing = {"traces": [{"name": "t.jsonl"}]}
    _fake_store(monkeypatch, listing, {"t.jsonl": "one\ntwo"})
    out = traces.fetch("https://store", "app", "tok", lambda lines: list(lines))
    assert out["stories"]["t.jsonl"] == ["one", "two"]
