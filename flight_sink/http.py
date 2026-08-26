"""Deposit tapes to an HTTP endpoint.

The transport for apps that should not be carrying storage of their own. An app that records
does not read its tapes back — a human does, later, from somewhere else — so durable storage on
the app is infrastructure it pays for and never uses. Sending them somewhere costs the app one
outbound request per coalesced deposit and nothing else.

Uses `urllib` from the standard library rather than an HTTP client, so this stays installable
with no dependencies: a sink is called on the hot path of whatever it is instrumenting, and
"add a dependency to the app you are observing" is a bad trade for a PUT.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Optional


class HttpTransport:
    """PUT each deposit to `{base}/t/{app}/{name}` with its digest in a header.

    The digest travels so the receiver can refuse bytes that changed in transit: the depositor
    says what it is sending, and a receiver that stores something else while answering 200 has
    quietly broken the only property a tape has.

    `timeout` is deliberately short. Publishing is best-effort and off the request path, and the
    recorder republishes the whole tape on the next call anyway — so a slow or sleeping receiver
    should cost a dropped deposit, never a piled-up queue of worker threads.
    """

    def __init__(self, base_url: str, app: str, token: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.app = app.strip("/")
        self.token = token
        self.timeout = timeout

    def put(self, name: str, data: bytes, sha256: str) -> None:
        req = urllib.request.Request(
            f"{self.base}/t/{self.app}/{name}",
            data=data,
            method="PUT",
            headers={
                "authorization": f"Bearer {self.token}",
                "content-type": "application/x-ndjson",
                "x-tape-sha256": sha256,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status >= 300:
                    raise RuntimeError(f"deposit rejected: HTTP {r.status}")
        except urllib.error.HTTPError as e:
            # Read the body: a 422 says the digest disagreed, which is worth seeing in a log
            # rather than being flattened into "the upload failed".
            detail = ""
            try:
                detail = e.read(512).decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(f"deposit rejected: HTTP {e.code} {detail}".strip()) from e


class HttpSink:
    """Tapes to an HTTP tape store. See `HttpTransport` and the base sink for the guarantees:
    never blocks the caller, coalesces bursts, drains at exit, stamps every deposit."""

    def __init__(self, base_url: str, app: str, token: str, timeout: float = 10.0, **kw):
        from flight_sink.sink import _BaseSink
        self._inner = _BaseSink(HttpTransport(base_url, app, token, timeout), **kw)

    def publish(self, name: str, data: bytes) -> None:
        self._inner.publish(name, data)

    def close(self, timeout: float = 10.0) -> None:
        self._inner.close(timeout)

    @property
    def stats(self) -> dict:
        return self._inner.stats
