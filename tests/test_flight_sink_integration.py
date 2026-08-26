"""The sink against the real recorder.

Everything in test_sink.py runs against a transport this repo wrote, which only ever proves
the sink behaves as I believe the recorder's contract requires. This file removes the belief:
it installs the actual recorder with a real boundary, makes real tool calls, and reads what
actually arrived in the sink directory.
"""

import pytest

fr = pytest.importorskip("flight_recorder")

from flight_sink import LocalSink, digest   # noqa: E402
from tests import sink_real_app as real_app     # noqa: E402


@pytest.fixture
def uninstalled():
    yield
    fr.uninstall()


def _boundary():
    return fr.Boundary(effects=[(real_app, ["fetch_rate"])])


def test_the_recorder_actually_feeds_the_sink(uninstalled, tmp_path):
    """The whole point, checked end to end: install with a sink, make calls, and the tape is
    off-box without anyone copying a file."""
    box, offbox = tmp_path / "box", tmp_path / "offbox"
    sink = LocalSink(offbox)
    fr.install(_boundary(), real_app, directory=str(box), enabled=True, sink=sink)

    real_app.quote("a@example.com")
    real_app.quote("b@example.com", pair="EURUSD")
    sink.close()

    name = fr.session_path().name
    landed = (offbox / name).read_bytes()

    assert landed == (box / name).read_bytes(), "the off-box tape differs from the one on disk"
    assert landed.count(b'"ev": "call"') == 2


def test_the_off_box_tape_replays(uninstalled, tmp_path):
    """The property that makes a sink worth having: what landed is not merely similar to the
    tape, it is a tape — the real code re-runs the recorded execution from it."""
    box, offbox = tmp_path / "box", tmp_path / "offbox"
    sink = LocalSink(offbox)
    fr.install(_boundary(), real_app, directory=str(box), enabled=True, sink=sink)
    real_app.quote("a@example.com")
    name = fr.session_path().name
    sink.close()
    fr.uninstall()

    class Adapter(fr.ReplayAdapter):
        boundary = _boundary()
        def resolve(self, fn_name, feed):
            return getattr(real_app, fn_name)

    report = fr.replay_call(offbox / name, 0, Adapter())

    assert report.ok, fr.format_report(report)


def test_the_name_the_sink_is_given_is_the_one_an_app_can_record(uninstalled, tmp_path):
    """`session_path().name` is the handle an app stamps onto something outside the recording
    (a support ticket, a bug report) to point back at the execution. It has to be the same
    string the sink filed the tape under, or the pointer dangles."""
    offbox = tmp_path / "offbox"
    sink = LocalSink(offbox)
    fr.install(_boundary(), real_app, directory=str(tmp_path / "box"), enabled=True, sink=sink)

    real_app.quote("a@example.com")
    handle = fr.session_path().name
    sink.close()

    assert (offbox / handle).exists(), "the handle an app would store does not resolve off-box"


def test_deposit_digest_matches_the_tape_that_landed(uninstalled, tmp_path):
    """Custody: the logged digest is of the bytes as deposited, so it still verifies later."""
    offbox = tmp_path / "offbox"
    sink = LocalSink(offbox)
    fr.install(_boundary(), real_app, directory=str(tmp_path / "box"), enabled=True, sink=sink)
    real_app.quote("a@example.com")
    name = fr.session_path().name
    sink.close()

    final = [ln for ln in (offbox / "deposits.log").read_text().strip().splitlines()
             if ln.endswith(name)][-1]
    assert final.split("  ")[0] == digest((offbox / name).read_bytes())


def test_nothing_is_published_when_the_gate_declines(uninstalled, tmp_path):
    """A gate that never says yes leaves no tape behind — and therefore no deposit either."""
    offbox = tmp_path / "offbox"
    sink = LocalSink(offbox)
    fr.install(_boundary(), real_app, directory=str(tmp_path / "box"),
               enabled=lambda fn, kwargs: kwargs.get("email") == "wanted@example.com",
               sink=sink)

    real_app.quote("ignored@example.com")
    sink.close()

    assert fr.session_path() is None
    assert not list(offbox.glob("flight-*.jsonl")), "a declined call still reached the sink"
