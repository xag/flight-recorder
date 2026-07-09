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

from flight_recorder.replay import (
    ReplayAdapter, format_report, load_session, replay_call,
)


class FlightDivergence(Exception):
    """A pinned recording no longer reproduces. Carries the formatted report as its message
    so pytest prints the divergence rather than a traceback into the replay machinery."""


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


def pytest_configure(config: pytest.Config) -> None:
    """Resolve the adapter once, at startup. A missing, malformed, or unimportable
    `flight_adapter` is a usage error before collection — not the same error repeated once
    per recorded call."""
    if _ini_path(config, "flight_recordings") is not None:
        _resolve_adapter(config)


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
            yield FlightUnreadableItem.from_parent(self, name="unreadable", reason=str(e))
            return
        for i, call in enumerate(calls):
            yield FlightCallItem.from_parent(
                self, name=f"call{i}::{call['fn']}", index=i)


class FlightUnreadableItem(pytest.Item):
    def __init__(self, *, reason: str, **kw):
        super().__init__(**kw)
        self.reason = reason

    def runtest(self) -> None:
        raise FlightUnreadable(
            f"{self.path.name} is under flight_recordings but is not a readable flight "
            f"session: {self.reason}")

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, FlightUnreadable):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        return self.path, None, self.name


class FlightCallItem(pytest.Item):
    def __init__(self, *, index: int, **kw):
        super().__init__(**kw)
        self.index = index

    def runtest(self) -> None:
        session = Path(self.path)
        trace_dir = _ini_path(self.config, "flight_trace")
        trace_path = None
        if trace_dir is not None:
            trace_dir.mkdir(parents=True, exist_ok=True)
            trace_path = trace_dir / f"{session.stem}.call{self.index}.trace.jsonl"
        report = replay_call(session, self.index, _load_adapter(self.config), trace_path)
        if not report.ok:
            raise FlightDivergence(format_report(self.index, report))

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, FlightDivergence):
            return f"{self.path.name} no longer reproduces:\n\n{excinfo.value}"
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
