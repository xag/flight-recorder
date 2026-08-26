"""The HTTP transport, against a real HTTP server rather than a mocked urlopen.

Mocking the client here would test my belief about what urllib sends. A throwaway server on a
loopback socket costs a few lines and tests what actually goes over the wire.
"""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from flight_sink import HttpSink
from flight_sink.http import HttpTransport

RECEIVED = []


class Handler(BaseHTTPRequestHandler):
    status = 200

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        RECEIVED.append({
            "path": self.path,
            "body": body,
            "auth": self.headers.get("authorization"),
            "sha": self.headers.get("x-tape-sha256"),
        })
        self.send_response(Handler.status)
        self.end_headers()
        self.wfile.write(b'{"ok":true}' if Handler.status < 300 else b'digest mismatch')

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    RECEIVED.clear()
    Handler.status = 200
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_deposits_to_the_app_scoped_path(server):
    HttpTransport(server, "dev-tools", "tok").put("flight-1.jsonl", b"x\n", "abc")
    assert RECEIVED[0]["path"] == "/t/dev-tools/flight-1.jsonl"


def test_sends_the_token_and_the_digest(server):
    payload = b'{"ev":"session"}\n'
    sha = hashlib.sha256(payload).hexdigest()
    HttpTransport(server, "a", "s3cret").put("flight-1.jsonl", payload, sha)

    assert RECEIVED[0]["auth"] == "Bearer s3cret"
    assert RECEIVED[0]["sha"] == sha
    assert RECEIVED[0]["body"] == payload


def test_a_rejected_deposit_raises_so_the_pump_counts_it(server):
    Handler.status = 422
    with pytest.raises(RuntimeError) as e:
        HttpTransport(server, "a", "tok").put("flight-1.jsonl", b"x\n", "wrong")
    assert "422" in str(e.value)


def test_sink_never_raises_at_the_caller_even_when_the_store_refuses(server):
    """The recorder calls publish on the hot path; a refusing store must cost a counter, not
    an exception."""
    Handler.status = 500
    sink = HttpSink(server, "a", "tok")
    sink.publish("flight-1.jsonl", b"x\n")     # must not raise
    sink.close(timeout=5.0)

    assert sink.stats["failures"] == 1


def test_sink_coalesces_before_it_reaches_the_wire(server):
    """The property that makes depositing per-call affordable: a burst of recorded calls is
    one request, not twenty."""
    sink = HttpSink(server, "a", "tok")
    for i in range(1, 21):
        sink.publish("flight-1.jsonl", f"v{i}\n".encode())
    sink.close(timeout=5.0)

    assert len(RECEIVED) < 20, "every publish hit the network"
    assert RECEIVED[-1]["body"] == b"v20\n", "the newest bytes were not the ones deposited"


def test_the_digest_sent_matches_the_bytes_sent(server):
    """The receiver refuses on mismatch, so a transport that computed it over different bytes
    would fail every deposit — check the pairing, not just the presence."""
    sink = HttpSink(server, "a", "tok")
    sink.publish("flight-1.jsonl", b'{"ev":"call"}\n')
    sink.close(timeout=5.0)

    last = RECEIVED[-1]
    assert last["sha"] == hashlib.sha256(last["body"]).hexdigest()
