"""Replay: re-execute a recorded call with the recording as its world, under full tracing.

Recorded events are fed back in their original order; the code runs under `sys.settrace`,
and every variable change in every traced frame is written to a queryable JSONL trace. Like
the recorder, this duplicates no behavior: playback checks that the replayed code asks the
boundary the same questions in the same order (anything else is a ReplayDivergence naming
the first difference) and hands back the recorded answers. Chain writes are compared, never
executed; a changed effect call is a path divergence.

Each app supplies a small ReplayAdapter: how to resolve the recorded function name into a
callable (building any playback objects it needs), what to prewarm untraced, which
directory's frames to trace, and how to apply header-recorded constants.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import importlib
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from flight_recorder.boundary import Boundary, ChainTarget
from flight_recorder.record import hook, patch_boundary, unpatch_all, _patch
from flight_recorder.serial import (
    from_jsonable, redact_jsonable, render, safe_repr, short, to_jsonable, trace_jsonable,
)


class ReplayDivergence(Exception):
    """The replayed execution asked the boundary a different question than the recording
    holds at this position — the code path itself has diverged."""


class ProbeUnanswerable(BaseException):
    """Under probe replay, the code asked a boundary question the recording holds no
    further answer for — the mutation redirected execution onto a path the tape cannot
    serve. Not a code bug and not a divergence: a limit of this particular recording.

    BaseException on purpose: it is raised inside the replayed application's own frames,
    and probe mode exists precisely for hostile paths — where defensive `except Exception`
    handlers are likeliest. Application code must not be able to swallow it and let a
    trajectory the tape never answered masquerade as a verdict."""


class ReplayedEffectError(Exception):
    """A recorded effect exception whose original type has no registered reviver."""


# --- the feed --------------------------------------------------------------------

_CHAIN_ARGS = re.compile(r"\([^()]*\)")


def _skeleton(sig: Optional[str]) -> str:
    """A chain signature with its argument content erased: `collection(users).where(x,>,0)`
    → `collection.where`. Under mutation the CONTENT of a query legitimately changes (it
    flows from mutated data) but its SHAPE — which methods on which path — does not; probe
    matching compares shapes so one collection's rows can never answer another's query.
    Heuristic: parenthesized argument renderings that themselves contain parentheses may
    survive the erasure; a false mismatch is reported, never a silent crossed wire."""
    return _CHAIN_ARGS.sub("", sig or "")


class Feed:
    """The recording as the world. Strict mode is a contract: each question must be the
    recorded one, in order, exactly. Probe mode (mutated recordings) is only an answering
    service: questions are matched by kind and shape (effect name, chain skeleton),
    order-monotonic, skipping recorded events the mutated execution no longer asks —
    because a mutation legitimately changes which questions get asked, but the tape still
    only holds the answers it holds."""

    def __init__(self, events: list, probe: bool = False):
        self.events = events
        self.pos = 0
        self.probe = probe
        self.skipped = 0
        self.consumed = 0
        self.skip_log: list[str] = []
        self.write_divergences: list[str] = []
        self.writes: list[dict] = []  # every write the REPLAYED code performed

    @staticmethod
    def _want(kind: str, sig: Optional[str], op: Optional[str], fn: Optional[str]) -> str:
        return kind + (f" {op} {sig}" if sig is not None else f" {fn}" if fn is not None
                       else "")

    def _matches(self, ev: dict, kind: str, sig: Optional[str], op: Optional[str],
                 fn: Optional[str]) -> bool:
        if ev["k"] != kind:
            return False
        if kind == "db" and sig is not None:
            if ev.get("op") != op:
                return False
            if self.probe:
                return _skeleton(ev.get("sig")) == _skeleton(sig)
            return ev.get("sig") == sig
        if kind == "fx" and fn is not None:
            return ev.get("fn") == fn
        return True

    def pop_expect(self, kind: str, sig: Optional[str] = None, op: Optional[str] = None,
                   fn: Optional[str] = None) -> dict:
        if self.probe:
            j = self.pos
            while j < len(self.events):
                if self._matches(self.events[j], kind, sig, op, fn):
                    if j > self.pos:
                        self.skipped += j - self.pos
                        self.skip_log.append(
                            f"'{self._want(kind, sig, op, fn)}' answered by event {j}, "
                            f"skipping {j - self.pos} recorded event(s)")
                    self.pos = j + 1
                    self.consumed += 1
                    return self.events[j]
                j += 1
            raise ProbeUnanswerable(
                f"the replayed code asked for '{self._want(kind, sig, op, fn)}' but the "
                f"recording holds no further such event — the mutation sent execution "
                f"down a path this recording cannot answer")
        if self.pos >= len(self.events):
            raise ReplayDivergence(
                f"replay asked for a '{kind}' event at position {self.pos} but the "
                "recording is exhausted — the replayed code takes a longer path than "
                "the recorded one")
        ev = self.events[self.pos]
        if not self._matches(ev, kind, sig, op, fn):
            got = ev["k"] + (f" {ev.get('op')} {ev.get('sig')}" if ev["k"] == "db"
                             else f" {ev.get('fn')}" if ev["k"] == "fx" else "")
            raise ReplayDivergence(
                f"boundary divergence at event {self.pos}:\n"
                f"  recorded: {got}\n  replayed: {self._want(kind, sig, op, fn)}")
        self.pos += 1
        self.consumed += 1
        return ev

    @property
    def remaining(self) -> int:
        return len(self.events) - self.pos


# --- chain playback ----------------------------------------------------------------

class Snap:
    """A recorded document snapshot: exactly the surface a consumer reads."""

    def __init__(self, rec: dict):
        self.id = rec["id"]
        self.exists = rec["exists"]
        self._data = rec["data"]

    def to_dict(self) -> Optional[dict]:
        return from_jsonable(self._data) if self._data is not None else None


def _arg_jsonable(x: Any) -> Any:
    if isinstance(x, PlaybackChain):
        return {"__ref__": object.__getattribute__(x, "_sig")}
    return to_jsonable(x)


class PlaybackChain:
    """Mirror of the recording chain proxy: builds the same signatures, answers terminal
    reads from the feed, turns terminal writes into comparisons."""

    def __init__(self, feed: Feed, target: ChainTarget, sig: str = ""):
        object.__setattr__(self, "_feed", feed)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_sig", sig)

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"<flight-playback {object.__getattribute__(self, '_sig') or 'client'}>"

    def __getattr__(self, name: str) -> Any:
        feed: Feed = object.__getattribute__(self, "_feed")
        target: ChainTarget = object.__getattribute__(self, "_target")
        sig: str = object.__getattribute__(self, "_sig")

        def call(*args: Any, **kwargs: Any) -> Any:
            if name in target.terminal_reads:
                ev = feed.pop_expect("db", sig=sig, op=name)
                res = ev["res"]
                if isinstance(res, list):
                    return [Snap(r) for r in res]
                return Snap(res)
            if name in target.terminal_writes:
                # Scrubbed like the recording was, so a redacted write still compares —
                # and so t.writes never carries a value the tape was forbidden to hold.
                replayed = redact_jsonable([_arg_jsonable(a) for a in args], hook.redact)
                # Every write the replayed code performs is captured for the Trajectory —
                # writes are never executed, but invariants must be able to judge them
                # ("never writes when the corpus is empty").
                feed.writes.append({"op": name, "sig": sig, "args": replayed})
                if feed.probe:
                    # No answer needed from the tape, and comparing against a pre-mutation
                    # recording is meaningless: the capture above is the whole story.
                    return None
                ev = feed.pop_expect("db", sig=sig, op=name)
                if replayed != ev.get("args"):
                    feed.write_divergences.append(
                        f"{name} on {sig or 'client'}:\n"
                        f"    recorded: {json.dumps(ev.get('args'), ensure_ascii=False)[:400]}\n"
                        f"    replayed: {json.dumps(replayed, ensure_ascii=False)[:400]}")
                return None
            seg = f"{name}({', '.join(short(a) for a in args)})"
            return PlaybackChain(feed, target, f"{sig}.{seg}" if sig else seg)

        return call


# --- the tracer ------------------------------------------------------------------------

TRACE_VERSION = 2  # 1: values were reprs. 2: values are data (see serial.trace_jsonable).


class Tracer:
    """sys.settrace-based recorder of the replayed execution's internal state: one JSONL
    event per function call ('C', with args), per line whose execution changed a local
    ('L', with the delta), per return ('R') and per raised exception ('X'), behind a header
    ('H') naming the format version.

    Values are recorded as data, not reprs, so an invariant can do arithmetic on them and
    look inside a document — and so two traces of the same execution are equal, which reprs
    carrying memory addresses never were."""

    def __init__(self, out_path: Path, root: str, skip_files: Optional[set] = None,
                 skip_locals: tuple = ("self", "svc")):
        self.path = out_path
        self.root = root
        self.skip_files = skip_files or set()
        self.skip_locals = skip_locals
        self._f = out_path.open("w", encoding="utf-8")
        self._prev: dict[int, dict] = {}
        self._line: dict[int, int] = {}
        self.transitions = 0
        self._f.write(json.dumps({"e": "H", "trace_version": TRACE_VERSION}) + "\n")

    def start(self) -> None:
        sys.settrace(self._global)

    def stop(self) -> None:
        sys.settrace(None)
        self._f.close()

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, ensure_ascii=False, default=repr) + "\n")
        self.transitions += 1

    def _locals(self, frame: Any) -> dict:
        return {k: trace_jsonable(v) for k, v in frame.f_locals.items()
                if not k.startswith("__") and k not in self.skip_locals}

    @staticmethod
    def _where(frame: Any, lineno: Optional[int] = None) -> str:
        return f"{os.path.basename(frame.f_code.co_filename)}:{lineno or frame.f_lineno}"

    def _global(self, frame: Any, event: str, arg: Any) -> Optional[Callable]:
        fname = frame.f_code.co_filename
        if not fname.startswith(self.root) or os.path.basename(fname) in self.skip_files:
            return None
        snap = self._locals(frame)
        self._write({"e": "C", "fn": frame.f_code.co_qualname,
                     "at": self._where(frame), "args": snap})
        self._prev[id(frame)] = snap
        self._line[id(frame)] = frame.f_lineno
        return self._local

    @staticmethod
    def _same(a: Any, b: Any) -> bool:
        """Change detection over encoded values. Plain != would miss type-changing
        transitions Python calls equal (True == 1 == 1.0); the serialized forms differ."""
        if a == b and type(a) is type(b):
            return True
        try:
            return json.dumps(a, default=repr) == json.dumps(b, default=repr)
        except Exception:
            return False

    def _local(self, frame: Any, event: str, arg: Any) -> Callable:
        fid = id(frame)
        if event == "line":
            cur = self._locals(frame)
            prev = self._prev.get(fid, {})
            delta = {k: v for k, v in cur.items()
                     if k not in prev or not self._same(prev[k], v)}
            if delta:
                self._write({"e": "L", "fn": frame.f_code.co_qualname,
                             "at": self._where(frame, self._line.get(fid)), "d": delta})
            self._prev[fid] = cur
            self._line[fid] = frame.f_lineno
        elif event == "return":
            self._write({"e": "R", "fn": frame.f_code.co_qualname,
                         "at": self._where(frame), "v": trace_jsonable(arg)})
            self._prev.pop(fid, None)
            self._line.pop(fid, None)
        elif event == "exception":
            exc = arg[1]
            self._write({"e": "X", "fn": frame.f_code.co_qualname,
                         "at": self._where(frame), "type": type(exc).__name__,
                         "v": safe_repr(exc)})
        return self._local


# --- adapter + driver ----------------------------------------------------------------------

class ReplayAdapter:
    """Per-app wiring. Subclass (or duck-type) and pass to replay_call/run_cli."""

    boundary: Boundary
    trace_root: str = ""
    skip_files: set = frozenset({"record.py", "replay.py"})

    def prewarm(self) -> None:
        """Load anything heavy (corpora, caches) before tracing starts."""

    def resolve(self, fn_name: str, feed: Feed) -> Callable:
        """Return the callable to re-execute, accepting the recorded kwargs. Build any
        playback objects (e.g. a service holding a PlaybackChain) here."""
        raise NotImplementedError

    def header_patches(self, header: dict) -> list:
        """Extra (module, attr, value) patches from the session header. The generic
        `constants` dict is applied automatically; override for app-specific keys."""
        return []


def _apply_constants(header: dict) -> None:
    for dotted, value in (header.get("constants") or {}).items():
        mod_name, _, attr = dotted.rpartition(".")
        try:
            module = importlib.import_module(mod_name)
        except ImportError:
            continue
        _patch(module, attr, from_jsonable(value))


def load_session(path: Path) -> tuple[dict, list]:
    header: dict = {}
    calls: list = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("ev") == "session":
                header = obj
            elif obj.get("ev") == "call":
                calls.append(obj)
    if not header:
        raise ValueError(f"{path} has no session header — not a flight recording?")
    return header, calls


@dataclass
class ReplayReport:
    fn: str
    result_match: bool
    error_match: bool
    divergence: Optional[str] = None
    result_diff: list = field(default_factory=list)
    write_divergences: list = field(default_factory=list)
    events_consumed: int = 0
    events_total: int = 0
    trace_path: Optional[Path] = None
    transitions: int = 0
    warnings: list = field(default_factory=list)
    # What the replayed code actually produced, jsonable. An invariant asserts on this —
    # not on the recorded result, which is the thing being questioned.
    replayed_result: Any = None
    replayed_error: Optional[str] = None
    replayed_writes: list = field(default_factory=list)  # every write the code performed
    call_kwargs: Any = None  # the recorded call's kwargs, revived
    probe: bool = False
    # Set when a probe replay hit a question the tape can't answer. Distinct from
    # divergence: it impeaches neither code nor recording, only their pairing.
    unanswerable: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (self.result_match and self.error_match and self.divergence is None
                and not self.write_divergences and self.events_consumed == self.events_total)


def replay_call(path: Path, index: int, adapter: ReplayAdapter,
                trace_path: Optional[Path] = None, probe: bool = False) -> ReplayReport:
    """Re-execute one recorded call. `probe=True` replays a MUTATED recording: boundary
    answers come from the (edited) tape matched by name rather than exactly, writes and
    result/error matching don't gate anything, and the verdict belongs to invariants.
    A recording whose call carries `"probe": true` (saved by the mutation API) enables
    probe mode by itself, so a pinned mutated fixture can't be mistaken for a strict one."""
    header, calls = load_session(path)
    if not 0 <= index < len(calls):
        raise ValueError(f"--call {index} out of range: {len(calls)} calls in {path.name}")
    rec = calls[index]
    probe = probe or bool(rec.get("probe"))
    report = ReplayReport(fn=rec["fn"], result_match=False, error_match=False,
                          events_total=len(rec["events"]), probe=probe)

    adapter.prewarm()
    feed = Feed(rec["events"], probe=probe)

    boundary = Boundary(effects=adapter.boundary.effects,
                        clock_modules=adapter.boundary.clock_modules,
                        random_modules=adapter.boundary.random_modules,
                        error_revivers=adapter.boundary.error_revivers,
                        redact=adapter.boundary.redact)
    patch_boundary(boundary)
    # Declared chains whose holder exists now are swapped for playback; chains living on
    # objects the adapter constructs (a fresh service) are its resolve()'s business.
    for target in adapter.boundary.chains:
        if getattr(target.holder, target.attr, None) is not None:
            _patch(target.holder, target.attr, PlaybackChain(feed, target))
    _apply_constants(header)
    for module, attr, value in adapter.header_patches(header):
        _patch(module, attr, value)
    hook.mode, hook.feed = "replay", feed

    # From here to the main try, a failure (an adapter that can't resolve, a trace path in
    # a missing directory) must not leave the process armed: hook in replay mode with this
    # feed and the boundary still patched would poison everything that runs next.
    try:
        fn = adapter.resolve(rec["fn"], feed)
        kwargs = from_jsonable(rec["kwargs"])
        report.call_kwargs = kwargs
        tracer = Tracer(trace_path, adapter.trace_root, set(adapter.skip_files)) \
            if trace_path else None
    except BaseException:
        hook.mode, hook.feed, hook.redact = "off", None, {}
        unpatch_all()
        raise

    result, error = None, None
    try:
        if tracer:
            tracer.start()
        if inspect.iscoroutinefunction(getattr(fn, "__flight_wrapped__", fn)) \
                or inspect.iscoroutinefunction(fn):
            result = asyncio.run(fn(**kwargs))
        else:
            result = fn(**kwargs)
    except ReplayDivergence as e:
        report.divergence = str(e)
    except ProbeUnanswerable as e:
        report.unanswerable = str(e)
    except Exception as e:
        error = repr(e)
    finally:
        if tracer:
            tracer.stop()
            report.trace_path, report.transitions = tracer.path, tracer.transitions
        hook.mode, hook.feed, hook.redact = "off", None, {}
        unpatch_all()

    report.events_consumed = feed.consumed
    report.write_divergences = feed.write_divergences
    report.replayed_writes = feed.writes
    report.replayed_error = error
    report.error_match = error == rec.get("error")
    if probe:
        report.warnings.append(
            f"probe replay: {feed.consumed} event(s) answered, {feed.skipped} skipped, "
            f"{feed.remaining} left over")
        report.warnings.extend(feed.skip_log)
    if not report.error_match:
        report.result_diff = [f"recorded error: {rec.get('error')}",
                              f"replayed error: {error}"]
    if report.divergence is None:
        # Scrubbed like the recorded result was, so a redacted recording still matches.
        # Invariants therefore judge the redacted result — the only one that exists.
        replayed = redact_jsonable(to_jsonable(result), adapter.boundary.redact_rules())
        report.replayed_result = replayed
        report.result_match = replayed == rec["result"]
        if not report.result_match and report.error_match:
            a = json.dumps(rec["result"], ensure_ascii=False, indent=1).splitlines()
            b = json.dumps(replayed, ensure_ascii=False, indent=1).splitlines()
            report.result_diff = list(difflib.unified_diff(
                a, b, "recorded", "replayed", lineterm=""))[:60]
    return report


