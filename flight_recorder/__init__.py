"""flight-recorder: record any app's tool calls at their nondeterminism boundary; replay
them deterministically with every internal variable observable.

A program's execution is fully determined by its code plus its nondeterministic inputs.
Declare those inputs as a `Boundary` (effect functions, chained clients, clock, random,
env constants), `install()` the recorder, and every call becomes one JSONL record — args,
ordered boundary events, result. `replay_call()` re-executes a record on the real code with
the recorded inputs fed back, under a `sys.settrace` tracer, and reports whether the
recording was reproduced bit-for-bit.

The cardinal rule, for this lib and for the per-app boundary declarations it consumes:
INSTRUMENT, NEVER DUPLICATE. Nothing evaluates queries or reimplements client behavior;
the only structural knowledge anywhere is names.
"""

from flight_recorder.boundary import (
    Boundary, ChainTarget, DEFAULT_TERMINAL_READS, DEFAULT_TERMINAL_WRITES,
)
from flight_recorder.record import (
    ChainNode, DatetimeShim, Gate, RandomShim, SessionSink, FORMAT_VERSION,
    hook, install, install_mcp, session_path, uninstall,
)
from flight_recorder.replay import (
    Feed, PlaybackChain, ProbeUnanswerable, ReplayAdapter, ReplayDivergence,
    ReplayedEffectError, ReplayReport, Snap, TRACE_VERSION, Tracer, format_report,
    load_session, replay_call, run_cli,
)
from flight_recorder.mutate import Recording
from flight_recorder.invariants import (
    Call, InvariantReport, Invariant, Obs, Raise, Return, Trace, Trajectory, Violation,
    check_invariants, collect, format_invariant_report, invariant,
)
from flight_recorder.design import (
    DesignInvariant, DesignReport, Node, Render, check_design, contrast, design_invariant,
    format_design_report, load_renders, luminance, standard_invariants, token_invariants,
)
from flight_recorder.session import (
    Finding, Session, SessionInvariant, SessionVerdict, Step, check_sessions,
    format_session_verdict, load_sessions, no_retry_after_failure, no_tool_bounce,
    no_wasted_repeats, session_invariant,
)
from flight_recorder.serial import (
    REDACTED, Truncated, TruncatedText, from_jsonable, from_trace_jsonable,
    redact_jsonable, snapshot_jsonable, to_jsonable, trace_jsonable,
)

__all__ = [
    "Boundary", "ChainTarget", "DEFAULT_TERMINAL_READS", "DEFAULT_TERMINAL_WRITES",
    "ChainNode", "DatetimeShim", "Gate", "RandomShim", "SessionSink", "FORMAT_VERSION",
    "hook", "install", "install_mcp", "session_path", "uninstall",
    "Feed", "PlaybackChain", "ProbeUnanswerable", "Recording", "ReplayAdapter",
    "ReplayDivergence", "ReplayedEffectError", "ReplayReport", "Snap", "TRACE_VERSION",
    "Tracer", "format_report", "load_session", "replay_call", "run_cli",
    "Call", "Invariant", "InvariantReport", "Obs", "Raise", "Return", "Trace", "Trajectory",
    "Violation", "check_invariants", "collect", "format_invariant_report", "invariant",
    "DesignInvariant", "DesignReport", "Node", "Render", "check_design", "contrast",
    "design_invariant", "format_design_report", "load_renders", "luminance",
    "standard_invariants", "token_invariants",
    "Finding", "Session", "SessionInvariant", "SessionVerdict", "Step", "check_sessions",
    "format_session_verdict", "load_sessions", "no_retry_after_failure", "no_tool_bounce",
    "no_wasted_repeats", "session_invariant",
    "REDACTED", "Truncated", "TruncatedText", "from_jsonable", "from_trace_jsonable",
    "redact_jsonable", "snapshot_jsonable", "to_jsonable", "trace_jsonable",
]
