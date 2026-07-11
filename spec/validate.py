"""Tape v1 conformance checker — the normative one.

`spec/tape-v1.md` is the prose; this is the arbiter. It is deliberately written against
nothing but the JSON: it imports no part of flight_recorder, so it cannot accidentally
bless whatever the Python implementation happens to do. The Node port carries a mirror of
this file (`js/src/spec/validate.js`), and both must agree on every fixture.

Returns a list of human-readable violations; empty means conformant.
"""

from __future__ import annotations

import json
from typing import Any

VERSION = 1
MAX_DEPTH = 16
MARKERS = {"__dt__", "__date__", "__opaque__"}
# Reserved by the trace encoding — a *reader* must tolerate them, so they are legal in a
# tape even though a v1 recorder never emits them.
RESERVED_MARKERS = {"__snap__", "__seq__", "__str__", "__esc__"}
EVENT_KINDS = {"fx", "db", "now", "rand"}


def _is_iso(s: Any) -> bool:
    if not isinstance(s, str):
        return False
    from datetime import datetime
    try:
        datetime.fromisoformat(s)
        return True
    except ValueError:
        return False


def _is_tz_aware(s: Any) -> bool:
    from datetime import datetime
    if not _is_iso(s):
        return False
    return datetime.fromisoformat(s).tzinfo is not None


def _check_value(v: Any, path: str, out: list, depth: int = 0) -> None:
    """A boundary value: JSON, with at most a marker at any node."""
    if depth > MAX_DEPTH:
        out.append(f"{path}: nested deeper than {MAX_DEPTH}; must degrade to __opaque__")
        return
    if v is None or isinstance(v, (str, int, float, bool)):
        return
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check_value(x, f"{path}[{i}]", out, depth + 1)
        return
    if isinstance(v, dict):
        if len(v) == 1:
            k = next(iter(v))
            if k in MARKERS:
                if k in ("__dt__", "__date__") and not _is_iso(v[k]):
                    out.append(f"{path}: {k} payload is not ISO-8601: {v[k]!r}")
                if k == "__opaque__":
                    if not isinstance(v[k], str):
                        out.append(f"{path}: __opaque__ payload must be a string")
                    elif len(v[k]) > 200:
                        out.append(f"{path}: __opaque__ payload exceeds 200 chars")
                return
            if k in RESERVED_MARKERS:
                return  # reserved: legal, not interpreted here
        for k, x in v.items():
            if not isinstance(k, str):
                out.append(f"{path}: object key {k!r} is not a string")
            _check_value(x, f"{path}.{k}", out, depth + 1)
        return
    out.append(f"{path}: {type(v).__name__} is not JSON")


def _check_snapshot(s: Any, path: str, out: list) -> None:
    if not isinstance(s, dict):
        out.append(f"{path}: snapshot must be an object")
        return
    for key in ("id", "exists", "data"):
        if key not in s:
            out.append(f"{path}: snapshot missing {key!r}")
    if "exists" in s and not isinstance(s["exists"], bool):
        out.append(f"{path}.exists: must be a bool")
    if "data" in s:
        _check_value(s["data"], f"{path}.data", out)


def _check_event(e: Any, path: str, out: list) -> None:
    if not isinstance(e, dict):
        out.append(f"{path}: event must be an object")
        return
    k = e.get("k")
    if k not in EVENT_KINDS:
        return  # unknown kind: a reader must ignore it (forward compatibility)

    if k == "fx":
        if not isinstance(e.get("fn"), str):
            out.append(f"{path}: fx needs a string 'fn'")
        if not isinstance(e.get("args"), list):
            out.append(f"{path}: fx needs an array 'args'")
        else:
            _check_value(e["args"], f"{path}.args", out)
        if not isinstance(e.get("kwargs"), dict):
            out.append(f"{path}: fx needs an object 'kwargs' ({{}} in JS)")
        else:
            _check_value(e["kwargs"], f"{path}.kwargs", out)
        has_res, has_err = "res" in e, "err" in e
        if has_res == has_err:
            out.append(f"{path}: fx must carry exactly one of 'res' / 'err'")
        if has_res:
            _check_value(e["res"], f"{path}.res", out)
        if has_err:
            err = e["err"]
            if not isinstance(err, dict) or not isinstance(err.get("type"), str):
                out.append(f"{path}.err: must be an object with a string 'type'")

    elif k == "db":
        if not isinstance(e.get("op"), str):
            out.append(f"{path}: db needs a string 'op'")
        if not isinstance(e.get("sig"), str):
            out.append(f"{path}: db needs a string 'sig'")
        has_res, has_args = "res" in e, "args" in e
        if has_res and has_args:
            out.append(f"{path}: db carries 'res' (a read) or 'args' (a write), never both")
        if not has_res and not has_args:
            out.append(f"{path}: db must carry 'res' or 'args'")
        if has_res:
            r = e["res"]
            if isinstance(r, list):
                for i, s in enumerate(r):
                    _check_snapshot(s, f"{path}.res[{i}]", out)
            else:
                _check_snapshot(r, f"{path}.res", out)
        if has_args:
            _check_value(e["args"], f"{path}.args", out)

    elif k == "now":
        # ISO-8601, and deliberately NOT required to be timezone-aware. This is an
        # app-visible value, not recorder metadata: the app called now() and got back
        # whatever it got back. `datetime.now()` is naive, and in Python comparing a naive
        # datetime with an aware one raises — so a replay that "helpfully" handed back an
        # aware value where the recording saw a naive one would change behaviour, which is
        # the one thing replay may never do. Round-trip exactly what the app saw.
        if not _is_iso(e.get("v")):
            out.append(f"{path}: now.v must be an ISO-8601 string, got {e.get('v')!r}")

    elif k == "rand":
        if e.get("m") != "sample":
            out.append(f"{path}: rand.m must be 'sample' in v1, got {e.get('m')!r}")
        for key in ("n", "kk"):
            if not isinstance(e.get(key), int):
                out.append(f"{path}: rand.{key} must be an int")
        idx = e.get("idx")
        if not isinstance(idx, list) or not all(isinstance(i, int) for i in idx):
            out.append(f"{path}: rand.idx must be an array of ints")
        elif isinstance(e.get("n"), int):
            bad = [i for i in idx if not 0 <= i < e["n"]]
            if bad:
                out.append(f"{path}: rand.idx {bad} out of range for population {e['n']}")
            if isinstance(e.get("kk"), int) and len(idx) != e["kk"]:
                out.append(f"{path}: rand.idx has {len(idx)} positions but kk={e['kk']}")


