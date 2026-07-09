"""Invariants: assertions over a replayed execution's trajectory.

A pinned recording is a **regression** oracle. It asserts that the code behaves as it
behaved when the recording was pinned, and it can never say the recorded behavior was
right — a bug records as faithfully as a fix.

An invariant is a **correctness** oracle. It is a claim about every execution, written once
and checked against any recording, so it can condemn the very first observation of a bug —
which no recording can. It sees more than the output: the trace makes internal claims
checkable, so a property like "level never leaves the corpus empty" is a lookup rather than
an inference.

    @fr.invariant("never claims end-of-corpus while words remain")
    def _(t: fr.Trajectory):
        assert not (t.result["done"] and t.result["corpus"] - t.result["deck"] > 0)

    @fr.invariant("level never excludes the whole corpus")
    def _(t: fr.Trajectory):
        for obs in t.trace.values("level"):
            assert obs.value > 0, f"level={obs.value} at {obs.at}"

    report = fr.check_invariants(session, 0, Adapter(), INVARIANTS)
    assert report.ok, fr.format_invariant_report(report)

The failure of an invariant is a claim about the code, not about the recording. The failure
of a *replay* is a claim about the recording. They are different findings and this module
keeps them apart: a recording whose replay diverged has no trustworthy trajectory to assert
over, so its invariants are not run and not reported as held.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from flight_recorder.replay import ReplayAdapter, ReplayReport, replay_call
from flight_recorder.serial import from_jsonable, from_trace_jsonable, render


# --- observations over a trace ---------------------------------------------------------

@dataclass(frozen=True)
class Obs:
    """One sighting of a named variable, at the line whose execution produced it."""
    at: str      # "file.py:lineno"
    fn: str      # qualified name of the frame's function
    name: str
    value: Any

    def __repr__(self) -> str:
        return f"{self.name}={render(self.value)} at {self.at} in {self.fn}"


@dataclass(frozen=True)
class Call:
    at: str
    fn: str
    args: dict


@dataclass(frozen=True)
class Return:
    at: str
    fn: str
    value: Any


@dataclass(frozen=True)
class Raise:
    at: str
    fn: str
    type: str
    detail: str


class Trace:
    """A replayed execution's internal state, queryable.

    Every traced value is data (see serial.trace_jsonable): numbers compare, documents are
    dicts, and anything long is a prefix that still reports its true `len()`.
    """

    def __init__(self, events: Sequence[dict]):
        self.events = [e for e in events if e.get("e") != "H"]

    @classmethod
    def load(cls, path: Path) -> "Trace":
        from flight_recorder.replay import TRACE_VERSION
        with Path(path).open(encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        header = events[0] if events and events[0].get("e") == "H" else {}
        if header.get("trace_version") != TRACE_VERSION:
            # A version-1 trace holds reprs, and asserting arithmetic over reprs would
            # fail confusingly rather than loudly. Traces are cheap: regenerate.
            raise ValueError(
                f"{Path(path).name} was written by an older tracer "
                f"(version {header.get('trace_version', 1)}, need {TRACE_VERSION}) — "
                "re-run the replay to regenerate it")
        return cls(events)

    def __len__(self) -> int:
        return len(self.events)

    def values(self, name: str) -> list[Obs]:
        """Every value `name` took, in execution order — arguments it arrived with and each
        line that changed it. The timeline `--watch` prints, as data."""
        out = []
        for e in self.events:
            bag = e.get("d") if e["e"] == "L" else e.get("args") if e["e"] == "C" else None
            if bag and name in bag:
                out.append(Obs(e["at"], e["fn"], name, from_trace_jsonable(bag[name])))
        return out

    def first(self, name: str) -> Optional[Obs]:
        seen = self.values(name)
        return seen[0] if seen else None

    def final(self, name: str) -> Optional[Obs]:
        seen = self.values(name)
        return seen[-1] if seen else None

    def names(self) -> set[str]:
        out: set[str] = set()
        for e in self.events:
            out.update((e.get("d") or {}) if e["e"] == "L" else (e.get("args") or {}))
        return out

    def calls(self, fn: Optional[str] = None) -> list[Call]:
        return [Call(e["at"], e["fn"], {k: from_trace_jsonable(v)
                                        for k, v in (e.get("args") or {}).items()})
                for e in self.events
                if e["e"] == "C" and (fn is None or e["fn"] == fn or e["fn"].endswith("." + fn))]

    def returns(self, fn: Optional[str] = None) -> list[Return]:
        return [Return(e["at"], e["fn"], from_trace_jsonable(e.get("v")))
                for e in self.events
                if e["e"] == "R" and (fn is None or e["fn"] == fn or e["fn"].endswith("." + fn))]

    def raised(self) -> list[Raise]:
        return [Raise(e["at"], e["fn"], e.get("type", ""), e.get("v", ""))
                for e in self.events if e["e"] == "X"]

    def __iter__(self) -> Iterator[dict]:
        return iter(self.events)


# --- the trajectory an invariant sees ---------------------------------------------------

@dataclass(frozen=True)
class Trajectory:
    """One replayed call, whole: what went in, what came out, and everything in between.

    `result` is what the REPLAYED code produced, not what was recorded — and it is None
    when the call raised. A tool that legitimately raises will hand result-reading
    invariants a None; guard those with `t.raised` (or early-return on it).

    `writes` is every write the replayed code performed — op, chain signature, args.
    Writes are never executed on replay, but they are first-class trajectory: "never
    writes when the corpus is empty" is an assertable claim."""
    fn: str
    kwargs: dict
    result: Any
    error: Optional[str]
    trace: Trace
    replay: ReplayReport
    writes: list = field(default_factory=list)

    @property
    def raised(self) -> bool:
        return self.error is not None


