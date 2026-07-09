"""pytest integration: pinned recordings as a non-regression suite.

A recording is a *regression* oracle, never a correctness one — it asserts only that the
code still behaves as it behaved when the recording was pinned. That is exactly what a
non-regression suite wants, and this plugin is the batteries for it: point it at a
directory of pinned `.jsonl` sessions and each recorded call becomes its own test, passing
when `replay_call` reproduces the recording bit-for-bit and failing with the same
divergence report the CLI prints.

    # pyproject.toml / pytest.ini / setup.cfg
    [tool.pytest.ini_options]
    flight_recordings = "tests/recordings"
    flight_adapter = "app.replay:Adapter"

Nothing here is active until `flight_recordings` and `flight_adapter` are both set, so the
plugin is inert in projects that merely depend on flight-recorder.

Replay patches the boundary process-wide, so these tests must not run in parallel with each
other (pytest-xdist will interleave them and they will diverge spuriously).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Optional

import pytest

from flight_recorder.invariants import (
    check_invariants, collect, format_invariant_report,
)
from flight_recorder.replay import (
    ReplayAdapter, format_report, load_session, replay_call,
)


class FlightDivergence(Exception):
    """A pinned recording no longer reproduces. Carries the formatted report as its message
    so pytest prints the divergence rather than a traceback into the replay machinery."""


class FlightViolation(Exception):
    """The recording reproduced, but the code broke a declared invariant. A different
    finding from a divergence: the recording is fine, the claim about the code is not."""


class FlightUnanswerable(Exception):
    """A mutated recording sent the code down a path the tape cannot answer. Impeaches
    neither the code nor the invariants — this fixture just doesn't reach that far."""


class FlightUnreadable(Exception):
    """A file under the recordings directory is not a readable flight session."""


# --- configuration ------------------------------------------------------------------

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("flight_recordings",
                  "directory of pinned flight recordings (.jsonl) to replay as tests",
                  default="")
    parser.addini("flight_adapter",
                  "import path of the ReplayAdapter, e.g. 'app.replay:Adapter'",
                  default="")
    parser.addini("flight_trace",
                  "directory to write replay state traces into (default: no tracing)",
                  default="")
    parser.addini("flight_invariants",
                  "module (or 'module:ATTR') declaring @invariant claims to assert against "
                  "every replayed call",
                  default="")


def _ini_path(config: pytest.Config, name: str) -> Optional[Path]:
    raw = str(config.getini(name)).strip()
    return (Path(str(config.rootpath)) / raw) if raw else None


def _resolve_adapter(config: pytest.Config):
    """Import `flight_adapter` and return the class or instance it names, without
    instantiating it."""
    spec = str(config.getini("flight_adapter")).strip()
    if not spec:
        raise pytest.UsageError(
            "flight_recordings is set but flight_adapter is not - the plugin cannot know "
            "how to resolve a recorded function name into a callable. Set e.g. "
            "flight_adapter = 'app.replay:Adapter'")
    mod_name, sep, attr = spec.partition(":")
    if not (sep and mod_name and attr):
        raise pytest.UsageError(
            f"flight_adapter must be 'module:Attribute', got {spec!r}")
    try:
        return getattr(importlib.import_module(mod_name), attr)
    except (ImportError, AttributeError) as e:
        raise pytest.UsageError(f"flight_adapter {spec!r} is not importable: {e}") from e


def _resolve_invariants(config: pytest.Config) -> list:
    """Import `flight_invariants` and collect the claims declared there. Accepts a module
    (every `@invariant` in it) or 'module:ATTR' (a list, or a single Invariant)."""
    spec = str(config.getini("flight_invariants")).strip()
    if not spec:
        return []
    mod_name, sep, attr = spec.partition(":")
    try:
        source = importlib.import_module(mod_name)
        if sep:
            source = getattr(source, attr)
    except (ImportError, AttributeError) as e:
        raise pytest.UsageError(f"flight_invariants {spec!r} is not importable: {e}") from e
    found = collect(source)
    if not found:
        raise pytest.UsageError(f"flight_invariants {spec!r} declares no @invariant")
    return found


def pytest_configure(config: pytest.Config) -> None:
    """Resolve the adapter and the invariants once, at startup. A missing, malformed, or
    unimportable `flight_adapter` is a usage error before collection — not the same error
    repeated once per recorded call."""
    if _ini_path(config, "flight_recordings") is not None:
        _resolve_adapter(config)
        _resolve_invariants(config)


def _load_adapter(config: pytest.Config) -> ReplayAdapter:
    """Build the adapter afresh per test: an adapter may construct a Boundary in its
    __init__, and replay mutates what a Boundary points at."""
    obj = _resolve_adapter(config)
    return obj() if isinstance(obj, type) else obj


# --- collection ---------------------------------------------------------------------

def pytest_collect_file(file_path: Path, parent: pytest.Collector):
    if file_path.suffix != ".jsonl":
        return None
    root = _ini_path(parent.config, "flight_recordings")
    if root is None:
        return None
    try:
        if not file_path.resolve().is_relative_to(root.resolve()):
            return None
    except OSError:
        return None
    return FlightSessionFile.from_parent(parent, path=file_path)


