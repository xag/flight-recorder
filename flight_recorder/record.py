"""Recording: transparent instrumentation at a declared boundary.

The cardinal rule is INSTRUMENT, NEVER DUPLICATE. Nothing here evaluates a query, computes a
date, or knows what any value means. Effect wrappers forward to the real function and log
what crossed; the chain proxy forwards every attribute unchanged and logs terminal calls;
the shims delegate to the real datetime/random. The only structural knowledge is names.

Both modes live in the same wrappers, switched by `hook.mode`:
- "record": call the real thing, append an event to the active tool call's buffer.
- "replay": serve the recorded answer from `hook.feed` (see flight_recorder.replay).
- "off": pass straight through.
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
from typing import Any, Callable, Optional

from flight_recorder.boundary import Boundary, ChainTarget
from flight_recorder.serial import short, snapshot_jsonable, to_jsonable

FORMAT_VERSION = 1


# --- shared runtime state -------------------------------------------------------

class _Hook:
    mode: str = "off"   # off | record | replay
    feed: Any = None    # flight_recorder.replay.Feed during replay


hook = _Hook()

_active: ContextVar[Optional[list]] = ContextVar("flight_active", default=None)


def _emit(ev: dict) -> None:
    buf = _active.get()
    if buf is not None:
        buf.append(ev)


# --- effect wrapping (module-level functions, sync or async) ---------------------

def _effect_event(name: str, args: tuple, kwargs: dict) -> dict:
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


def _replay_effect(boundary: Boundary, name: str, args: tuple, kwargs: dict) -> Any:
    from flight_recorder.replay import ReplayDivergence
    ev = hook.feed.pop_expect("fx", fn=name)
    asked = _effect_event(name, args, kwargs)
    if asked["args"] != ev.get("args") or asked["kwargs"] != ev.get("kwargs"):
        raise ReplayDivergence(
            f"effect {name} called with different arguments than recorded:\n"
            f"  recorded: {json.dumps({'args': ev.get('args'), 'kwargs': ev.get('kwargs')}, ensure_ascii=False)[:400]}\n"
            f"  replayed: {json.dumps({'args': asked['args'], 'kwargs': asked['kwargs']}, ensure_ascii=False)[:400]}")
    if "err" in ev:
        raise boundary.revive_error(ev["err"])
    from flight_recorder.serial import from_jsonable
    return from_jsonable(ev.get("res"))


def _wrap_effect(boundary: Boundary, qualname: str, fn: Callable) -> Callable:
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            if hook.mode == "replay":
                return _replay_effect(boundary, qualname, args, kwargs)
            if hook.mode != "record" or _active.get() is None:
                return await fn(*args, **kwargs)
            ev = _effect_event(qualname, args, kwargs)
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
            return _replay_effect(boundary, qualname, args, kwargs)
        if hook.mode != "record" or _active.get() is None:
            return fn(*args, **kwargs)
        ev = _effect_event(qualname, args, kwargs)
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
            return [population[i] for i in ev["idx"]]
        idx = _random.sample(range(len(population)), k)
        if hook.mode == "record":
            _emit({"k": "rand", "m": "sample", "n": len(population), "kk": k, "idx": idx})
        return [population[i] for i in idx]

    def __getattr__(self, name: str) -> Any:
        return getattr(_random, name)


# --- session file -------------------------------------------------------------------

class Recorder:
    def __init__(self, directory: str, boundary: Boundary):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"flight-{stamp}-{os.getpid()}.jsonl"
        self._lock = threading.Lock()
        self._seq = 0
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

    def _write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=repr)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def write_call(self, fn: str, kwargs: dict, events: list, result: Any,
                   error: Optional[str], ms: float) -> None:
        self._seq += 1
        self._write({
            "ev": "call", "seq": self._seq, "fn": fn,
            "kwargs": to_jsonable(kwargs), "events": events,
            "result": to_jsonable(result), "error": error,
            "ts": datetime.now().astimezone().isoformat(), "ms": round(ms, 2),
        })


# --- tool wrapping / install ------------------------------------------------------------

_recorder: Optional[Recorder] = None
_patches: list[tuple[Any, str, Any]] = []  # (module_or_obj, attr, original)


def _finish_call(fn_name: str, call_kwargs: dict, buf: list, result: Any,
                 error: Optional[str], t0: float) -> None:
    if _recorder is not None:
        _recorder.write_call(fn_name, call_kwargs, buf, result, error,
                             (time.perf_counter() - t0) * 1000)


def _wrap_tool(fn: Callable, skip_params: tuple = ("svc",)) -> Callable:
    sig = inspect.signature(fn)

    def _bind(args: tuple, kwargs: dict) -> dict:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return {k: v for k, v in bound.arguments.items() if k not in skip_params}

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            if _recorder is None or _active.get() is not None:
                return await fn(*args, **kwargs)
            buf: list = []
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
                _finish_call(fn.__name__, _bind(args, kwargs), buf, result, error, t0)
        awrapper.__flight_wrapped__ = fn  # type: ignore[attr-defined]
        return awrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _recorder is None or _active.get() is not None:
            return fn(*args, **kwargs)
        buf: list = []
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
            _finish_call(fn.__name__, _bind(args, kwargs), buf, result, error, t0)
    wrapper.__flight_wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


def _patch(target: Any, attr: str, value: Any) -> None:
    _patches.append((target, attr, getattr(target, attr)))
    setattr(target, attr, value)


def patch_boundary(boundary: Boundary) -> None:
    """Wrap the boundary's effects, chains, and shims in place (used by both record and
    replay; behavior switches on hook.mode). Idempotence is the caller's business."""
    for module, names in boundary.effects:
        for name in names:
            fn = getattr(module, name)
            fn = getattr(fn, "__flight_wrapped__", fn)
            _patch(module, name, _wrap_effect(boundary, f"{module.__name__}.{name}", fn))
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


def install(boundary: Boundary, tools_module: Any, directory: str = "flight",
            enabled: bool = True, tool_skip_params: tuple = ("svc",)) -> None:
    """Turn recording on for this process: wrap the boundary, wrap every public function
    defined in `tools_module`, open a session file. No-op when `enabled` is falsy (the app
    decides how that maps to its env)."""
    global _recorder
    if not enabled or _recorder is not None:
        return
    _recorder = Recorder(directory, boundary)
    patch_boundary(boundary)
    for name, fn in vars(tools_module).items():
        if (callable(fn) and not name.startswith("_")
                and getattr(fn, "__module__", "") == tools_module.__name__
                and not inspect.isclass(fn)):
            _patch(tools_module, name, _wrap_tool(fn, tool_skip_params))
    hook.mode = "record"


def uninstall() -> None:
    """Undo install(): restore every patched attribute, drop the session."""
    global _recorder
    unpatch_all()
    hook.mode = "off"
    hook.feed = None
    _recorder = None


def session_path() -> Optional[Path]:
    return _recorder.path if _recorder else None