# --- CLI --------------------------------------------------------------------------------

def _print_call_list(path: Path) -> None:
    header, calls = load_session(path)
    print(f"{path.name} — recorded {header.get('started', '?')}, "
          f"python {header.get('python', '?')}, {len(calls)} call(s)\n")
    for i, c in enumerate(calls):
        err = f"  ERROR {c['error']}" if c.get("error") else ""
        tag = "  PROBE (mutated)" if c.get("probe") else ""
        print(f"  --call {i}: {c['fn']}  ({len(c['events'])} events, "
              f"{c.get('ms', '?')} ms){err}{tag}")
        print(f"           kwargs: {json.dumps(c['kwargs'], ensure_ascii=False)[:90]}")
        print(f"           result: {json.dumps(c['result'], ensure_ascii=False)[:70]}")
    # Orphaned sidecars are calls that never finished — the process died mid-call
    # (crash, OOM, kill). Their events up to the point of death are all we have.
    stem = path.name[:-len(path.suffix)] if path.suffix else path.name
    for sidecar in sorted(path.parent.glob(f"{stem}.call*.inflight")):
        try:
            lines = sidecar.read_text(encoding="utf-8").splitlines()
            hdr = json.loads(lines[0]) if lines else {}
            print(f"  INCOMPLETE (crashed mid-call): {sidecar.name} — "
                  f"{hdr.get('fn', '?')}, {max(len(lines) - 1, 0)} event(s) "
                  f"recorded before death, started {hdr.get('started', '?')}")
        except Exception as e:
            print(f"  INCOMPLETE (unreadable sidecar): {sidecar.name} ({e})")


