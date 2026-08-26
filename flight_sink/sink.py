"""Off-box destinations for flight-recorder tapes.

A recorder hands a sink the whole tape, by name, every time the tape grows:

    sink.publish("flight-20260721-101500-641.jsonl", b'{"ev":"session"...}\\n...')

Three properties of that contract shape everything here.

**It is called on the hot path, holding the recorder's write lock.** In an async server that is
the event-loop thread, so a `publish` that blocks on network I/O stalls every concurrent
request, not just the recorded one. Nothing in this module does I/O in `publish`; it hands the
bytes to a worker thread and returns.

**It is cumulative and idempotent.** Each call carries the tape *so far* under a stable name,
so deposits are overwrites, not appends, and a deposit that never lands costs nothing as long
as a later one does. That is what lets the worker COALESCE: when several versions of one tape
are queued, only the newest is worth uploading, and dropping the rest is not data loss.

**It is best-effort by contract.** The recorder ignores exceptions from `publish` — a recorder
must not break the app it observes. Sinks here therefore swallow their own transport failures
and count them, rather than letting one bad upload become the app's problem.

What this module deliberately does NOT do:

- **Re-check `forbid`.** The recorder guards every line *before* the bytes exist (its `_guard`
  runs inside `_write`, ahead of the sink's mirror), so a forbidden value cannot reach a sink
  in-process; re-running the same patterns over the same bytes would be duplication with no
  new information. That check belongs at the door of a store receiving tapes from recorders it
  does not control, which is a different trust boundary from this one.
- **Interpret a tape.** Nothing here parses a line, knows what a field means, or cares. A sink
  moves bytes and stamps their digest.
"""

from __future__ import annotations

import atexit
import hashlib
import threading
from pathlib import Path
from typing import Callable, Optional, Protocol


def digest(data: bytes) -> str:
    """The deposit's identity: sha256, hex, no prefix.

    Stamped when the bytes arrive rather than computed later from a stored object, because the
    point of a digest is to say what was deposited *at that moment*. A hash taken afterwards
    attests to whatever the file has become since.
    """
    return hashlib.sha256(data).hexdigest()


class Transport(Protocol):
    """Where bytes actually go. The one thing that differs between a bucket and a directory."""

    def put(self, name: str, data: bytes, sha256: str) -> None: ...


class _Pump:
    """A coalescing worker thread: newest-wins per name, one transport call at a time.

    Holds at most one pending payload per tape, so a burst of recorded calls collapses into a
    single upload of the latest bytes instead of one upload per call. `dropped` counts the
    payloads superseded before they were sent — expected and healthy, not an error.
    """

    def __init__(self, transport: Transport, on_error: Optional[Callable[[str, BaseException], None]] = None):
        self._transport = transport
        self._on_error = on_error
        self._pending: dict[str, bytes] = {}
        self._cv = threading.Condition()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self.deposits = 0   # payloads actually handed to the transport
        self.dropped = 0    # payloads superseded by a newer version of the same tape
        self.failures = 0   # transport calls that raised

    def _ensure_thread(self) -> None:
        if self._thread is None:
            # Daemon: a tape is diagnostics, and a half-written deposit must never be the
            # reason a process refuses to exit. `close()` is the ordered path; atexit calls it.
            self._thread = threading.Thread(target=self._run, name="flight-sink", daemon=True)
            self._thread.start()

    def offer(self, name: str, data: bytes) -> None:
        with self._cv:
            if self._closed:
                return
            if name in self._pending:
                self.dropped += 1
            self._pending[name] = data
            self._ensure_thread()
            self._cv.notify()

    def _next(self) -> Optional[tuple[str, bytes]]:
        with self._cv:
            while not self._pending and not self._closed:
                self._cv.wait()
            if not self._pending:
                return None
            name = next(iter(self._pending))
            return name, self._pending.pop(name)

    def _run(self) -> None:
        while True:
            item = self._next()
            if item is None:
                return
            name, data = item
            try:
                self._transport.put(name, data, digest(data))
                self.deposits += 1
            except BaseException as e:  # noqa: BLE001 - see module docstring: best-effort
                self.failures += 1
                if self._on_error is not None:
                    try:
                        self._on_error(name, e)
                    except Exception:
                        pass

    def close(self, timeout: float = 10.0) -> None:
        """Drain what is queued, then stop. Called at exit so the final deposit lands.

        The last `publish` of a session is the only one that carries the complete tape, so a
        process that exits without draining keeps whatever partial version happened to be
        uploaded last. That is the difference between a tape you can replay and a tape that
        stops mid-session.
        """
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self._cv.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)


class _BaseSink:
    """Shared plumbing: hand off, never block, drain at exit."""

    def __init__(self, transport: Transport,
                 on_error: Optional[Callable[[str, BaseException], None]] = None):
        self._pump = _Pump(transport, on_error)
        atexit.register(self._pump.close)

    def publish(self, name: str, data: bytes) -> None:
        self._pump.offer(name, data)

    def close(self, timeout: float = 10.0) -> None:
        self._pump.close(timeout)

    @property
    def stats(self) -> dict:
        return {"deposits": self._pump.deposits,
                "dropped": self._pump.dropped,
                "failures": self._pump.failures}


class _DirTransport:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, name: str, data: bytes, sha256: str) -> None:
        # Write-then-replace: a reader tailing the directory never sees a torn tape.
        tmp = self.dir / f".{name}.partial"
        tmp.write_bytes(data)
        tmp.replace(self.dir / name)
        with (self.dir / "deposits.log").open("a", encoding="utf-8") as f:
            f.write(f"{sha256}  {len(data)}  {name}\n")


class LocalSink(_BaseSink):
    """Tapes to a directory, with a deposit log. The reference implementation.

    Useful in its own right (a mounted volume outlives the process that wrote it) and it is what
    the tests run against: the coalescing, draining and digest-stamping under test here are the
    parts that are wrong-or-right independently of any cloud client.
    """

    def __init__(self, directory: str | Path, **kw):
        super().__init__(_DirTransport(directory), **kw)


class _GcsTransport:
    def __init__(self, bucket: str, prefix: str = "", client: object = None):
        if client is None:
            from google.cloud import storage  # imported late: the local sink needs no cloud dep
            client = storage.Client()
        self._bucket = client.bucket(bucket) if hasattr(client, "bucket") else client
        self.prefix = prefix.strip("/")

    def put(self, name: str, data: bytes, sha256: str) -> None:
        key = f"{self.prefix}/{name}" if self.prefix else name
        blob = self._bucket.blob(key)
        # The digest travels with the object rather than in a side table, so a deposit is
        # self-describing to anyone who lists the bucket without this library.
        blob.metadata = {"sha256": sha256}
        blob.upload_from_string(data, content_type="application/x-ndjson")


class GcsSink(_BaseSink):
    """Tapes to a Google Cloud Storage bucket.

    Turn on **object versioning** for the bucket. Deposits are overwrites (the recorder
    republishes the whole tape as it grows), so without versioning the bucket holds only the
    latest state of each tape and an overwrite is indistinguishable from a rewrite. Versioning
    is what makes the history of a tape auditable, and it belongs to the storage layer — a
    sink that tried to reimplement it would be keeping its own worse copy of a solved problem.

    `prefix` separates sources sharing a bucket (an environment, a machine, a service).
    """

    def __init__(self, bucket: str, prefix: str = "", client: object = None, **kw):
        super().__init__(_GcsTransport(bucket, prefix, client), **kw)
