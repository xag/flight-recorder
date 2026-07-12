"""Trajectory invariants: claims about the sequence of calls the MODEL chose to make.

The recorder was built to judge the code. It replays one call and asserts over its internals, and
that is the right instrument for the question *is the implementation correct?* It has nothing to
say about a different question, which for an MCP server is the one that decides whether the
product is any good:

    the code is perfect, and the model drove it stupidly.

That is not a bug in a function. It is a bug in the SURFACE — in what the tools are called, in
what their descriptions promise, in what their results tell the caller to do next. No unit test
can see it, because every call was individually correct. It is only visible in the ORDER.

And the order is already on the tape. A session line carries `fn`, `kwargs`, `result` and `ts`,
one line per call, in the order the calls were made — which IS the model's policy, executed. The
recorder has been writing the model's choices down since the first commit. Nobody has read them
that way.

    @session_invariant("the model never repeats a call whose answer cannot have changed")
    def _(s: Session):
        for first, again in s.repeats():
            assert False, f"{again.fn} at {again.seq} repeats {first.seq} verbatim"

    verdict = check_sessions(load_sessions("flight/"), CLAIMS)
    print(format_session_verdict(verdict))

THE VERDICT IS A RATE, NOT A BOOLEAN

Everywhere else in this library a claim either holds or is violated, because the code is
deterministic given its boundary answers. The model is not. A session that violates a claim is
EVIDENCE that the surface invites the mistake, not proof that it always will; and a session that
holds proves nothing about the next one. So a trajectory claim is checked over MANY sessions and
reported as a rate, and the way to test a fix to a tool description is to re-run the sessions and
watch the rate move. This is the one place in flight-recorder where the answer is statistical, and
pretending otherwise would be the most dishonest thing the library could do.

WHAT THE TAPE CANNOT SEE

It holds the model's calls. It does not hold the model's words. So "it gave me flashcards and then
asked how I did, when it already knew" is only half-visible here: the tape can prove the server
was ASKED (get_status was called, and its result carried the last round), and it cannot prove what
the model then said to the user. A trajectory claim answers *did it have the information?* — never
*did it use it?* Claims must be written to the first question. The second one needs the transcript,
which is a different boundary and is not recorded here.

A VIOLATION HERE IMPEACHES THE SURFACE, NOT THE CODE

Which is why a finding carries `about`. A replay divergence says the code changed. A trajectory
violation says the tool descriptions, the result prose, or the protocol let a competent model do
the wrong thing — and the fix is almost never in the function body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from flight_recorder.serial import from_jsonable, render

# --- one choice the model made ------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One tool call: what the model asked for, and what it read back."""

    seq: int
    fn: str
    kwargs: dict
    result: Any
    error: Optional[str]
    ts: Optional[datetime]
    ms: float
    events: list = field(default_factory=list)

    @property
    def text(self) -> str:
        """The result as the model read it. An MCP tool answers in prose, so this is the whole of
        what the next choice was conditioned on."""
        if isinstance(self.result, str):
            return self.result
        return render(self.result)

    @property
    def key(self) -> str:
        """The identity of a call. Two steps with the same key asked the world the same question
        in the same words."""
        return self.fn + "\x00" + json.dumps(self.kwargs, sort_keys=True, default=str)

    @property
    def raised(self) -> bool:
        """The call threw.

        NOT the same as the call FAILING. A tool that catches its own error and returns
        "Couldn't create the issue: ..." as a perfectly successful string has `error: null` on the
        tape — the failure is prose, and prose is invisible to every mechanical check. That is a
        finding about the surface in its own right, and it is why `failed=` is a predicate the app
        supplies rather than something this module presumes to know."""
        return self.error is not None

    @property
    def wrote_to_store(self) -> bool:
        """This call wrote through a chained client. Per tape v1 a `db` event carries `res` (a
        read) or `args` (a write), so the tape says which.

        NOT "this call changed the world". An app whose writes go through an `fx` effect instead
        of a chain terminal writes without a single `db` event, and the tape cannot tell an
        effect that read from one that wrote — `fx` records a name, its arguments and its answer,
        and nothing about its direction. So this is a fact about chains, and a claim that needs to
        know whether the world moved must not lean on it. See `Session.repeats`."""
        return any(e.get("k") == "db" and "args" in e for e in self.events)

    def __repr__(self) -> str:
        return f"<{self.seq} {self.fn}({', '.join(f'{k}={v!r}' for k, v in self.kwargs.items())})>"