def _print_watch(trace_path: Path, names: list) -> None:
    print(f"\nTimeline of {', '.join(names)}:")
    with trace_path.open(encoding="utf-8") as f:
        for line in f:
            ev = json.loads(line)
            if ev.get("e") == "H":
                continue
            vals = ev.get("d") or ev.get("args") or {}
            for name in names:
                if name in vals:
                    print(f"  {ev['at']:<34} {ev['fn']:<30} {name} = {render(vals[name])}")


def format_report(rec_index: int, report: ReplayReport) -> str:
    """The human-readable verdict: what matched, and where the first divergence was. Shared
    by the CLI and the pytest plugin, so a failing pinned recording reads the same in CI as
    it does under `--call`."""
    out: list[str] = [f"  warning: {w}" for w in report.warnings]
    if report.probe:
        verdict = "PROBE — a mutated recording is judged by invariants, not by match"
        if report.unanswerable:
            verdict = f"UNANSWERABLE\n  {report.unanswerable}"
    else:
        verdict = "MATCH — replay reproduced the recording bit-for-bit" if report.ok \
            else "DIVERGED"
    out.append(f"Replayed {report.fn} (call {rec_index}): {verdict}")
    out.append(f"  boundary events: {report.events_consumed}/{report.events_total} consumed"
               + ("" if report.events_consumed == report.events_total or report.probe
                  else "  <-- replayed code took a SHORTER path than recorded"))
    if report.divergence:
        out.append(f"  {report.divergence}")
    if report.result_diff:
        out.extend("  " + l for l in report.result_diff)
    if report.write_divergences:
        out.append(f"  write divergences ({len(report.write_divergences)}):")
        out.extend(f"    {d}" for d in report.write_divergences)
    if report.trace_path:
        out.append(f"  trace: {report.trace_path} ({report.transitions} state transitions)")
    return "\n".join(out)


