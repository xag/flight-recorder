"""Reading tapes in views small enough to put in a conversation.

A tape can be megabytes; a model reading one through a tool pays for every byte it is handed.
So the budget that matters is the size of the answer, not the size of the evidence: the whole
tape is read here and never returned whole.

Three views, by depth rather than by item:

    index(tape)   the session and one line per call        — what happened
    call(tape, i) every event of one call, payloads clipped — how it happened
    value(...)    one payload, whole (or a named slice)     — the detail that matters

The middle view returns ALL of a call's events, with each payload clipped, rather than a page
of events in full. The sequence is what tells you what happened; the detail lives in one or two
values you then ask for by name. Paginating events would trade one large answer for N round
trips, and N tool calls plus N results cost more context than the thing they were avoiding.
Where a range is genuinely needed it is a range (`lo:hi`), never one item at a time.

Every bound reports what it dropped. A view that looks complete and is not is the same failure
as a test that passes without touching the artifact.

Payloads are read as raw JSON rather than revived through `serial.from_jsonable`: this module
clips values it never interprets, and reviving a 4 MB structure in order to truncate it is work
done to be thrown away. The envelope fields it does read (`ev`, `fn`, `ms`, `error`, `events`,
`k`, `name`) are the frozen part of the tape spec (spec/tape-v1.md), so these views read a tape
from any runtime.

Stdlib only, like the rest of the package: the views take bytes, and where the bytes come from
(a directory, a bucket, a store over HTTP) is the caller's business.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# Clip lengths chosen so a 40-event call lands around 4-6 KB of text: enough to read the shape
# of every step, small enough that reading a call is never the thing that fills a context.
CLIP = 240
HEAD_TAIL = 0.6   # of a clip, how much comes from the head; the rest is the tail


class TapeError(Exception):
    """A tape could not be read as asked; the message is safe to show."""


def _clip(value: Any, limit: int = CLIP) -> tuple[str, Optional[int]]:
    """Render a value small. Returns (text, full_size or None if it fitted).

    Clips head AND tail: the first rows and the last rows of a response say far more than
    twice as much of the beginning.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=repr)
    if len(text) <= limit:
        return text, None
    head = int(limit * HEAD_TAIL)
    tail = limit - head
    return f"{text[:head]} …{len(text) - limit} more… {text[-tail:]}", len(text)


def _lines(tape: bytes) -> list[dict]:
    out = []
    for raw in tape.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            # A tape whose last line is half-written is normal: the recorder publishes as it
            # grows. Skipping it is honest; failing the whole read is not.
            continue
    return out


RUNTIMES = ("python", "node", "dotnet", "go", "java", "php")
FORMAT_VERSIONS = (1,)


def _read(tape: bytes) -> tuple[dict, list[dict]]:
    """The header and the calls. Refuses a format version these views do not implement, as
    the spec requires of a reader; a tape with no header yet is read for what it has."""
    records = _lines(tape)
    header = next((r for r in records if r.get("ev") == "session"), {})
    if "version" in header and header["version"] not in FORMAT_VERSIONS:
        raise TapeError(f"tape format version {header['version']!r} is not one this reader "
                        f"implements ({', '.join(map(str, FORMAT_VERSIONS))})")
    return header, [r for r in records if r.get("ev") == "call"]


def runtime(header: dict) -> Optional[str]:
    """Which runtime recorded the tape, and its version: the header carries exactly one of
    the runtime keys (`"go": "go1.26.5"`), and adding a runtime adds a key."""
    for key in RUNTIMES:
        if key in header:
            return f"{key} {header[key]}"
    return None


def index(tape: bytes) -> dict:
    """The session, and one line per call. The view you start from."""
    header, calls = _read(tape)
    return {
        "started": header.get("started"),
        "runtime": runtime(header),
        "constants": header.get("constants"),
        "calls": len(calls),
        "bytes": len(tape),
        "index": [{
            "i": i,
            "fn": c.get("fn"),
            "ms": c.get("ms"),
            "events": len(c.get("events") or []),
            "error": c.get("error"),
            "spans": sorted({e.get("name") for e in (c.get("events") or [])
                             if e.get("k") == "sem" and e.get("name")}),
        } for i, c in enumerate(calls)],
    }


