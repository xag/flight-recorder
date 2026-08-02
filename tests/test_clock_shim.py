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
