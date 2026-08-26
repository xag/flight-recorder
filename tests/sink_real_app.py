"""A real (tiny) instrumented app: an effect module and a tool module over it.

Deliberately not a stub of the recorder — the integration test installs the actual recorder
against this and reads what actually lands in the sink. A sink verified only against a
hand-rolled transport is a sink verified against my belief about the recorder's contract.
"""

from __future__ import annotations

# --- the nondeterminism boundary -------------------------------------------------------

def fetch_rate(pair: str) -> float:
    """The outside world. Never actually called during replay."""
    return {"EURUSD": 1.09}.get(pair, 0.0)


# --- the tools -------------------------------------------------------------------------

def quote(email: str, pair: str = "EURUSD") -> dict:
    rate = fetch_rate(pair)
    return {"pair": pair, "rate": rate, "for": email}