# --- declaring invariants ---------------------------------------------------------------

@dataclass(frozen=True)
class Invariant:
    description: str
    check: Callable[[Trajectory], None]
    # This claim consciously judges the raise/no-raise question (it asserts on t.raised /
    # t.error). Its presence tells probe checking that a crash has a designated judge, so
    # the safety-net `raised` outcome stands down. See check_invariants.
    judges_raise: bool = False

    def __call__(self, t: Trajectory) -> None:
        self.check(t)


def invariant(description: str, judges_raise: bool = False
              ) -> Callable[[Callable[[Trajectory], None]], Invariant]:
    """Declare a claim about every execution. The body asserts; the description is what a
    failure is reported as, so write it as the property, not as the check.

    Set `judges_raise=True` on the claim that decides whether raising is correct (e.g.
    "rejects a hostile input with ValueError", or "never crashes"). Without one, a probe
    replay whose call raised is reported as the outcome `raised` rather than silently
    held — a crash must never pass because every claim politely looked away."""
    def wrap(fn: Callable[[Trajectory], None]) -> Invariant:
        return Invariant(description=description, check=fn, judges_raise=judges_raise)
    return wrap


def collect(source: Any) -> list[Invariant]:
    """Every Invariant declared in a module (or listed in a sequence). Lets a module of
    `@invariant`-decorated `def _` be pointed at directly.

    An explicit sequence is a claim that every entry is an invariant, so a bare function
    in one is an error — silently dropping it would report `held` for a claim that was
    never checked. (A module is different: its other members are just other members.)"""
    if isinstance(source, Invariant):
        return [source]
    if isinstance(source, (list, tuple, set)):
        for i in source:
            if not isinstance(i, Invariant):
                raise TypeError(
                    f"{i!r} is not an Invariant — decorate it with @invariant(\"…\")")
        return list(source)
    return [v for v in vars(source).values() if isinstance(v, Invariant)]


# --- checking ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    invariant: str
    detail: str
    broke: bool = False  # the invariant itself raised something other than an assertion


@dataclass
class InvariantReport:
    fn: str
    outcome: str                 # held | violated | diverged | unanswerable | raised
    replay: ReplayReport
    violations: list = field(default_factory=list)
    checked: int = 0

    @property
    def probe(self) -> bool:
        return self.replay.probe

    @property
    def reproduced(self) -> bool:
        """Whether the replay reproduced the recording bit-for-bit. Independent of the
        invariants: the code can have changed its answer (not reproduced) while every
        claim about it still holds — and vice versa."""
        return self.replay.ok

    @property
    def ok(self) -> bool:
        """Everything is fine. Strict: the recording reproduced AND every invariant held —
        the two verdicts stay separately readable (`reproduced`, `outcome`) because they
        impeach different things. Probe (mutated recording): reproduction is meaningless
        by construction, so `held` alone decides."""
        if self.probe:
            return self.outcome == "held"
        return self.outcome == "held" and self.replay.ok