def trace_index(trace: bytes) -> dict:
    """The testimony, in order: one line per call, the acts it claimed, the evidence beneath.

    A TRACE is the reduction a tape store may derive from a tape so that something outlives
    the tape (see flight_sink.traces). It is tape-shaped JSONL, so `call()` reads one of its
    calls unchanged. What it needs of its own is the INDEX, because the two artifacts are read
    for opposite reasons and the tape index answers the wrong question about a trace.

    `index()` reports spans as a sorted SET — right for a tape, where you are hunting for the
    call that did the thing. A trace exists to answer "did this sequence happen", so the
    sequence is the content: these are the acts in the order they were claimed, taken from the
    `begin` phase so an act appears once rather than twice.

    `evidence` counts the raw events beneath the acts, where the trace kept them (a trace that
    keeps each raw event reduced to its name is enough for licensing and totality to be
    checked for ever). An `acts` of 0 beside a non-zero `evidence` is a call that testified to
    nothing while doing things at the boundary, visible without opening the call.
    """
    header, calls = _read(trace)
    rows = []
    for i, c in enumerate(calls):
        evs = c.get("events") or []
        sem = [e for e in evs if e.get("k") == "sem"]
        rows.append({
            "i": i,
            "fn": c.get("fn"),
            "acts": [e.get("name") for e in sem
                     if e.get("phase") in (None, "begin") and e.get("name")],
            "evidence": len(evs) - len(sem),
            "error": c.get("error"),
        })
    return {
        "started": header.get("started"),
        # Whatever the depositor stamped rides verbatim on a trace, so a trace can say which
        # generation of the alphabet it belongs to — the thing that makes it comparable to
        # traces from releases either side of it.
        "generation": {k: header[k] for k in ("model", "commit", "version") if k in header},
        "calls": len(calls),
        "bytes": len(trace),
        "acts": sum(len(r["acts"]) for r in rows),
        "index": rows,
    }


def call(tape: bytes, i: int, events: Optional[str] = None, clip: int = CLIP) -> dict:
    """Every event of one call, in order, with payloads clipped.

    `events` optionally narrows to a range, "lo:hi" — a segment, never a single item.
    """
    _, calls = _read(tape)
    if not 0 <= i < len(calls):
        raise TapeError(f"no call {i}: this tape has {len(calls)}")
    c = calls[i]
    evs = c.get("events") or []

    lo, hi = 0, len(evs)
    if events:
        try:
            a, _, b = events.partition(":")
            lo = int(a) if a else 0
            hi = int(b) if b else len(evs)
        except ValueError:
            raise TapeError(f"bad event range {events!r}; use 'lo:hi'")
        lo, hi = max(0, lo), min(len(evs), hi)

    shaped, clipped = [], 0
    for n, e in enumerate(evs[lo:hi], start=lo):
        row: dict = {"n": n, "k": e.get("k"), "name": e.get("name") or e.get("fn")}
        for field in ("args", "kwargs", "data", "res", "err"):
            if field in e and e[field] not in (None, {}, []):
                text, full = _clip(e[field], clip)
                row[field] = text
                if full is not None:
                    row[f"{field}_bytes"] = full
                    clipped += 1
        shaped.append(row)

    kwargs_text, kwargs_full = _clip(c.get("kwargs"), clip)
    result_text, result_full = _clip(c.get("result"), clip)
    out = {
        "i": i, "fn": c.get("fn"), "ms": c.get("ms"), "error": c.get("error"),
        "kwargs": kwargs_text, "result": result_text,
        "events": shaped,
        "showing": f"{lo}:{hi} of {len(evs)}",
    }
    if kwargs_full:
        out["kwargs_bytes"] = kwargs_full
    if result_full:
        out["result_bytes"] = result_full
    if clipped:
        out["clipped"] = f"{clipped} payload(s) clipped to {clip} chars — read_tape_value for one whole"
    if hi < len(evs) or lo > 0:
        out["omitted"] = f"{len(evs) - (hi - lo)} event(s) outside the requested range"
    return out


def value(tape: bytes, i: int, event: int, field: str = "res", limit: int = 20000) -> dict:
    """One payload, whole — up to a hard cap, which is reported when it bites."""
    _, calls = _read(tape)
    if not 0 <= i < len(calls):
        raise TapeError(f"no call {i}: this tape has {len(calls)}")
    evs = calls[i].get("events") or []
    if not 0 <= event < len(evs):
        raise TapeError(f"no event {event} in call {i}: it has {len(evs)}")
    raw = evs[event].get(field)
    if raw is None:
        present = [f for f in ("args", "kwargs", "data", "res", "err") if f in evs[event]]
        raise TapeError(f"event {event} has no {field!r}; it has {present}")
    text = json.dumps(raw, ensure_ascii=False, indent=1, default=repr)
    out = {"call": i, "event": event, "field": field, "bytes": len(text)}
    if len(text) > limit:
        out["value"] = text[:limit]
        out["truncated"] = f"{len(text) - limit} chars not shown of {len(text)}"
    else:
        out["value"] = text
    return out
