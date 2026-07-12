"""Boundary value (de)serialization.

Everything that crosses the recorded boundary is encoded as JSON with revivable markers for
datetimes/dates; anything exotic degrades to an opaque repr (which then can't be revived —
acceptable, because well-factored apps read plain JSON-ish data plus datetimes back from
their stores).

Traced *internal* values are a different problem, handled by trace_jsonable: they are not
inputs to be revived faithfully but claims to be asserted against, they are captured on
every executed line, and they include whatever objects the code happens to hold. So they
are recorded as data (not reprs — you cannot do arithmetic on `'2'`, and `<Snap object at
0x7f…>` is both opaque and different on every run), document snapshots are unwrapped, and
anything long is cut to a prefix that still knows its true length.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Optional

_MAX_DEPTH = 16

# What a redacted field's value becomes under a bare (None) rule.
REDACTED = "[REDACTED]"

# Caps for traced values. A local can be a 10k-row list, and the tracer snapshots it on
# every line that touches its frame.
TRACE_MAX_ITEMS = 100
TRACE_MAX_CHARS = 512


def _opaque_value(v: Any) -> dict:
    """A value the tape cannot represent, marked — with the memory address scrubbed.

    The default repr of an object carries its id: `<Image object at 0x7f3c…>`. Recording
    that is recording a POINTER, and a pointer is different on every run — so the effect
    or result it belongs to can never match on replay, and the divergence has nothing to
    do with the code under test. Any tool returning a plain object (an image, a handle)
    was unreplayable purely because of this. The tracer already scrubbed addresses for
    exactly this reason (`_opaque` below); the tape did not.
    """
    return {"__opaque__": _ADDR.sub("", repr_or_placeholder(v))[:200]}


def repr_or_placeholder(v: Any) -> str:
    try:
        return repr(v)
    except Exception as e:  # a repr that raises must not take the recording down with it
        return f"<unreprable {type(v).__name__}: {type(e).__name__}>"


def to_jsonable(v: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return _opaque_value(v)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return {"__dt__": v.isoformat()}
    if isinstance(v, date):
        return {"__date__": v.isoformat()}
    if isinstance(v, dict):
        return {str(k): to_jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [to_jsonable(x, depth + 1) for x in v]
    return _opaque_value(v)


def redact_jsonable(v: Any, rules: dict) -> Any:
    """Apply field-name redaction rules (see Boundary.redact) to a jsonable tree. A dict
    entry whose key is named in `rules` has its value replaced — by REDACTED when the rule
    is None, else by the rule applied to the (jsonable) value; everything else recurses.
    A rule that raises degrades to REDACTED: the failure direction is 'masked', never
    'leaked' and never 'broke the recorded call'."""
    if not rules:
        return v
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k in rules:
                rule = rules[k]
                if rule is None:
                    out[k] = REDACTED
                else:
                    try:
                        out[k] = rule(x)
                    except Exception:
                        out[k] = REDACTED
            else:
                out[k] = redact_jsonable(x, rules)
        return out
    if isinstance(v, list):
        return [redact_jsonable(x, rules) for x in v]
    return v


def forbidden_hit(text: str, patterns: Any) -> Optional[str]:
    """The first Boundary.forbid pattern that matches `text`, or None if it is clean.

    Scans the SERIALIZED record, not the value tree, and that is the whole point. Redaction
    is field-name driven, so it protects exactly the fields you named; a secret reaches the
    tape through every path a field name cannot see — a positional argument, a chain
    signature, an opaque repr, a key, a string some effect built by concatenation. The one
    thing all of those have in common is that they end up in the line about to be written.
    So the tripwire reads that line.

    Returns the PATTERN, never the match. The caller puts this in an exception message, and
    a tripwire that quotes the credential it caught — into a log, a stack trace, an issue —
    is the leak it exists to prevent.
    """
    for p in patterns:
        if p.search(text):
            return p.pattern
    return None


def from_jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        if len(v) == 1:
            if "__dt__" in v:
                return datetime.fromisoformat(v["__dt__"])
            if "__date__" in v:
                return date.fromisoformat(v["__date__"])
            # JavaScript has two nothings; Python has one. A JS recorder distinguishes
            # `undefined` from `null` because a replay there can depend on it — reading such
            # a tape here, both are simply None. Python never emits this marker.
            if "__undef__" in v:
                return None
            if "__opaque__" in v:
                return v["__opaque__"]
        return {k: from_jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [from_jsonable(x) for x in v]
    return v


def snapshot_jsonable(snap: Any) -> dict:
    """Serialize a document snapshot (anything with .id/.exists/.to_dict) — identity,
    existence, data; the only surface a well-behaved consumer reads."""
    exists = bool(getattr(snap, "exists", True))
    data = snap.to_dict() if exists else None
    return {"id": getattr(snap, "id", None), "exists": exists, "data": to_jsonable(data)}


def short(v: Any, limit: int = 60) -> str:
    """Compact stable rendering of a chain-call argument for signatures."""
    try:
        s = json.dumps(to_jsonable(v), ensure_ascii=False, default=repr)
    except Exception:
        s = repr(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def safe_repr(v: Any, limit: int = 160) -> str:
    try:
        r = repr(v)
    except Exception:
        return "<unreprable>"
    return r if len(r) <= limit else r[: limit - 1] + "…"


# --- traced internal values -----------------------------------------------------------

class Truncated(list):
    """A traced sequence cut to a prefix. `len()` is the TRUE length; the contents are the
    first TRACE_MAX_ITEMS elements. So `len(docs) > 0` is trustworthy while `docs[500]` is
    not there to be read."""

    def __init__(self, head: list, total: int):
        super().__init__(head)
        self.total = total

    def __len__(self) -> int:
        return self.total

    def __repr__(self) -> str:
        return f"<{self.total} items, first {list.__len__(self)} traced: {list.__repr__(self)}>"


class TruncatedText(str):
    """A traced string cut to a prefix. `len()` is the TRUE length; the value is the head."""

    def __new__(cls, head: str, total: int):
        s = super().__new__(cls, head)
        s.total = total
        return s

    def __len__(self) -> int:
        return self.total


_ADDR = re.compile(r" at 0x[0-9A-Fa-f]+")

# Every single-key marker the trace encoding uses. A user dict that happens to have exactly
# this shape must be escaped on encode, or revival would mistake it for a marker.
_MARKERS = frozenset({"__dt__", "__date__", "__undef__", "__opaque__", "__snap__", "__seq__",
                      "__str__", "__esc__"})


def _opaque(v: Any) -> dict:
    """An untraceable value's marker. The memory address is scrubbed from the repr: it is
    noise to a reader and nondeterminism to a trace — two replays of the same execution
    must produce byte-identical traces, and `<list_iterator at 0x7f…>` never would."""
    return {"__opaque__": _ADDR.sub("", safe_repr(v))}


def _snapshottable(v: Any) -> bool:
    # getattr-with-default only swallows AttributeError; a proxy whose __getattr__ raises
    # something else must not detonate a probe that runs on every local of every line.
    try:
        return callable(getattr(v, "to_dict", None)) and hasattr(v, "exists")
    except Exception:
        return False


def trace_jsonable(v: Any, depth: int = 0) -> Any:
    """Encode one traced internal value. Unlike to_jsonable this unwraps document snapshots
    and caps long values, because it runs on every local of every executed line.

    It must NEVER raise: it is called from inside a sys.settrace callback, and an exception
    there is injected into the frame being traced — corrupting the very replay the trace is
    meant to observe. Anything hostile degrades to an opaque marker instead."""
    try:
        return _trace_encode(v, depth)
    except Exception:
        return _opaque(v)


def _trace_encode(v: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return _opaque(v)
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, str):
        if len(v) <= TRACE_MAX_CHARS:
            return v
        return {"__str__": {"len": len(v), "head": v[:TRACE_MAX_CHARS]}}
    if isinstance(v, datetime):
        return {"__dt__": v.isoformat()}
    if isinstance(v, date):
        return {"__date__": v.isoformat()}
    if _snapshottable(v):  # a document snapshot: the surface a consumer actually reads
        try:
            return {"__snap__": snapshot_jsonable(v)}
        except Exception:
            return _opaque(v)
    if isinstance(v, dict):
        if len(v) == 1 and next(iter(v), None) in _MARKERS:
            # a user dict shaped exactly like a marker: escape it so it revives as itself
            k = next(iter(v))
            return {"__esc__": {str(k): trace_jsonable(v[k], depth + 1)}}
        return {str(k): trace_jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        if isinstance(v, (set, frozenset)):
            # hash order varies per process (PYTHONHASHSEED); a trace must not
            try:
                items = sorted(v, key=safe_repr)
            except Exception:
                items = list(v)
        else:
            items = list(v)
        if len(items) <= TRACE_MAX_ITEMS:
            return [trace_jsonable(x, depth + 1) for x in items]
        head = [trace_jsonable(x, depth + 1) for x in items[:TRACE_MAX_ITEMS]]
        return {"__seq__": {"len": len(items), "head": head}}
    return _opaque(v)


def from_trace_jsonable(v: Any) -> Any:
    """Revive a traced value into something an invariant can assert on."""
    if isinstance(v, dict):
        if len(v) == 1:
            if "__dt__" in v:
                return datetime.fromisoformat(v["__dt__"])
            if "__date__" in v:
                return date.fromisoformat(v["__date__"])
            if "__opaque__" in v:
                return v["__opaque__"]
            if "__snap__" in v:
                return from_trace_jsonable(v["__snap__"])
            if "__seq__" in v:
                spec = v["__seq__"]
                return Truncated([from_trace_jsonable(x) for x in spec["head"]], spec["len"])
            if "__str__" in v:
                spec = v["__str__"]
                return TruncatedText(spec["head"], spec["len"])
            if "__esc__" in v:  # a user dict that merely looked like a marker
                return {k: from_trace_jsonable(x) for k, x in v["__esc__"].items()}
        return {k: from_trace_jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [from_trace_jsonable(x) for x in v]
    return v


def render(v: Any, limit: int = 90) -> str:
    """One-line display of a traced value, for --watch."""
    try:
        s = json.dumps(v, ensure_ascii=False, default=repr)
    except Exception:
        s = safe_repr(v)
    return s if len(s) <= limit else s[: limit - 1] + "…"