def validate_line(obj: Any, i: int, out: list, *, first: bool) -> None:
    if not isinstance(obj, dict):
        out.append(f"line {i}: not an object")
        return
    ev = obj.get("ev")

    if first:
        if ev != "session":
            out.append(f"line {i}: the first line must be the session header, got ev={ev!r}")
            return
    elif ev == "session":
        out.append(f"line {i}: a second session header")
        return

    if ev == "session":
        if obj.get("version") != VERSION:
            out.append(f"line {i}: version must be {VERSION}, got {obj.get('version')!r}")
        if not _is_tz_aware(obj.get("started")):
            out.append(f"line {i}: session.started must be timezone-aware ISO-8601")
        if not isinstance(obj.get("constants"), dict):
            out.append(f"line {i}: session.constants must be an object")
        else:
            _check_value(obj["constants"], f"line {i}.constants", out)
        runtimes = [k for k in ("python", "node") if k in obj]
        if len(runtimes) != 1:
            out.append(f"line {i}: session must name exactly one runtime (python|node), got {runtimes}")
        return

    if ev == "call":
        seq = obj.get("seq")
        if not isinstance(seq, int) or seq < 1:
            out.append(f"line {i}: call.seq must be an int >= 1")
        if not isinstance(obj.get("fn"), str):
            out.append(f"line {i}: call.fn must be a string")
        if not isinstance(obj.get("kwargs"), dict):
            out.append(f"line {i}: call.kwargs must be an object")
        else:
            _check_value(obj["kwargs"], f"line {i}.kwargs", out)
        if "result" in obj:
            _check_value(obj["result"], f"line {i}.result", out)
        if "error" not in obj:
            out.append(f"line {i}: call must carry 'error' (null when it did not raise)")
        elif obj["error"] is not None and not isinstance(obj["error"], str):
            out.append(f"line {i}: call.error must be a string or null")
        if not _is_tz_aware(obj.get("ts")):
            out.append(f"line {i}: call.ts must be timezone-aware ISO-8601")
        if not isinstance(obj.get("ms"), (int, float)):
            out.append(f"line {i}: call.ms must be a number")
        evs = obj.get("events")
        if not isinstance(evs, list):
            out.append(f"line {i}: call.events must be an array")
        else:
            for j, e in enumerate(evs):
                _check_event(e, f"line {i}.events[{j}]", out)
        return

    # unknown ev (e.g. the reserved "inflight"): a reader must tolerate it.


def validate_tape(text: str) -> list[str]:
    """Validate a whole tape. Returns violations; empty means conformant."""
    out: list[str] = []
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ["empty tape: the session header is mandatory"]

    seqs = []
    for i, ln in enumerate(lines):
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError as e:
            # Only the final line may be torn (the process died mid-write).
            if i == len(lines) - 1:
                continue
            out.append(f"line {i}: not JSON ({e})")
            continue
        validate_line(obj, i, out, first=(i == 0))
        if isinstance(obj, dict) and obj.get("ev") == "call" and isinstance(obj.get("seq"), int):
            seqs.append(obj["seq"])

    if seqs != sorted(seqs) or (seqs and seqs != list(range(1, len(seqs) + 1))):
        out.append(f"call.seq must be 1-based and monotonic; got {seqs}")

    return out
