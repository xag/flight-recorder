"""Boundary value (de)serialization.

Everything that crosses the recorded boundary is encoded as JSON with revivable markers for
datetimes/dates; anything exotic degrades to an opaque repr (which then can't be revived —
acceptable, because well-factored apps read plain JSON-ish data plus datetimes back from
their stores).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

_MAX_DEPTH = 16


def to_jsonable(v: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return {"__opaque__": repr(v)[:200]}
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
    return {"__opaque__": repr(v)[:200]}


def from_jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        if len(v) == 1:
            if "__dt__" in v:
                return datetime.fromisoformat(v["__dt__"])
            if "__date__" in v:
                return date.fromisoformat(v["__date__"])
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
