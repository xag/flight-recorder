"""Off-box destinations for flight-recorder tapes: a directory, or a GCS bucket.

    import flight_recorder as fr
    from flight_sink import GcsSink

    fr.install(BOUNDARY, tools, sink=GcsSink("my-tapes-bucket", prefix="prod"))

Implements flight-recorder's `SessionSink` protocol (`publish(name, data)`) without importing
flight-recorder: the protocol is structural, so a sink satisfies it by shape. That keeps this
package installable next to any version of the recorder, and keeps the recorder free of cloud
dependencies.
"""

from flight_sink.http import HttpSink, HttpTransport
from flight_sink.sink import (
    GcsSink, LocalSink, Transport, digest,
)

__all__ = ["GcsSink", "HttpSink", "HttpTransport", "LocalSink", "Transport", "digest"]
