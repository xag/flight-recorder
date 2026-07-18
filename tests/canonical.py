"""The canonical fixture scenario — the same shape every runtime records into
`spec/fixtures/*-sem-toy.jsonl`, so the six tapes differ only in the runtime key and the
timestamps.

Kept apart from `toy_tools` on purpose. That module is Python's own app toy, shaped by what
this suite needs to test — `study_status` for invariants, `signup` for redaction,
`confirm_wipe` for an awaited round trip. This one is shaped by what the CROSS-RUNTIME fixture
has to prove, and the two pull in different directions. Conflating them is how a fixture
quietly drifts to suit a local test.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime

import flight_recorder as fr


class ToyError(Exception):
    """The toy's own error type. Its `args` are what a reviver rebuilds it from."""


# --- the outside world ----------------------------------------------------------------


async def store_get(key: str) -> dict:
    return {"name": "Alice", "x": 3}


async def store_set(key: str, value: dict) -> str:
    return "OK"


async def store_boom(key: str) -> None:
    raise ToyError(f"no such key: {key}", 42)


class _Snap:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self.exists = True
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class _Node:
    """The Firestore-shaped chained client: intermediate calls answer nothing, the terminal
    one does. Semantics-free — it answers every chain from one canned row."""

    def __getattr__(self, name: str):
        if name in ("collection", "document", "where", "limit", "order_by"):
            return lambda *a, **k: self
        if name == "get":
            return lambda: _Snap("alice", {"name": "Alice", "x": 3})
        if name == "stream":
            return lambda: [_Snap("0", {"name": "alpha", "x": 1}),
                            _Snap("1", {"name": "beta", "x": 2})]
        if name == "set":
            return lambda data: None
        raise AttributeError(name)


DB = _Node()

LIMIT = 3


# --- the canonical tool ---------------------------------------------------------------


async def enrol(user: str, password: str = "") -> dict:
    """A clock read that belongs to the CALL (it happens before the span opens), then a span
    enclosing a nested span, a point note, a span whose body raises — recorded with
    `outcome: "error"`, the exception caught by the caller and turned into a second note.

    Span data carries both a value marker (a datetime) and a value redaction must reach (a
    password), because both are shapes the fixture exists to freeze.
    """
    started = datetime.now()

    with fr.span("enrol", user=user, started=started, password=password):
        # A chained read, not an effect: the canonical scenario puts a `db` event inside a
        # span, which is the one enclosure a reader most wants to see and the one an fx-only
        # span never demonstrates.
        with fr.span("load_corpus"):
            snap = DB.collection("users").document(user).get()
        fr.note("corpus_read", found=snap.exists)

        try:
            with fr.span("register", password=password):
                await store_set(f"user:{user}", {"password": password})
                await store_boom(user)
        except (ToyError, fr.ReplayedEffectError) as e:
            # Two arms: the real type when recording (and when a reviver is declared), the
            # stand-in when replaying a tape whose boundary declares none.
            fr.note("registration_failed", why=_why(e))

        return {"user": user, "name": snap.to_dict()["name"]}


def _why(e: Exception) -> str:
    """The error's message, however it arrived.

    A revived ToyError carries `("no such key: alice", 42)`; the stand-in carries a rendered
    sentence. Both must produce the same note, or the fixture's testimony would depend on
    whether a reviver happened to be declared.
    """
    if isinstance(e, ToyError):
        return str(e.args[0])
    text = str(e)
    return text.split(": ", 1)[1] if text.startswith("ToyError: ") else text


async def greet(user: str) -> dict:
    """The canonical plain scenario: an effect, a chained read, all four random shapes, both
    clocks, and a chained write — every event kind the format defines, on one tape."""
    row = await store_get(user)

    list(DB.collection("users").where("x", ">", 0).stream())

    random.sample([0, 1, 2], 2)
    random.randbytes(4)
    random.random()
    random.randint(0, 99)
    at = datetime.now()
    time.perf_counter()

    DB.collection("users").document(user).set({"at": at})

    return {"name": row["name"]}


async def explode(user: str) -> None:
    """A raising effect produces both an `fx.err` and a non-null `call.error`."""
    await store_boom(user)


def plain_boundary() -> fr.Boundary:
    me = sys.modules[__name__]
    return fr.Boundary(
        effects=[(me, ["store_get", "store_set", "store_boom"])],
        chains=[fr.ChainTarget(me, "DB")],
        clock_modules=[me],
        random_modules=[me],
        constants=[(me, "LIMIT")],
        redact={"password": None},
        error_revivers={"ToyError": lambda args: ToyError(*args)},
    )


def sem_boundary() -> fr.Boundary:
    me = sys.modules[__name__]
    return fr.Boundary(
        effects=[(me, ["store_set", "store_boom"])],
        chains=[fr.ChainTarget(me, "DB")],
        clock_modules=[me],
        redact={"password": None},
        error_revivers={"ToyError": lambda args: ToyError(*args)},
    )
