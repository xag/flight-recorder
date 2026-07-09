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
    Feed, PlaybackChain, ReplayAdapter, ReplayDivergence, ReplayedEffectError,
    ReplayReport, Snap, Tracer, format_report, load_session, replay_call, run_cli,
)
from flight_recorder.serial import from_jsonable, snapshot_jsonable, to_jsonable

__all__ = [
    "Boundary", "ChainTarget", "DEFAULT_TERMINAL_READS", "DEFAULT_TERMINAL_WRITES",
    "ChainNode", "DatetimeShim", "Gate", "RandomShim", "SessionSink", "FORMAT_VERSION",
    "hook", "install", "install_mcp", "session_path", "uninstall",
    "Feed", "PlaybackChain", "ReplayAdapter", "ReplayDivergence", "ReplayedEffectError",
    "ReplayReport", "Snap", "Tracer", "format_report", "load_session", "replay_call",
    "run_cli", "from_jsonable", "snapshot_jsonable", "to_jsonable",
]
