"""Recording: transparent instrumentation at a declared boundary.

The cardinal rule is INSTRUMENT, NEVER DUPLICATE. Nothing here evaluates a query, computes a
date, or knows what any value means. Effect wrappers forward to the real function and log
what crossed; the chain proxy forwards every attribute unchanged and logs terminal calls;
the shims delegate to the real datetime/random. The only structural knowledge is names.

Both modes live in the same wrappers, switched by `hook.mode`:
- "record": call the real thing, append an event to the active tool call's buffer.
- "replay": serve the recorded answer from `hook.feed` (see flight_recorder.replay).
- "off": pass straight through.

Whether a given call is recorded at all is a separate question from `hook.mode`, and it is
answered per call by the gate (`install(enabled=...)`): a bool decides once for the process,
a callable decides afresh for every tool call.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import random as _random
import sys
import threading
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Union

from flight_recorder.boundary import Boundary, ChainTarget
from flight_recorder.serial import (
    forbidden_hit, redact_jsonable, short, snapshot_jsonable, to_jsonable,
)

FORMAT_VERSION = 1


class ForbiddenValue(Exception):
    """A `Boundary.forbid` pattern matched the record the recorder was about to write.

    Raised at record time, before any bytes reach the file, the sidecar or the sink — so the
    credential does not land, anywhere, ever. This is the one failure in the recorder that is
    deliberately NOT best-effort: everywhere else the direction is "the recording is a bit
    poorer, the app survives", because a recorder must not break the app it observes. Here it
    inverts. A tape being written with a live credential on it is not a poorer recording, it
    is an exfiltration path, and the app is already in the state you swore it would never be
    in. Failing the call is the quiet option.

    The message names the RULE and never the match: it ends up in logs and stack traces, and
    a tripwire that quotes the secret it caught has become the leak it was there to prevent.
    """


def _guard(line: str, patterns: list, what: str) -> None:
    """Refuse to write `line` if a forbid pattern matches it. A no-op — and free — for the
    boundary that declares no tripwire, which is every boundary that existed before this."""
    if not patterns:
        return
    hit = forbidden_hit(line, patterns)
    if hit is not None:
        raise ForbiddenValue(
            f"{what} matches a forbidden pattern ({hit!r}) after redaction — nothing was "
            f"written. A value that must never reach a tape was about to: name the field in "
            f"Boundary.redact, or widen a rule that has stopped matching, and record again.")


class SessionSink(Protocol):
    """Where a session goes besides the local disk, so recordings are retrievable without
    filesystem access to the box that made them (object storage, an artifact store...).

    `publish` is handed the session file's name and its full current bytes after the header
    is written and after every completed call.

    It is called synchronously, holding the recorder's write lock, on whatever thread
    finished the call — in an async server, that is the event-loop thread. A `publish` that
    blocks on network I/O therefore stalls *every* concurrent request, not just the recorded
    one. Hand the bytes to a queue or a thread and return.

    Raising `Exception` is ignored: like the crash sidecars, publication is best-effort and
    will not break the call being recorded. `BaseException` (KeyboardInterrupt, SystemExit)
    is deliberately left to propagate, as everywhere else.

    Only completed calls reach a sink. A call that dies mid-flight leaves its events in a
    local `.inflight` sidecar, which is not published — a crashed call's last words are
    readable only on the box.
    """

    def publish(self, name: str, data: bytes) -> None: ...


# The gate: `False`/`True` decide once at install; a callable is consulted per tool call,
# with the tool's name and its bound kwargs (minus `tool_skip_params`).
Gate = Union[bool, Callable[[str, dict], bool]]


# --- shared runtime state -------------------------------------------------------

class _Hook:
    mode: str = "off"   # off | record | replay
    feed: Any = None    # flight_recorder.replay.Feed during replay
    redact: dict = {}   # the active boundary's normalized redact rules
    scrub: Any = None   # the active boundary's value sweep (str -> str), or None
    # Where the REPLAYED code's own note()/span() calls land. A recorded sem event is never
    # fed back — it is testimony, and replay serves evidence — so the replayed code makes its
    # claims afresh and they are captured here, to be compared with the recorded ones.
    sems: Any = None    # a _SemCapture during replay, None otherwise


hook = _Hook()


class _SemCapture(list):
    """The replayed execution's semantic events, in order. Its sids are its own: they name
    spans within this replay, and nothing pairs them against the recording's."""

    def __init__(self) -> None:
        super().__init__()
        self._sid = 0

    def next_sid(self) -> int:
        self._sid += 1
        return self._sid

