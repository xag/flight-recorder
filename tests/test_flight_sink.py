"""What a sink must get right, in the order it matters.

The recorder's contract is unforgiving in one direction and forgiving in the other: it will
ignore any exception a sink raises, but it calls `publish` on the event-loop thread holding a
lock. So the tests that matter are about *timing and completeness*, not about bytes arriving —
bytes arriving is the easy part.
"""

import threading
import time

import pytest

from flight_sink import LocalSink, digest
from flight_sink.sink import _BaseSink


class SlowTransport:
    """A transport that blocks, so the test can tell whether `publish` waits for it."""

    def __init__(self, delay=0.2):
        self.delay = delay
        self.puts = []
        self.started = threading.Event()

    def put(self, name, data, sha256):
        self.started.set()
        time.sleep(self.delay)
        self.puts.append((name, data, sha256))


class FailingTransport:
    def __init__(self):
        self.attempts = 0

    def put(self, name, data, sha256):
        self.attempts += 1
        raise RuntimeError("bucket on fire")


def test_publish_does_not_block_on_the_transport():
    """The property the recorder's docstring is emphatic about: publish holds the write lock,
    so it must hand off and return. A sink that waits for the network stalls every request."""
    transport = SlowTransport(delay=0.5)
    sink = _BaseSink(transport)

    t0 = time.perf_counter()
    sink.publish("flight-1.jsonl", b"line\n")
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.05, f"publish blocked for {elapsed:.3f}s"
    assert transport.started.wait(timeout=2.0), "worker never started"
    sink.close()


def test_close_drains_so_the_final_tape_lands(tmp_path):
    """Only the last publish carries the whole tape. Exiting without draining keeps a
    truncated one, which is the difference between replayable and useless."""
    sink = LocalSink(tmp_path)
    for i in range(1, 6):
        sink.publish("flight-1.jsonl", b"".join(b'{"ev":"call"}\n' for _ in range(i)))
    sink.close()

    assert (tmp_path / "flight-1.jsonl").read_bytes().count(b"\n") == 5


def test_coalesces_to_newest_per_tape(tmp_path):
    """A burst of recorded calls must not become a burst of uploads. Superseded payloads are
    dropped by design; the count is reported, not hidden."""
    transport = SlowTransport(delay=0.05)
    sink = _BaseSink(transport)
    for i in range(1, 21):
        sink.publish("flight-1.jsonl", f"v{i}\n".encode())
    sink.close(timeout=5.0)

    assert sink.stats["deposits"] < 20, "no coalescing happened"
    assert sink.stats["dropped"] > 0
    assert sink.stats["deposits"] + sink.stats["dropped"] == 20, "a payload vanished unaccounted"
    assert transport.puts[-1][1] == b"v20\n", "the newest bytes were not the ones that landed"


def test_separate_tapes_are_never_coalesced_together(tmp_path):
    """Coalescing is per name. Two concurrent sessions must both survive."""
    sink = LocalSink(tmp_path)
    sink.publish("flight-a.jsonl", b"a\n")
    sink.publish("flight-b.jsonl", b"b\n")
    sink.close()

    assert (tmp_path / "flight-a.jsonl").read_bytes() == b"a\n"
    assert (tmp_path / "flight-b.jsonl").read_bytes() == b"b\n"


def test_transport_failure_never_reaches_the_caller():
    """A recorder must not break the app it observes; a sink must not break the recorder."""
    transport = FailingTransport()
    sink = _BaseSink(transport)
    sink.publish("flight-1.jsonl", b"x\n")   # must not raise
    sink.close(timeout=5.0)

    assert transport.attempts == 1
    assert sink.stats["failures"] == 1


def test_error_callback_sees_the_failure():
    """Swallowed is not the same as silent: an operator can be told."""
    seen = []
    sink = _BaseSink(FailingTransport(), on_error=lambda name, e: seen.append((name, str(e))))
    sink.publish("flight-1.jsonl", b"x\n")
    sink.close(timeout=5.0)

    assert seen == [("flight-1.jsonl", "bucket on fire")]


def test_deposit_log_stamps_the_digest_of_what_arrived(tmp_path):
    """Custody has to predate any dispute, so the digest is taken of the bytes as handed over,
    not of whatever the file has become by the time someone asks."""
    sink = LocalSink(tmp_path)
    payload = b'{"ev":"session"}\n'
    sink.publish("flight-1.jsonl", payload)
    sink.close()

    line = (tmp_path / "deposits.log").read_text(encoding="utf-8").strip()
    assert line == f"{digest(payload)}  {len(payload)}  flight-1.jsonl"


def test_partial_files_are_never_visible_under_the_tape_name(tmp_path):
    """A reader tailing the directory must never catch a half-written tape."""
    sink = LocalSink(tmp_path)
    sink.publish("flight-1.jsonl", b"x" * 100000)
    sink.close()

    assert not list(tmp_path.glob(".*partial")), "a partial file was left behind"
    assert (tmp_path / "flight-1.jsonl").read_bytes() == b"x" * 100000


def test_publish_after_close_is_ignored_not_an_error():
    """Interpreter shutdown ordering is not something the recorder should have to reason about."""
    sink = _BaseSink(SlowTransport(delay=0))
    sink.close()
    sink.publish("flight-1.jsonl", b"late\n")   # must not raise, must not hang


def test_satisfies_the_recorder_sink_protocol():
    """Conformance to `SessionSink`, checked against the real protocol when the recorder is
    installed. It is not `@runtime_checkable`, and making it so would be changing the library
    to suit its test — so conformance is checked the way a structural protocol actually defines
    it: the method exists, and its signature matches."""
    import inspect

    fr = pytest.importorskip("flight_recorder")
    expected = inspect.signature(fr.SessionSink.publish)
    actual = inspect.signature(LocalSink.publish)

    assert list(actual.parameters) == list(expected.parameters)
    assert [p.annotation for p in actual.parameters.values()] == \
           [p.annotation for p in expected.parameters.values()]