class Session:
    """One recording, read as the model's trajectory rather than as a set of calls."""

    def __init__(self, steps: list, source: Optional[Path] = None, started: Optional[str] = None):
        self.steps = steps
        self.source = source
        self.started = started

    @property
    def name(self) -> str:
        return self.source.stem if self.source else "session"

    def __len__(self) -> int:
        return len(self.steps)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)

    def __getitem__(self, i):
        return self.steps[i]

    @property
    def fns(self) -> list:
        """The trajectory, as a list of names. Usually the first thing worth looking at."""
        return [s.fn for s in self.steps]

    def where(self, *fns: str) -> list:
        return [s for s in self.steps if s.fn in fns]

    def before(self, step: Step) -> list:
        return [s for s in self.steps if s.seq < step.seq]

    def between(self, a: Step, b: Step) -> list:
        return [s for s in self.steps if a.seq < s.seq < b.seq]

    def gap(self, a: Step, b: Step) -> Optional[float]:
        """Seconds between two calls — the model's own thinking time, as the server saw it."""
        if not (a.ts and b.ts):
            return None
        return (b.ts - a.ts).total_seconds()

    def repeats(self) -> list:
        """Every call the model made twice, verbatim, AND GOT THE SAME ANSWER BACK.

        Asking the same question twice is perfectly rational when the answer could have changed.
        So the test is not whether the call is identical — it is whether the ANSWER was: two
        identical calls that returned identical results means the second one demonstrably learned
        nothing, and that is a fact read off the tape rather than an inference about the world.

        The first version of this asserted "and nothing wrote in between", reading the tape's `db`
        write events. It was unsound and a real recording said so within a minute: a coach session
        called `undo_practice` twice and got "Undid the last batch" and then "Nothing to undo" —
        the world had plainly moved, and no `db` write event marked it, because that app's writes
        go through an `fx` effect, and an `fx` event does not say whether it read or wrote.
        Comparing the answers needs none of that, and cannot be wrong about it."""
        out = []
        seen: dict = {}
        for s in self.steps:
            first = seen.setdefault(s.key, s)
            if first is not s and first.result == s.result:
                out.append((first, s))
        return out


def load_sessions(*paths) -> list:
    """Every recording under the given files, directories, or globs — trace files skipped."""
    files: list = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += sorted(q for q in p.glob("*.jsonl") if ".trace." not in q.name)
        else:
            files.append(p)

    out = []
    for f in files:
        steps, started = [], None
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line; every tape reader must tolerate it
            if d.get("ev") == "session":
                started = d.get("started")
            elif d.get("ev") == "call":
                ts = None
                if d.get("ts"):
                    try:
                        ts = datetime.fromisoformat(d["ts"])
                    except ValueError:
                        pass
                steps.append(Step(
                    seq=d["seq"], fn=d["fn"], kwargs=d.get("kwargs", {}),
                    result=from_jsonable(d.get("result")), error=d.get("error"),
                    ts=ts, ms=d.get("ms", 0.0), events=d.get("events", []),
                ))
        if steps:
            out.append(Session(steps, source=f, started=started))
    return out


# --- declaring claims ----------------------------------------------------------------------


@dataclass(frozen=True)
class SessionInvariant:
    description: str
    check: Callable[[Session], None]
    # What a violation impeaches. Almost always the SURFACE: the tool names, their descriptions,
    # the prose their results answer with. Saying so on the finding stops a reader from going to
    # look for a bug in a function that does not have one.
    about: str = "surface"

    def __call__(self, s: Session) -> None:
        self.check(s)


def session_invariant(description: str, about: str = "surface"
                      ) -> Callable[[Callable[[Session], None]], SessionInvariant]:
    """Declare a claim about every session. The body asserts; the description is what a failure is
    reported as, so write it as the property the surface should have."""

    def wrap(fn: Callable[[Session], None]) -> SessionInvariant:
        return SessionInvariant(description=description, check=fn, about=about)

    return wrap


def collect(source: Any) -> list:
    if isinstance(source, SessionInvariant):
        return [source]
    if isinstance(source, (list, tuple, set)):
        for i in source:
            if not isinstance(i, SessionInvariant):
                raise TypeError(f'{i!r} is not a SessionInvariant — decorate it with @session_invariant("…")')
        return list(source)
    return [v for v in vars(source).values() if isinstance(v, SessionInvariant)]


@dataclass(frozen=True)
class Finding:
    invariant: str
    about: str
    session: str
    detail: str
    broke: bool = False


