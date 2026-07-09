"""Mutation: author hostile boundary states as data (issue #8, the second half of #2).

Recordings make impossible states cheap to construct — an emptied corpus, a clock running
backwards, an oversized collection are edits to a JSONL file, not database setup. A mutated
recording replays in **probe mode**: the tape answers the code's boundary questions
(matched by name, order-monotonic, skipping allowed) but no longer polices arguments,
writes, or outputs — under mutation those comparisons are meaningless. The verdict belongs
to invariants: a mutated recording plus a declared claim IS a property test over the
boundary.

    rec = fr.Recording.load(path)
    call = rec.call(0)
    call.read(op="stream").result = []        # empty corpus
    call.effect("fetch_remote").result = {"v": 10**9}
    call.clock.reverse()                      # time runs backwards

    report = call.check(adapter, INVARIANTS)
    assert report.ok, fr.format_invariant_report(report)

    rec.save(recordings_dir / "empty-corpus.jsonl")   # pin it: now it's a suite member

A saved mutated call carries `"probe": true`, so replay and the pytest plugin treat it as
a probe fixture automatically — it cannot be mistaken for a strict regression pin.

What mutation can and cannot reach: the tape only holds answers to the questions the
original execution asked. A mutation that redirects the code onto a path that asks a
question the tape has no further answer for is reported as `unanswerable` — a limit of
this recording, impeaching neither the code nor the claim. Record a closer execution, or
edit the events the new path needs.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from flight_recorder.replay import load_session
from flight_recorder.serial import to_jsonable


def _snap_wrap(item: Any, i: int) -> Any:
    """Sugar for authoring read results: a plain dict is understood as document DATA and
    wrapped in snapshot shape; a dict using only snapshot keys (id/exists/data, data
    required) is normalized as a snapshot with defaults filled in. Anything else is a
    ValueError here, at the mutation site — never a confusing crash inside the replay
    that would be blamed on the code under test."""
    if isinstance(item, dict):
        if set(item) <= {"id", "exists", "data"} and "data" in item:
            return {"id": item.get("id", f"row{i}"), "exists": item.get("exists", True),
                    "data": to_jsonable(item["data"])}
        return {"id": f"row{i}", "exists": True, "data": to_jsonable(item)}
    raise ValueError(
        f"a read result must be document dict(s), got {type(item).__name__}: {item!r} — "
        "pass the document's data as a dict (it is wrapped into snapshot shape)")


class EffectHandle:
    """One recorded effect event. Setting `result` replaces its answer; setting `error`
    replaces it with a raised exception (revived on replay via the boundary's revivers)."""

    def __init__(self, ev: dict, owner: "CallHandle"):
        self._ev, self._owner = ev, owner

    @property
    def result(self) -> Any:
        return self._ev.get("res")

    @result.setter
    def result(self, value: Any) -> None:
        self._ev.pop("err", None)
        self._ev["res"] = to_jsonable(value)
        self._owner._dirty()

    @property
    def error(self) -> Optional[dict]:
        return self._ev.get("err")

    @error.setter
    def error(self, exc: Any) -> None:
        """Accepts an Exception instance or a ("TypeName", [args]) pair."""
        if isinstance(exc, BaseException):
            type_name, args = type(exc).__name__, list(getattr(exc, "args", []) or [])
        else:
            type_name, args = exc[0], list(exc[1])
        self._ev.pop("res", None)
        self._ev["err"] = {"type": type_name, "repr": f"{type_name}{tuple(args)!r}",
                           "args": to_jsonable(args)}
        self._owner._dirty()


class ReadHandle:
    """One recorded chain read (a `db` event with a result)."""

    def __init__(self, ev: dict, owner: "CallHandle"):
        self._ev, self._owner = ev, owner

    @property
    def result(self) -> Any:
        return self._ev.get("res")

    @result.setter
    def result(self, value: Any) -> None:
        if isinstance(value, list):
            self._ev["res"] = [_snap_wrap(x, i) for i, x in enumerate(value)]
        else:
            self._ev["res"] = _snap_wrap(value, 0)
        self._owner._dirty()


class RandHandle:
    """One recorded random draw. `idx` is the positions the replayed sample() picks."""

    def __init__(self, ev: dict, owner: "CallHandle"):
        self._ev, self._owner = ev, owner

    @property
    def idx(self) -> list:
        return self._ev.get("idx", [])

    @idx.setter
    def idx(self, value: list) -> None:
        idx = [int(i) for i in value]
        if any(i < 0 for i in idx):
            raise ValueError(f"idx must be non-negative positions, got {idx}")
        self._ev["idx"] = idx
        self._owner._dirty()


class ClockHandle:
    """Every clock read of the call, as a timeline you can rewrite."""

    def __init__(self, evs: list, owner: "CallHandle"):
        self._evs, self._owner = evs, owner

    @property
    def times(self) -> list:
        return [e["v"] for e in self._evs]

    @times.setter
    def times(self, values: list) -> None:
        from datetime import datetime
        if len(values) != len(self._evs):
            raise ValueError(f"{len(self._evs)} clock read(s) recorded, "
                             f"{len(values)} value(s) given")
        encoded = []
        for v in values:
            iso = v.isoformat() if hasattr(v, "isoformat") else str(v)
            try:
                datetime.fromisoformat(iso)  # replay will; fail here, at the author
            except ValueError as e:
                raise ValueError(f"not an ISO datetime: {v!r}") from e
            encoded.append(iso)
        for ev, iso in zip(self._evs, encoded):
            ev["v"] = iso
        self._owner._dirty()

    def reverse(self) -> None:
        """Time runs backwards."""
        self.times = list(reversed(self.times))


class CallHandle:
    """One recorded call, editable."""

    def __init__(self, rec: dict, recording: "Recording"):
        self.record = rec  # the raw dict, for anything the typed handles don't cover
        self._recording = recording

    def _dirty(self) -> None:
        self.record["probe"] = True

    # --- selectors -----------------------------------------------------------

    def _pick(self, matches: list, what: str, occurrence: int) -> dict:
        if not matches:
            have = sorted({e.get("fn") or e.get("op") or e.get("k")
                           for e in self.record["events"]})
            raise KeyError(f"no {what} in this call — its events are: {have}")
        if occurrence >= len(matches):
            raise KeyError(f"only {len(matches)} × {what} recorded, "
                           f"asked for occurrence {occurrence}")
        return matches[occurrence]

    def effect(self, name: str, occurrence: int = 0) -> EffectHandle:
        found = [e for e in self.record["events"] if e["k"] == "fx"
                 and (e.get("fn") == name or e.get("fn", "").endswith("." + name))]
        return EffectHandle(self._pick(found, f"effect '{name}'", occurrence), self)

    def read(self, op: Optional[str] = None, occurrence: int = 0) -> ReadHandle:
        found = [e for e in self.record["events"] if e["k"] == "db" and "res" in e
                 and (op is None or e.get("op") == op)]
        return ReadHandle(self._pick(found, f"read{f' {op}' if op else ''}", occurrence),
                          self)

    def rand(self, occurrence: int = 0) -> RandHandle:
        found = [e for e in self.record["events"] if e["k"] == "rand"]
        return RandHandle(self._pick(found, "random draw", occurrence), self)

    @property
    def clock(self) -> ClockHandle:
        return ClockHandle([e for e in self.record["events"] if e["k"] == "now"], self)

    # --- inputs ---------------------------------------------------------------

    @property
    def kwargs(self) -> dict:
        return self.record["kwargs"]

    def set_kwargs(self, **kv: Any) -> None:
        """Mutate the call's own inputs (oversized values, wrong types...)."""
        for k, v in kv.items():
            self.record["kwargs"][k] = to_jsonable(v)
        self._dirty()

    # --- run ------------------------------------------------------------------

    def check(self, adapter: Any, invariants: Any,
              trace_path: Optional[Path] = None) -> Any:
        """Replay this (mutated) call in probe mode and assert the invariants against
        what the real code does in the mutated world."""
        from flight_recorder.invariants import check_invariants
        self.record["probe"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = self._recording._write(Path(tmp) / "mutated.jsonl")
            index = self._recording.calls.index(self.record)
            return check_invariants(path, index, adapter, invariants,
                                    trace_path=trace_path, probe=True)


class Recording:
    """A session file as an editable object: load, mutate calls, check or pin."""

    def __init__(self, header: dict, calls: list):
        self.header = header
        self.calls = calls

    @classmethod
    def load(cls, path: Path) -> "Recording":
        header, calls = load_session(Path(path))
        return cls(header, calls)

    def call(self, index: int) -> CallHandle:
        if not 0 <= index < len(self.calls):
            raise IndexError(f"call {index} out of range: {len(self.calls)} call(s)")
        return CallHandle(self.calls[index], self)

    def _write(self, path: Path) -> Path:
        with Path(path).open("w", encoding="utf-8") as f:
            for obj in [self.header, *self.calls]:
                f.write(json.dumps(obj, ensure_ascii=False, default=repr) + "\n")
        return Path(path)

    def save(self, path: Path) -> Path:
        """Pin the (mutated) recording. Saved into a `flight_recordings` directory it
        becomes a suite member, checked in probe mode against the declared invariants."""
        return self._write(path)