class FlightSessionFile(pytest.File):
    """One pinned session file; one test item per call it recorded."""

    def collect(self):
        try:
            _, calls = load_session(Path(self.path))
        except (ValueError, OSError) as e:
            # A stray, truncated, or half-written .jsonl under the recordings directory.
            # It is a failing test of its own, not a collection error: raising here would
            # abort collection for the entire run, including the recordings that are fine.
            yield FlightUnreadableItem.from_parent(
                self, name="unreadable",
                reason=f"{Path(self.path).name} is under flight_recordings but is not a "
                       f"readable flight session: {e}")
            return
        has_invariants = bool(str(self.config.getini("flight_invariants")).strip())
        for i, call in enumerate(calls):
            probe = bool(call.get("probe"))
            if probe and not has_invariants:
                # A mutated recording asserts nothing by itself: its recorded result
                # predates the mutation, so only a declared claim can judge the code.
                yield FlightUnreadableItem.from_parent(
                    self, name=f"call{i}::{call['fn']}::probe",
                    reason=f"{Path(self.path).name} call {i} is a probe (mutated) "
                           "recording, which is only meaningful against invariants — "
                           "set flight_invariants")
                continue
            yield FlightCallItem.from_parent(
                self, name=f"call{i}::{call['fn']}" + ("::probe" if probe else ""),
                index=i, probe=probe)


class FlightUnreadableItem(pytest.Item):
    """A recordings-directory entry that cannot serve as a test, failing with the reason
    (pre-formatted by the collector) instead of aborting the run."""

    def __init__(self, *, reason: str, **kw):
        super().__init__(**kw)
        self.reason = reason

    def runtest(self) -> None:
        raise FlightUnreadable(self.reason)

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, FlightUnreadable):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        return self.path, None, self.name


class FlightCallItem(pytest.Item):
    def __init__(self, *, index: int, probe: bool = False, **kw):
        super().__init__(**kw)
        self.index = index
        self.probe = probe

    def runtest(self) -> None:
        session = Path(self.path)
        trace_dir = _ini_path(self.config, "flight_trace")
        trace_path = None
        if trace_dir is not None:
            trace_dir.mkdir(parents=True, exist_ok=True)
            trace_path = trace_dir / f"{session.stem}.call{self.index}.trace.jsonl"
        adapter = _load_adapter(self.config)
        invariants = _resolve_invariants(self.config)

        if self.probe:
            # Collection already guaranteed invariants exist for probe items.
            result = check_invariants(session, self.index, adapter, invariants, trace_path)
            if result.outcome == "unanswerable":
                raise FlightUnanswerable(format_invariant_report(result))
            if result.outcome == "raised":
                # A crash every claim politely guarded around: fail, and say how to
                # judge it explicitly (assert not t.raised, or judges_raise=True).
                raise FlightViolation(format_invariant_report(result))
            if result.violations:
                exc = FlightViolation(format_invariant_report(result))
                exc.all_broke = all(v.broke for v in result.violations)
                raise exc
            return

        if not invariants:
            report = replay_call(session, self.index, adapter, trace_path)
            if not report.ok:
                raise FlightDivergence(format_report(self.index, report))
            return

        # With invariants declared, the recording answers two questions: does the code still
        # do what it did (replay), and is what it does right (invariants)?
        result = check_invariants(session, self.index, adapter, invariants, trace_path)
        if not result.replay.ok:  # divergence, result/error mismatch, or leftover events
            raise FlightDivergence(format_report(self.index, result.replay))
        if result.violations:
            exc = FlightViolation(format_invariant_report(result))
            exc.all_broke = all(v.broke for v in result.violations)
            raise exc

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, FlightDivergence):
            return f"{self.path.name} no longer reproduces:\n\n{excinfo.value}"
        if isinstance(excinfo.value, FlightUnanswerable):
            return (f"{self.path.name} cannot answer the path the code now takes:\n\n"
                    f"{excinfo.value}")
        if isinstance(excinfo.value, FlightUnreadable):
            return str(excinfo.value)
        if isinstance(excinfo.value, FlightViolation):
            # Blame accurately: a claim that itself raised impeaches the claim, not the code.
            blame = ("an invariant is broken" if getattr(excinfo.value, "all_broke", False)
                     else "the code is wrong")
            lead = "under mutation, " if self.probe else "reproduces, but "
            return f"{self.path.name} {lead}{blame}:\n\n{excinfo.value}"
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        return self.path, None, self.name


# --- fixture for hand-wired replays --------------------------------------------------

@pytest.fixture
def flight_replay(request: pytest.FixtureRequest):
    """Replay a recording inside an ordinary test, for assertions the collector's
    pass/fail cannot express (e.g. inspecting the trace)."""
    def _replay(session, call: int = 0, adapter: Optional[ReplayAdapter] = None,
                trace_path: Optional[Path] = None):
        return replay_call(Path(session), call,
                           adapter or _load_adapter(request.config), trace_path)
    return _replay