@dataclass
class SessionVerdict:
    """What the claims said across every session — as rates, because the model is not
    deterministic and a single session neither convicts nor acquits a tool surface."""

    sessions: int = 0
    steps: int = 0
    findings: list = field(default_factory=list)
    # description -> (sessions violated, sessions checked)
    tally: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings

    def rate(self, description: str) -> float:
        bad, total = self.tally.get(description, (0, 0))
        return bad / total if total else 0.0


def check_sessions(sessions: Iterable, invariants: Any) -> SessionVerdict:
    checks = collect(invariants)
    sessions = list(sessions)
    v = SessionVerdict(sessions=len(sessions), steps=sum(len(s) for s in sessions))

    for inv in checks:
        v.tally[inv.description] = (0, len(sessions))

    for s in sessions:
        for inv in checks:
            try:
                inv(s)
            except AssertionError as e:
                bad, total = v.tally[inv.description]
                v.tally[inv.description] = (bad + 1, total)
                v.findings.append(Finding(inv.description, inv.about, s.name, str(e) or "assertion failed"))
            except Exception as e:  # the claim is broken, not the surface
                bad, total = v.tally[inv.description]
                v.tally[inv.description] = (bad + 1, total)
                v.findings.append(
                    Finding(inv.description, inv.about, s.name, f"{type(e).__name__}: {e}", broke=True))
    return v


def format_session_verdict(v: SessionVerdict) -> str:
    lines = [f"{v.sessions} session(s), {v.steps} call(s)"]
    for desc, (bad, total) in v.tally.items():
        mark = "ok " if not bad else "   "
        lines.append(f"  [{mark}] {desc}")
        if bad:
            lines.append(f"         violated in {bad}/{total} sessions ({100 * bad / total:.0f}%)")
    if v.findings:
        lines.append("")
        for f in v.findings:
            lines.append(f"  {f.session}  [{f.about}]  {f.invariant}"
                         + ("  (the claim itself broke)" if f.broke else ""))
            for ln in f.detail.splitlines():
                lines.append(f"      {ln}")
    return "\n".join(lines)


# --- the portable claims --------------------------------------------------------------------


def no_wasted_repeats() -> SessionInvariant:
    """The model never asks a question it has already had answered.

    Fully portable, and it needs no app knowledge whatsoever: it compares the call to the call and
    the answer to the answer, both of which are on every tape ever recorded."""

    @session_invariant("the model never re-asks a question it already had the answer to")
    def _(s: Session):
        bad = [f"call {b.seq} {b.fn} repeats call {a.seq} verbatim and gets the same answer back"
               for a, b in s.repeats()]
        assert not bad, "\n      ".join(bad)

    return _


def no_retry_after_failure(failed: Callable[[Step], bool]) -> SessionInvariant:
    """The model never re-issues, unchanged, a call that just failed.

    `failed` is the app's, not this module's, because a tool that returns "Couldn't do it: ..." as
    a successful string has hidden its failure in prose, and a library that guessed at that prose
    would be inventing knowledge it does not have. Supplying the predicate is also the moment you
    notice that the failure was never machine-readable in the first place."""

    @session_invariant("the model never retries a call that already failed the same way")
    def _(s: Session):
        bad = []
        for i, step in enumerate(s.steps):
            if not failed(step):
                continue
            for later in s.steps[i + 1:]:
                if later.key == step.key and failed(later):
                    bad.append(f"call {later.seq} {later.fn} re-issues call {step.seq} unchanged, "
                               f"after it failed with: {step.text.strip().splitlines()[0][:90]!r}")
                    break
        assert not bad, "\n      ".join(bad)

    return _


def no_tool_bounce(misrouted: Callable[[Step], bool]) -> SessionInvariant:
    """No call is answered only by being told to call a different tool.

    A bounce is the surface confessing that the model could not tell two tools apart. The server
    handles it gracefully, every test passes, and the user waits for a round trip that carried no
    information. `misrouted` is the app's, because only the app knows what its own redirection
    prose looks like."""

    @session_invariant("no call is answered only by a redirection to another tool")
    def _(s: Session):
        bad = [f"call {st.seq} {st.fn}({', '.join(f'{k}={v!r}' for k, v in st.kwargs.items() if k != 'email')})"
               f"\n        -> {st.text.strip().splitlines()[0][:100]}"
               for st in s.steps if misrouted(st)]
        assert not bad, "\n      ".join(bad)

    return _