_active: ContextVar[Optional[list]] = ContextVar("flight_active", default=None)
# True for the duration of any top-level tool call, recorded or not. `_active` cannot serve:
# it is unset for a call the gate declined, which would let a nested tool be gated afresh and
# recorded as a fragmentary top-level call of its own.
_in_call: ContextVar[bool] = ContextVar("flight_in_call", default=False)


# Redaction rewrites event payloads only, never the envelope (k/fn/op/sig/v/idx/name/phase/
# sid), so a rule named like an envelope key cannot corrupt the event structure. `err` is
# scrubbed whole: its `args` carry the exception's values, and a `"repr"` rule lets an app mask
# the recorded repr too. `data` is a semantic event's payload and is scrubbed like any other:
# testimony is written by the app, about the app's own values, and is exactly as likely to
# carry a credential as an effect's arguments are.
_PAYLOAD_KEYS = ("args", "kwargs", "res", "err", "data")


def _scrub_event(ev: dict) -> dict:
    rules, sweep = hook.redact, hook.scrub
    if rules or sweep is not None:
        for key in _PAYLOAD_KEYS:
            if key in ev:
                ev[key] = redact_jsonable(ev[key], rules, sweep)
    return ev


def _emit(ev: dict) -> None:
    buf = _active.get()
    if buf is not None:
        buf.append(_scrub_event(ev))


# --- effect wrapping (module-level functions, sync or async) ---------------------

def _effect_event(name: str, args: tuple, kwargs: dict, opts: dict) -> dict:
    if opts.get("method"):
        args = args[1:]  # self is identity, not input
    return {"k": "fx", "fn": name,
            "args": [to_jsonable(a) for a in args],
            "kwargs": {k: to_jsonable(v) for k, v in kwargs.items()}}


def _record_result(ev: dict, res: Any = None, err: Optional[BaseException] = None) -> None:
    if err is not None:
        ev["err"] = {"type": type(err).__name__, "repr": repr(err)[:300],
                     "args": to_jsonable(list(getattr(err, "args", []) or []))}
    else:
        ev["res"] = to_jsonable(res)
    _emit(ev)


def _replay_effect(boundary: Boundary, name: str, args: tuple, kwargs: dict,
                   opts: dict) -> Any:
    from flight_recorder.replay import ReplayDivergence
    ev = hook.feed.pop_expect("fx", fn=name)
    # Scrubbed like the recording was, so a redacted recording still compares: a replayed
    # value that is already a mask (it came off the tape) passes through unchanged —
    # which is why redact transforms must be idempotent.
    asked = _scrub_event(_effect_event(name, args, kwargs, opts))
    # Probe replay never compares args: a mutated upstream answer legitimately changes
    # every downstream question. The event name and order still gate.
    if not opts.get("loose_args") and not hook.feed.probe and (
            asked["args"] != ev.get("args") or asked["kwargs"] != ev.get("kwargs")):
        raise ReplayDivergence(
            f"effect {name} called with different arguments than recorded:\n"
            f"  recorded: {json.dumps({'args': ev.get('args'), 'kwargs': ev.get('kwargs')}, ensure_ascii=False)[:400]}\n"
            f"  replayed: {json.dumps({'args': asked['args'], 'kwargs': asked['kwargs']}, ensure_ascii=False)[:400]}")
    if "err" in ev:
        raise boundary.revive_error(ev["err"])
    from flight_recorder.serial import from_jsonable
    return from_jsonable(ev.get("res"))


