"""Read back the traces a tape store derived from the tapes this sink deposited.

The sink writes; this reads. `http.py` sends tapes away so an app that records need carry no
storage of its own — and that leaves the app unable to look at its own history, which is
usually right (a human reads tapes, later, from somewhere else) and occasionally wrong: an
app that wants to mine its own usage needs the history back.

A TRACE is not a tape. A store that keeps tapes on a ring buffer derives, at deposit, a
reduction that outlives them — every call envelope in order, the semantic spans, and none of
the raw boundary events. That is the half worth keeping for ever, and dropping the raw events
is what makes a trace small enough to keep after its tape has rolled off. What those lines
MEAN is not this package's business: the caller passes a reader.

Same posture as `http.py`: `urllib` from the standard library, no dependency added to the app
being observed. And the same refusal to be load-bearing — an unreachable or unconfigured
store returns empty, never raises. A miner is a diagnostic; it must not be able to take down
the surface that called it.

DECLARE IT AT THE BOUNDARY. Whatever calls `fetch` inside a recorded execution is doing I/O
against a store whose contents move with every deposit: undeclared, the fetch re-executes on
replay against a store that has changed, and every tape that ever called it diverges. It is
an effect like any other, and the caller is the one who must say so.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Newest-first cap. Enough history to see the current shape of things; small enough that the
# result rides a tape without bloating it, since a story is act names and nothing else.
LIMIT = 40
TTL = 600.0

_cache: dict[tuple, tuple[float, dict]] = {}


def _get(url: str, token: str, timeout: float = 5.0) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch(base_url: str, app: str, token: str,
          reader: Callable[[list[str]], list[str]],
          limit: int = LIMIT, ttl: float = TTL, timeout: float = 5.0) -> dict[str, Any]:
    """`{"stories": {trace-name: [act, ...]}, "traces": <how many the store holds>}`.

    `traces` is the store's whole count rather than the number returned, so a caller can see
    how much history the cap left out — a rolling window that silently shrinks is worse than
    one that says how far it reaches.

    `reader` turns a trace's lines into its ordered acts, and it is REQUIRED rather than
    defaulted. This package is transport: it knows how to reach a store and nothing about
    what a trace means. Defaulting it to `flight_recorder.episodes.story` was the first
    shape here, and the test suite refused it in one run — flight-sink has no dependency on
    flight-recorder and should not grow one to be a convenience. A caller that reads call
    envelopes passes that function; a caller reading something else passes something else.

    Returns empty on anything at all going wrong, and logs it. The TTL cache sits INSIDE this
    call on purpose: what a recorded execution captures should be what the caller actually
    got, cached or not.
    """
    empty: dict[str, Any] = {"stories": {}, "traces": 0}
    if not base_url or not token:
        return empty
    base = base_url.rstrip("/")
    key = (base, app, limit)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        listing = json.loads(_get(f"{base}/traces/{app}", token, timeout))
        names = [t["name"] for t in listing.get("traces", [])][:limit]
        stories = {}
        for name in names:
            body = _get(f"{base}/traces/{app}/{name}", token, timeout)
            acts = reader(body.decode("utf-8", "replace").splitlines())
            if acts:
                stories[name] = acts
        out = {"stories": stories, "traces": len(listing.get("traces", []))}
        _cache[key] = (time.monotonic(), out)
        return out
    except Exception as e:                                     # never cost the caller
        logger.warning("traces unavailable from %s: %s", base, e)
        return empty