def check_invariants(session: Path, index: int, adapter: ReplayAdapter,
                     invariants: Any, trace_path: Optional[Path] = None,
                     probe: bool = False) -> InvariantReport:
    """Replay one recorded call under tracing, then assert every invariant against it.

    A replay that diverged (the code asked the boundary a different question than the
    recording holds) leaves a truncated trajectory. Asserting over it would be asserting
    over a fiction, so the outcome is `diverged` and no invariant is run — the recording,
    not the code, is what needs attention.

    A replay that ran to completion but no longer matches the recording is different: the
    trajectory is real (it is what the current code does on those boundary inputs), so the
    invariants ARE checked — but `ok` stays False via `reproduced`.

    `probe=True` (or a call pinned with `"probe": true` by the mutation API) checks a
    MUTATED recording: reproduction is meaningless, the invariants alone judge, and a
    boundary question the edited tape cannot answer is the outcome `unanswerable` —
    impeaching neither the code nor the claim, only this recording's reach.
    """
    checks = collect(invariants)
    with tempfile.TemporaryDirectory() as tmp:
        path = trace_path or (Path(tmp) / f"{Path(session).stem}.call{index}.trace.jsonl")
        report = replay_call(Path(session), index, adapter, path, probe=probe)
        if trace_path is None:
            report.trace_path = None  # it lives in tmp and dies with this block
        if report.divergence:
            return InvariantReport(fn=report.fn, outcome="diverged", replay=report)
        if report.unanswerable:
            return InvariantReport(fn=report.fn, outcome="unanswerable", replay=report)

        trajectory = Trajectory(
            fn=report.fn,
            kwargs=report.call_kwargs,
            result=from_jsonable(report.replayed_result),
            error=report.replayed_error,
            trace=Trace.load(path),
            replay=report,
            writes=report.replayed_writes,
        )

    violations = []
    for inv in checks:
        try:
            inv(trajectory)
        except AssertionError as e:
            violations.append(Violation(inv.description, str(e) or "assertion failed"))
        except Exception as e:  # the invariant is broken, not the code
            detail = f"{type(e).__name__}: {e}"
            if trajectory.raised:
                detail += ("\n(the replayed call raised, so t.result is None — "
                           "guard this invariant with t.raised)")
            violations.append(Violation(inv.description, detail, broke=True))

    outcome = "violated" if violations else "held"
    if (report.probe and trajectory.raised and not violations
            and not any(c.judges_raise for c in checks)):
        # Under mutation a crash can slip every claim: the recommended strict-mode guard
        # is `if t.raised: return`, and a suite of guarded claims would wave a crash
        # through as `held`. Unless some claim owns the raise question (judges_raise),
        # an unjudged crash is its own outcome — never a silent pass.
        outcome = "raised"

    return InvariantReport(fn=report.fn, outcome=outcome,
                           replay=report, violations=violations, checked=len(checks))


def format_invariant_report(report: InvariantReport) -> str:
    if report.outcome == "diverged":
        return (f"{report.fn}: replay DIVERGED, so no invariant was checked — the recording "
                f"no longer describes this code.\n  {report.replay.divergence}")
    if report.outcome == "unanswerable":
        return (f"{report.fn}: UNANSWERABLE — the mutation sent the code down a path this "
                f"recording cannot answer; no invariant was checked.\n"
                f"  {report.replay.unanswerable}")
    if report.outcome == "raised":
        return (f"{report.fn}: RAISED under mutation — {report.replay.replayed_error} — "
                f"and no declared invariant judges the raise. If crashing here is a bug, "
                f"assert `not t.raised`; if raising is the correct hostile-input "
                f"behavior, declare that claim with @invariant(..., judges_raise=True).")
    if report.ok:
        kind = "probe: " if report.probe else ""
        return f"{report.fn}: {kind}{report.checked} invariant(s) held"
    if report.outcome == "held":  # invariants fine; the replay itself didn't reproduce
        return (f"{report.fn}: {report.checked} invariant(s) held, but the replay did NOT "
                f"reproduce the recording — the code's answer changed")
    lines = [f"{report.fn}: {len(report.violations)} of {report.checked} invariant(s) VIOLATED"]
    for v in report.violations:
        lines.append(f"  {'BROKEN INVARIANT' if v.broke else 'violated'}: {v.invariant}")
        for detail in v.detail.splitlines() or [""]:
            lines.append(f"      {detail}")
    return "\n".join(lines)