def _wrap_effect(boundary: Boundary, qualname: str, fn: Callable,
                 opts: Optional[dict] = None) -> Callable:
    """opts: {"method": True} — the wrapped callable is a class function; args[0] (self)
    is identity, not input, and is skipped in recording and replay comparison.
    {"loose_args": True} — record args for inspection but don't compare them on replay
    (for effects whose args are machine-dependent, e.g. filesystem paths); the event
    order and effect name still gate the replay."""
    opts = opts or {}
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            if hook.mode == "replay":
                return _replay_effect(boundary, qualname, args, kwargs, opts)
            if hook.mode != "record" or _active.get() is None:
                return await fn(*args, **kwargs)
            ev = _effect_event(qualname, args, kwargs, opts)
            try:
                res = await fn(*args, **kwargs)
            except BaseException as e:
                _record_result(ev, err=e)
                raise
            _record_result(ev, res=res)
            return res
        awrapper.__flight_wrapped__ = fn  # type: ignore[attr-defined]
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if hook.mode == "replay":
            return _replay_effect(boundary, qualname, args, kwargs, opts)
        if hook.mode != "record" or _active.get() is None:
            return fn(*args, **kwargs)
        ev = _effect_event(qualname, args, kwargs, opts)
        try:
            res = fn(*args, **kwargs)
        except BaseException as e:
            _record_result(ev, err=e)
            raise
        _record_result(ev, res=res)
        return res
    wrapper.__flight_wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


# --- chain proxy (chained clients: query builders, document refs, batches) -------

def _unwrap(x: Any) -> Any:
    return object.__getattribute__(x, "_real") if isinstance(x, ChainNode) else x


def _arg_jsonable(x: Any) -> Any:
    if isinstance(x, ChainNode):
        return {"__ref__": object.__getattribute__(x, "_sig")}
    return to_jsonable(x)


