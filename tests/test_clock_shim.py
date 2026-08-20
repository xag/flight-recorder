"""The clock shim patches by identity, not by name.

`TimeShim` stands in for `import time`. A module that says `from datetime import time`
binds the same NAME to a class, and patching that one replaced a constructor with a
clock: `time(9)` became a TypeError. Only inside recording — so the tape recorded,
faithfully, a program the instrumentation had broken. That is the one failure a
recorder must never have, because every other bug it can be trusted to report.
"""

from __future__ import annotations

import sys
import time as _stdlib_time
from datetime import datetime, time

import pytest

import flight_recorder as fr
from flight_recorder.record import patch_boundary, unpatch_all


@pytest.fixture(autouse=True)
def _restore():
    yield
    unpatch_all()


def _boundary() -> fr.Boundary:
    return fr.Boundary(clock_modules=[sys.modules[__name__]])


def test_a_datetime_time_survives_being_declared_a_clock_module():
    """This module imports `time` from datetime, and declares itself a clock module —
    exactly the shape that broke. The constructor must still construct."""
    assert time is not _stdlib_time
    patch_boundary(_boundary())
    assert time is not _stdlib_time, "the shim replaced datetime.time"
    assert time(9).hour == 9, "a clock was patched over a constructor"


def test_a_real_time_module_is_still_shimmed():
    """The other half: identity must not become 'never patch'. A module that really
    does `import time` still gets its perf_counter recorded."""
    module = sys.modules[__name__]
    setattr(module, "time", _stdlib_time)
    try:
        patch_boundary(_boundary())
        assert getattr(module, "time") is not _stdlib_time, "the clock was not shimmed"
        assert isinstance(getattr(module, "time"), fr.TimeShim)
    finally:
        unpatch_all()
        setattr(module, "time", time)


def test_a_module_with_no_time_at_all_is_left_alone():
    """A boundary that declares a clock module holding no `time` was correct before the
    shim existed and stays correct: nothing to patch is not an error."""
    module = type(sys)("clockless")
    module.datetime = datetime  # a clock module reads the clock somehow; this one only here
    patch_boundary(fr.Boundary(clock_modules=[module]))
    assert not hasattr(module, "time")


def test_a_pinned_clock_answers_the_pin_running_and_records_it_as_now():
    """`clock_at` makes a recorded now() answer the pinned instant plus the time since - in
    the asked timezone - and the tape carries that answer as its `now` event, exactly as if
    the machine's clock had said so. Running: two reads are two instants, in order, so an
    app stamping its writes with the clock still stamps them apart. The pin is lifted on
    exit, nested or not."""
    from datetime import timedelta, timezone

    from flight_recorder.record import DatetimeShim, _active, hook

    at = datetime(2026, 8, 16, 8, 0, tzinfo=timezone.utc)
    buf: list = []
    hook.mode = "record"
    token = _active.set(buf)
    try:
        with fr.clock_at(at):
            first = DatetimeShim.now(timezone.utc)
            assert timedelta(0) <= first - at < timedelta(seconds=5)
            assert DatetimeShim.now(timezone(timedelta(hours=2))).hour == 10
            with fr.clock_at(at + timedelta(days=1)):
                assert DatetimeShim.now(timezone.utc).day == 17
            later = DatetimeShim.now(timezone.utc)
            assert later > first, "a set clock runs; it does not stop"
            assert later - at < timedelta(seconds=5)
        assert hook.pinned_now is None
        assert abs((DatetimeShim.now(timezone.utc) - datetime.now(timezone.utc)).total_seconds()) < 5
    finally:
        hook.mode = "off"
        _active.reset(token)
    nows = [e for e in buf if e.get("k") == "now"]
    assert len(nows) == 5
    assert nows[0]["v"] == first.isoformat()
    assert nows[0]["v"].startswith("2026-08-16T08:00:0")


def test_a_pin_is_a_datetime():
    with pytest.raises(TypeError):
        with fr.clock_at("2026-08-16"):  # type: ignore[arg-type]
            pass