def _print_report(rec_index: int, report: ReplayReport) -> None:
    print(format_report(rec_index, report))


def run_cli(adapter: ReplayAdapter, argv: Optional[list] = None,
            prog: str = "replay") -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Replay a flight-recorded call with full internal-state tracing.")
    ap.add_argument("session", type=Path, help="flight session .jsonl file")
    ap.add_argument("--call", type=int, default=None,
                    help="index of the call to replay (omit to list calls)")
    ap.add_argument("--trace", type=Path, default=None,
                    help="trace output path (default: <session>.call<N>.trace.jsonl)")
    ap.add_argument("--no-trace", action="store_true", help="replay without tracing")
    ap.add_argument("--watch", default="",
                    help="comma-separated variable names to print a timeline for")
    args = ap.parse_args(argv)

    if not args.session.exists():
        print(f"No such file: {args.session}", file=sys.stderr)
        return 1
    if args.call is None:
        _print_call_list(args.session)
        return 0

    trace_path = None
    if not args.no_trace:
        trace_path = args.trace or args.session.with_suffix(f".call{args.call}.trace.jsonl")
    report = replay_call(args.session, args.call, adapter, trace_path)
    _print_report(args.call, report)
    if trace_path and args.watch:
        _print_watch(trace_path, [w.strip() for w in args.watch.split(",") if w.strip()])
    if report.probe:
        # A probe fixture has no match to exit on; the CLI's verdict is only whether the
        # tape could answer the path at all. Invariants judge content, elsewhere.
        return 0 if report.divergence is None and report.unanswerable is None else 2
    return 0 if report.ok else 2