class ChainNode:
    """Transparent proxy over any node of a chained client's object graph. Forwards
    everything to the real object; logs terminal reads/writes."""

    def __init__(self, real: Any, sig: str, target: ChainTarget):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_sig", sig)
        object.__setattr__(self, "_target", target)

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_real"))

    def __repr__(self) -> str:
        return f"<flight-recorded {object.__getattribute__(self, '_sig') or 'client'}>"

    def __getattr__(self, name: str) -> Any:
        real = object.__getattribute__(self, "_real")
        sig = object.__getattribute__(self, "_sig")
        target: ChainTarget = object.__getattribute__(self, "_target")
        attr = getattr(real, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            raw_args = [_unwrap(a) for a in args]
            if name in target.terminal_reads:
                res = attr(*raw_args, **kwargs)
                if hasattr(res, "to_dict"):  # a single document snapshot
                    _emit({"k": "db", "op": name, "sig": sig,
                           "res": snapshot_jsonable(res)})
                    return res
                docs = list(res)
                _emit({"k": "db", "op": name, "sig": sig,
                       "res": [snapshot_jsonable(s) for s in docs]})
                return docs
            if name in target.terminal_writes:
                res = attr(*raw_args, **kwargs)
                _emit({"k": "db", "op": name, "sig": sig,
                       "args": [_arg_jsonable(a) for a in args]})
                return res
            res = attr(*raw_args, **kwargs)
            seg = f"{name}({', '.join(short(a) for a in args)})"
            return ChainNode(res, f"{sig}.{seg}" if sig else seg, target)

        return call


# --- clock / random shims ---------------------------------------------------------

class _DatetimeShimMeta(type):
    def __instancecheck__(cls, inst: Any) -> bool:
        return isinstance(inst, datetime)

    def __getattr__(cls, name: str) -> Any:
        return getattr(datetime, name)


class DatetimeShim(metaclass=_DatetimeShimMeta):
    """Stands in for the `datetime` class inside boundary modules. Everything delegates to
    the real datetime except now(), which is recorded / replayed."""

    def __new__(cls, *args: Any, **kwargs: Any) -> datetime:
        return datetime(*args, **kwargs)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if hook.mode == "replay":
            ev = hook.feed.pop_expect("now")
            return datetime.fromisoformat(ev["v"])
        v = datetime.now(tz)
        if hook.mode == "record":
            _emit({"k": "now", "v": v.isoformat()})
        return v


class RandomShim:
    """Stands in for the `random` module inside boundary modules. sample() draws positions
    via the real RNG and records them, so replay picks the same members without re-rolling."""

    def sample(self, population: Any, k: int) -> list:
        population = list(population)
        if hook.mode == "replay":
            ev = hook.feed.pop_expect("rand")
            if hook.feed.probe and any(not 0 <= i < len(population) for i in ev["idx"]):
                from flight_recorder.replay import ProbeUnanswerable
                raise ProbeUnanswerable(
                    f"the recorded random draw {ev['idx']} does not fit the mutated "
                    f"population of {len(population)} — set the rand event's idx too "
                    f"(call.rand().idx = [...])")
            return [population[i] for i in ev["idx"]]
        idx = _random.sample(range(len(population)), k)
        if hook.mode == "record":
            _emit({"k": "rand", "m": "sample", "n": len(population), "kk": k, "idx": idx})
        return [population[i] for i in idx]

    def __getattr__(self, name: str) -> Any:
        return getattr(_random, name)


# --- semantic events: the app's testimony, next to the evidence ----------------------
#
# A tape records what the world answered. It says nothing about what the execution MEANT —
# and meaning is what a reader is actually looking for. So an app may declare, in its own
# free-text vocabulary, that a stretch of execution constituted a domain-level act, and have
# that declaration recorded in-stream, interleaved with the raw events it encloses.
#
# The cardinal rule is untouched: INSTRUMENT, NEVER DUPLICATE. The library gains no semantics.
# Names and payloads are opaque here; nothing validates them, nothing interprets them, nothing
# checks that a span called "charge_card" charged anything. A semantic event is the app's
# *testimony* about its own execution, written down next to the *evidence* — the raw boundary
# events inside the span. This library records both and judges neither. Whether the testimony
# is licensed by the evidence is a question for a reader, and it is a question that only has
# teeth because both are on the same tape, in order.
#
# Order is the meaning: enclosure is derived from the sequence of begin/end events, so there
# are no parent pointers to get wrong. Well-nesting is guaranteed by construction, because the
# only way to open a span is a context manager.
#
# Spans are CALL-SCOPED. A span never crosses a call boundary, and session-level meaning
# ("this user's whole conversation") is a reader's composition, not a recorder's.

def _sem(name: str, phase: str, data: dict, sid: Optional[int] = None,
         outcome: Optional[str] = None) -> Optional[int]:
    """Emit one semantic event, or nothing at all. Returns the sid, or None if nothing was
    written — which is the ordinary case in production, where the recorder is off.

    Under replay the same calls fire again, from the same code, and are CAPTURED rather than
    written: the recorded sems are never fed back (they were never answers), so what the
    replayed code claims this time is a fresh statement, and a reader can compare the two."""
    if hook.mode == "record":
        sink = _active.get()
    elif hook.mode == "replay":
        sink = hook.sems
    else:
        return None
    if sink is None:
        return None

    if sid is None:
        sid = sink.next_sid()
    ev = {"k": "sem", "name": name, "phase": phase, "sid": sid}
    if outcome is not None:
        ev["outcome"] = outcome
    if data:
        ev["data"] = {k: to_jsonable(v) for k, v in data.items()}

    if hook.mode == "record":
        # Through _emit, so `data` meets `redact` (it is in _PAYLOAD_KEYS) and then the
        # `forbid` tripwire in _CallSink.append. Testimony gets exactly the same treatment as
        # evidence: an app that names a password in a span's data has leaked it precisely as
        # hard as one that passed it to an effect.
        _emit(ev)
    else:
        # Scrubbed like the recording was: a replayed sem ends up in a report that gets
        # printed, and a value the tape was forbidden to hold must not reach a terminal either.
        sink.append(_scrub_event(ev))
    return sid


def note(name: str, **data: Any) -> None:
    """Record that something meaningful just happened, at a point: `note("turn_skipped",
    reason="absent")`.

    A strict no-op when no recording is active for this call — not installed, or the gate said
    no. That is not an optimisation, it is the contract: this call sits in production code
    paths, so it must cost nothing and it must have no failure modes when off. Nothing here can
    raise except the forbidden-value tripwire, which is supposed to."""
    _sem(name, "point", data)


class span:
    """Record that a stretch of execution constituted a domain act, and enclose the raw events
    it produced:

        with fr.span("assign_turn", chore=chore_id):
            ...                          # every boundary event in here is inside the span
        async with fr.span("assign_turn", chore=chore_id):
            ...                          # same object, awaited

    A reader can then load the span tree first and descend into raw JSONL only inside the span
    that looks wrong — which is the difference between reading a tape and searching one.

    If the body raises, the `end` event is still written, carrying `outcome: "error"`, and the
    exception propagates untouched. A span that vanishes from the tape when the code inside it
    failed would hide precisely the execution somebody came to the tape to read.

    A no-op when recording is off, in which case the `with` block is an ordinary block."""

    __slots__ = ("_name", "_data", "_sid")

    def __init__(self, name: str, **data: Any):
        self._name = name
        self._data = data
        self._sid: Optional[int] = None

    def _begin(self) -> "span":
        self._sid = _sem(self._name, "begin", self._data)
        return self

    def _end(self, exc_type: Optional[type]) -> None:
        # No sid means the begin was never recorded (recording was off, or the gate declined),
        # so there is nothing to close. A lone `end` on a tape would be a lie about nesting.
        if self._sid is not None:
            _sem(self._name, "end", {}, sid=self._sid,
                 outcome="error" if exc_type is not None else "ok")

    def __enter__(self) -> "span":
        return self._begin()

    def __exit__(self, exc_type: Optional[type], exc: Any, tb: Any) -> bool:
        self._end(exc_type)
        return False  # never swallow: the recorder observes, it does not intervene

    async def __aenter__(self) -> "span":
        return self._begin()

    async def __aexit__(self, exc_type: Optional[type], exc: Any, tb: Any) -> bool:
        self._end(exc_type)
        return False


# --- session file -------------------------------------------------------------------

class _CallSink(list):
    """The active call's event buffer, mirrored line-by-line to an `.inflight` sidecar so
    a hard-killed call (SIGKILL, OOM) still leaves its events on disk. A normal call end
    folds the events into the session record and removes the sidecar; an orphaned sidecar
    IS the crashed call's partial record (the CLI lists them as INCOMPLETE). Mirroring
    failures never break the call — the sidecar is best-effort by design."""

    def __init__(self, path: Path, header: dict, forbid: Optional[list] = None):
        super().__init__()
        self._path = path
        self._forbid = forbid or []
        self._sid = 0
        line = json.dumps(header, ensure_ascii=False, default=repr)
        # Ahead of the open(), and outside the best-effort try below: a sidecar is a file on
        # disk like any other, and a forbidden value must not reach it either. The guard is
        # the one thing here that is allowed to raise.
        _guard(line, self._forbid, f"the opening record of call {header.get('fn')!r}")
        try:
            self._f = path.open("w", encoding="utf-8")
            self._f.write(line + "\n")
            self._f.flush()
        except Exception:
            self._f = None

    def append(self, ev: dict) -> None:
        line = json.dumps(ev, ensure_ascii=False, default=repr)
        # Before the buffer, not just before the file: the buffer becomes the call record.
        # This is the earliest the tape can know, and it names the event that carried it.
        _guard(line, self._forbid, f"a recorded {ev.get('k')!r} event")
        super().append(ev)
        if self._f is not None:
            try:
                self._f.write(line + "\n")
                self._f.flush()
            except Exception:
                pass

    def next_sid(self) -> int:
        """The next span id. Unique within the call, which is all a `sid` ever has to be:
        enclosure is derived from the ORDER of the begin/end events, not from a parent
        pointer, so an id only has to tell one span's end from another's."""
        self._sid += 1
        return self._sid

    def finalize(self) -> None:
        try:
            if self._f is not None:
                self._f.close()
            self._path.unlink(missing_ok=True)
        except Exception:
            pass


class Recorder:
    def __init__(self, directory: str, boundary: Boundary,
                 sink: Optional[SessionSink] = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"flight-{stamp}-{os.getpid()}.jsonl"
        self.sink = sink
        self._redact = boundary.redact_rules()
        self._scrub = boundary.scrub
        self._forbid = boundary.forbid_patterns()
        self._lock = threading.Lock()
        self._bytes = bytearray()  # mirror of the file, kept only to feed a sink
        self._seq = 0
        self._inflight = 0
        header = {
            "ev": "session", "version": FORMAT_VERSION,
            "started": datetime.now().astimezone().isoformat(),
            "python": sys.version.split()[0],
            "constants": {f"{m.__name__}.{n}": to_jsonable(getattr(m, n))
                          for m, n in boundary.constants},
        }
        for key, get in boundary.header_extras.items():
            header[key] = get()
        self._write(header)

    def _publish(self) -> None:
        """Best-effort, under the write lock so a sink never observes a torn line and never
        sees an older session than one it already saw. Fed from the in-memory mirror rather
        than re-reading the file, so publishing stays O(1) per call rather than O(N²) over
        a session."""
        if self.sink is None:
            return
        try:
            self.sink.publish(self.path.name, bytes(self._bytes))
        except Exception:
            pass

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=repr)
        # The last gate, and the widest: the header's constants and extras, a positional
        # effect argument, an opaque repr, a chain signature — everything that reaches the
        # tape reaches it as this line, including what `redact` structurally cannot see.
        _guard(line, self._forbid, f"the {obj.get('ev')!r} record")
        data = (line + "\n").encode("utf-8")
        with self._lock:
            with self.path.open("ab") as f:  # bytes, so the file and the mirror agree
                f.write(data)
            if self.sink is not None:
                self._bytes += data
                self._publish()

    def start_call(self, fn: str, kwargs: dict) -> _CallSink:
        """Open the call's sidecar-mirrored event buffer (crash capture starts here)."""
        with self._lock:
            self._inflight += 1
            n = self._inflight
        return _CallSink(
            self.dir / f"{self.path.stem}.call{n}.inflight",
            {"ev": "inflight", "fn": fn,
             "kwargs": redact_jsonable(to_jsonable(kwargs), self._redact, self._scrub),
             "started": datetime.now().astimezone().isoformat()},
            self._forbid)

    def write_call(self, fn: str, kwargs: dict, events: list, result: Any,
                   error: Optional[str], ms: float) -> None:
        self._seq += 1
        self._write({
            "ev": "call", "seq": self._seq, "fn": fn,
            "kwargs": redact_jsonable(to_jsonable(kwargs), self._redact, self._scrub),
            "events": events,
            "result": redact_jsonable(to_jsonable(result), self._redact, self._scrub),
            "error": error,
            "ts": datetime.now().astimezone().isoformat(), "ms": round(ms, 2),
        })


# --- tool wrapping / install ------------------------------------------------------------

_recorder: Optional[Recorder] = None
_patches: list[tuple[Any, str, Any]] = []  # (module_or_obj, attr, original)
# Set when installed with a callable gate: what a Recorder would need, held until the gate
# first says yes. A gate that never fires must leave no session file behind.
_pending: Optional[tuple[str, Boundary, Optional[SessionSink]]] = None
_gate: Optional[Callable[[str, dict], bool]] = None
_recorder_lock = threading.Lock()


def _ensure_recorder() -> Optional[Recorder]:
    """The session file is opened by the first call the gate admits, not by install()."""
    global _recorder
    if _recorder is not None or _pending is None:
        return _recorder
    with _recorder_lock:
        if _recorder is None and _pending is not None:
            directory, boundary, sink = _pending
            _recorder = Recorder(directory, boundary, sink)
    return _recorder


def _should_record(fn_name: str, call_kwargs: dict) -> bool:
    if _gate is None:
        return _recorder is not None
    try:
        return bool(_gate(fn_name, call_kwargs))
    except Exception:
        return False  # a broken gate must never break the call it was asked about


def _finish_call(fn_name: str, call_kwargs: dict, buf: list, result: Any,
                 error: Optional[str], t0: float) -> None:
    if _recorder is not None:
        _recorder.write_call(fn_name, call_kwargs, list(buf), result, error,
                             (time.perf_counter() - t0) * 1000)
    if isinstance(buf, _CallSink):
        buf.finalize()


def _wrap_tool(fn: Callable, skip_params: tuple = ("svc",),
               tool_name: Optional[str] = None) -> Callable:
    """`tool_name` is the name the gate is asked about and the recording stores. It defaults
    to the Python function's name, which is the tool's name whenever tools are plain module
    functions; a registry that renames them (install_mcp) must pass the registered name."""
    sig = inspect.signature(fn)
    name = tool_name or fn.__name__

    def _bind(args: tuple, kwargs: dict) -> dict:
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            return {k: v for k, v in bound.arguments.items() if k not in skip_params}
        except TypeError:  # the call itself will raise the real error
            return {}

    def _decide(args: tuple, kwargs: dict) -> tuple[Optional[dict], Optional[_CallSink]]:
        """Gate this top-level call, and if it is admitted open its event buffer."""
        call_kwargs = _bind(args, kwargs)
        if not _should_record(name, call_kwargs):
            return None, None
        recorder = _ensure_recorder()
        if recorder is None:  # uninstalled between the gate and here
            return None, None
        return call_kwargs, recorder.start_call(name, call_kwargs)

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            if _in_call.get():  # nested: the outermost tool call already decided
                return await fn(*args, **kwargs)
            call_kwargs, buf = _decide(args, kwargs)
            depth = _in_call.set(True)
            try:
                if buf is None:
                    return await fn(*args, **kwargs)
                token = _active.set(buf)
                t0 = time.perf_counter()
                result, error = None, None
                try:
                    result = await fn(*args, **kwargs)
                    return result
                except Exception as e:
                    error = repr(e)
                    raise
                finally:
                    _active.reset(token)
                    _finish_call(name, call_kwargs, buf, result, error, t0)
            finally:
                _in_call.reset(depth)
        awrapper.__flight_wrapped__ = fn  # type: ignore[attr-defined]
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _in_call.get():  # nested: the outermost tool call already decided
            return fn(*args, **kwargs)
        call_kwargs, buf = _decide(args, kwargs)
        depth = _in_call.set(True)
        try:
            if buf is None:
                return fn(*args, **kwargs)
            token = _active.set(buf)
            t0 = time.perf_counter()
            result, error = None, None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = repr(e)
                raise
            finally:
                _active.reset(token)
                _finish_call(name, call_kwargs, buf, result, error, t0)
        finally:
            _in_call.reset(depth)
    wrapper.__flight_wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


def _patch(target: Any, attr: str, value: Any) -> None:
    _patches.append((target, attr, getattr(target, attr)))
    setattr(target, attr, value)


def patch_boundary(boundary: Boundary) -> None:
    """Wrap the boundary's effects, chains, and shims in place (used by both record and
    replay; behavior switches on hook.mode). Idempotence is the caller's business."""
    hook.redact = boundary.redact_rules()
    hook.scrub = boundary.scrub
    for entry in boundary.effects:
        module, names = entry[0], entry[1]
        opts = entry[2] if len(entry) > 2 else None
        for name in names:
            fn = getattr(module, name)
            fn = getattr(fn, "__flight_wrapped__", fn)
            _patch(module, name,
                   _wrap_effect(boundary, f"{module.__name__}.{name}", fn, opts))
    for target in boundary.chains:
        real = getattr(target.holder, target.attr, None)
        if real is not None:
            _patch(target.holder, target.attr, ChainNode(real, "", target))
    for module in boundary.clock_modules:
        _patch(module, "datetime", DatetimeShim)
    for module in boundary.random_modules:
        _patch(module, "random", RandomShim())


def unpatch_all() -> None:
    while _patches:
        target, attr, orig = _patches.pop()
        setattr(target, attr, orig)


def _arm(directory: str, boundary: Boundary, enabled: Gate,
         sink: Optional[SessionSink]) -> bool:
    """Shared install prologue. Returns False if there is nothing to do — either recording
    is statically off, or an install is already live (both installs are idempotent).

    A static `True` opens the session file now, as it always has. A callable defers it: the
    wrappers go in, but nothing is written until the gate first admits a call.

    An install that fails partway (an unwritable directory, a bad boundary) leaves nothing
    behind: the armed state is rolled back so a retry is a fresh install rather than a
    silent no-op against the wreck of the first attempt."""
    global _recorder, _pending, _gate
    if _recorder is not None or _pending is not None:
        return False
    if not callable(enabled) and not enabled:
        return False
    _pending = (directory, boundary, sink)
    _gate = enabled if callable(enabled) else None
    try:
        if _gate is None:
            _ensure_recorder()
        patch_boundary(boundary)
    except BaseException:
        unpatch_all()
        hook.redact, hook.scrub = {}, None
        _recorder = _pending = _gate = None
        raise
    return True


def install(boundary: Boundary, tools_module: Any, directory: str = "flight",
            enabled: Gate = True, tool_skip_params: tuple = ("svc",),
            sink: Optional[SessionSink] = None) -> None:
    """Turn recording on for this process: wrap the boundary, wrap every public function
    defined in `tools_module`, open a session file.

    `enabled` is the gate. A bool decides once, for the whole process — falsy is a complete
    no-op, nothing is patched. A callable `(tool_name, kwargs) -> bool` is instead consulted
    on every tool call, so a single running server can record one user's request, or one
    tool, and leave the rest of its traffic untouched; the session file is created by the
    first call the gate admits, so a gate that never fires leaves no file at all.

    `sink` optionally publishes the session off-box as it grows (see SessionSink).
    """
    if not _arm(directory, boundary, enabled, sink):
        return
    for name, fn in vars(tools_module).items():
        if (callable(fn) and not name.startswith("_")
                and getattr(fn, "__module__", "") == tools_module.__name__
                and not inspect.isclass(fn)):
            _patch(tools_module, name, _wrap_tool(fn, tool_skip_params))
    hook.mode = "record"


def install_mcp(boundary: Boundary, mcp_server: Any, directory: str = "flight",
                enabled: Gate = True, tool_skip_params: tuple = (),
                sink: Optional[SessionSink] = None) -> None:
    """Like install(), but for apps whose tool bodies don't live in one module (e.g. tools
    registered from libraries): wraps every tool already registered on the FastMCP server,
    at the registry (`Tool.fn`). Register all tools before calling this."""
    if not _arm(directory, boundary, enabled, sink):
        return
    for registered, tool in mcp_server._tool_manager._tools.items():
        # The gate is asked about the name clients call, which a registry may alias away
        # from the Python function's own (`@mcp.tool(name="search_code") def _do_search`).
        _patch(tool, "fn", _wrap_tool(tool.fn, tool_skip_params,
                                      tool_name=getattr(tool, "name", None) or registered))
    hook.mode = "record"


def uninstall() -> None:
    """Undo install(): restore every patched attribute, drop the session and the gate."""
    global _recorder, _pending, _gate
    unpatch_all()
    hook.mode = "off"
    hook.feed = None
    hook.redact, hook.scrub = {}, None
    hook.sems = None
    _recorder = None
    _pending = None
    _gate = None


def session_path() -> Optional[Path]:
    """The session file, or None if none exists yet — which under a callable gate means no
    call has been admitted, not that recording is off."""
    return _recorder.path if _recorder else None
