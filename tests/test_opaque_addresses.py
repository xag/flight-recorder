"""A tape must not contain pointers.

The default repr of an object carries its memory address — `<Image object at 0x7f3c…>`.
Recording that records a pointer, which differs on every run, so the effect or result it
belongs to can never match on replay. Any tool returning a plain object (an image, a
file handle, a client) was unreplayable for that reason alone, and the divergence looked
like a bug in the code under test.

The tracer already scrubbed addresses. The tape did not.
"""

import re

from flight_recorder.serial import to_jsonable

ADDR = re.compile(r"0x[0-9A-Fa-f]{4,}")


class Image:
    """A stand-in for anything with the default repr — mcp's Image, a socket, a client."""


class Unreprable:
    def __repr__(self):
        raise RuntimeError("no repr for you")


def test_an_objects_address_never_reaches_the_tape():
    once = to_jsonable(Image())
    again = to_jsonable(Image())

    assert "Image" in once["__opaque__"], "the marker should still say what it was"
    assert not ADDR.search(once["__opaque__"]), f"a pointer reached the tape: {once}"
    # ...and the point of all that: two runs of the same code record the same value.
    assert once == again


def test_addresses_are_scrubbed_wherever_the_object_hides():
    rec = to_jsonable({"result": [{"img": Image()}]})
    assert not ADDR.search(str(rec)), rec


def test_a_repr_that_raises_does_not_take_the_recording_down():
    rec = to_jsonable(Unreprable())
    assert "Unreprable" in rec["__opaque__"]
    assert not ADDR.search(rec["__opaque__"])
