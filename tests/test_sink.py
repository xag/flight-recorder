"""Session sinks (issue #4): a recording is published off-box as it grows, so it can be
retrieved without filesystem access to the machine that made it. The sink is best-effort —
it never breaks, delays past its own slowness, or alters the call being recorded."""

import asyncio
import json

import pytest

import flight_recorder as fr
from tests import toy_tools
from tests.test_roundtrip import make_boundary


class MemorySink:
    """The whole protocol: publish(name, data). An S3 sink is this, with put_object."""

    def __init__(self):
        self.published = []

    def publish(self, name, data):
        self.published.append((name, data))

    @property
    def last(self):
        return self.published[-1][1]


@pytest.fixture
def uninstalled():
    yield
    fr.uninstall()


def _records(data: bytes):
    return [json.loads(l) for l in data.decode("utf-8").splitlines() if l.strip()]


def test_header_is_published_at_install_then_each_completed_call(uninstalled, tmp_path):
    sink = MemorySink()
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True, sink=sink)

    assert len(sink.published) == 1  # the session header, before any call
    assert _records(sink.last)[0]["ev"] == "session"

    toy_tools.greet("t@example.com", count=2)
    assert len(sink.published) == 2

    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))
    assert len(sink.published) == 3


def test_published_bytes_are_the_whole_session_and_parse_as_one(uninstalled, tmp_path):
    sink = MemorySink()
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True, sink=sink)
    toy_tools.greet("t@example.com", count=2)
    asyncio.run(toy_tools.remote_sum("t@example.com", "ab", "cd"))

    name, data = sink.published[-1]
    assert name == fr.session_path().name
    assert data == fr.session_path().read_bytes()

    recs = _records(data)
    assert recs[0]["ev"] == "session"
    assert [r["fn"] for r in recs[1:]] == ["greet", "remote_sum"]


def test_a_published_session_replays_without_ever_touching_the_local_file(
        uninstalled, tmp_path):
    # The point of the sink: what lands remotely is a complete, replayable recording.
    sink = MemorySink()
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True, sink=sink)
    asyncio.run(toy_tools.remote_sum("t@example.com", "abc", "wxyz"))
    published = sink.last
    fr.uninstall()

    elsewhere = tmp_path / "retrieved" / "from-the-sink.jsonl"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(published)

    from tests.test_roundtrip import ToyAdapter
    report = fr.replay_call(elsewhere, 0, ToyAdapter(), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_a_raising_sink_never_breaks_the_call(uninstalled, tmp_path):
    class BrokenSink:
        def publish(self, name, data):
            raise OSError("the bucket is on fire")

    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True,
               sink=BrokenSink())
    out = toy_tools.greet("t@example.com", count=2)  # must not raise

    assert "t@example.com" in out
    _, calls = fr.load_session(fr.session_path())  # and the local recording is intact
    assert [c["fn"] for c in calls] == ["greet"]


def test_a_gated_sink_publishes_nothing_until_a_call_is_admitted(uninstalled, tmp_path):
    sink = MemorySink()
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), sink=sink,
               enabled=lambda fn, kwargs: kwargs.get("email") == "wanted@example.com")

    toy_tools.greet("ignored@example.com", count=2)
    assert sink.published == []  # not even a header: there is no session yet

    toy_tools.greet("wanted@example.com", count=2)
    assert len(sink.published) == 2  # header, then the admitted call
    assert [r["fn"] for r in _records(sink.last)[1:]] == ["greet"]


def test_no_sink_is_the_default_and_costs_nothing(uninstalled, tmp_path):
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    toy_tools.greet("t@example.com", count=2)
    _, calls = fr.load_session(fr.session_path())
    assert len(calls) == 1
