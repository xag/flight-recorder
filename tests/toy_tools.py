"""Toy tool module for lib tests: one sync tool over a chained client + clock + random,
one async tool over effect functions. DB is deliberately semantics-free — it answers every
query from canned rows, which is all record/replay fidelity testing needs."""

from __future__ import annotations

import random
from datetime import datetime

import flight_recorder as fr

from tests import toy_effects as fx


class _Snap:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self.exists = True
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class _Node:
    def __init__(self, rows: list):
        self._rows = rows

    def __getattr__(self, name: str):
        if name in ("collection", "document", "where", "limit", "order_by"):
            return lambda *a, **k: self
        if name == "stream":
            return lambda: [_Snap(str(i), d) for i, d in enumerate(self._rows)]
        if name == "get":
            return lambda: _Snap("only", self._rows[0])
        if name == "set":
            return lambda data: None
        raise AttributeError(name)


class ToyDB(_Node):
    def __init__(self):
        super().__init__([{"name": "alpha", "x": 1}, {"name": "beta", "x": 2},
                          {"name": "gamma", "x": 3}])


DB = ToyDB()


def greet(email: str, count: int = 2) -> str:
    docs = list(DB.collection("users").document(email).collection("items")
                .where("x", ">", 0).stream())
    picked = random.sample(docs, count)
    names = sorted(d.to_dict()["name"] for d in picked)
    DB.collection("users").document(email).set({"greeted_at": datetime.now()})
    return f"{email} at {datetime.now().isoformat()}: " + ", ".join(names)


def outer(email: str) -> str:
    """A tool that calls another tool: the recorder must treat the pair as one call."""
    return greet(email, count=1)


def study_status(email: str, level: int = 1) -> dict:
    """The shape of the production bug that motivated invariants: `level` gates the deck,
    and at level 0 it excludes the whole corpus — so the status claims the corpus is
    finished while every item in it remains unstudied. The output is self-consistent; only
    a claim about every execution can call it wrong.

    The `coverage` division is the mutation-replay demo (issue #8): ToyDB always answers
    three rows, so no real recording can ever produce an empty corpus — the ZeroDivision
    is unreachable by recording and replaying, and reachable the moment the recorded rows
    are edited to []."""
    rows = list(DB.collection("users").document(email).collection("items")
                .where("x", ">", 0).stream())
    corpus = [r.to_dict() for r in rows]
    deck = [c for c in corpus if c["x"] <= level]
    return {"corpus": len(corpus), "deck": len(deck), "done": len(deck) == 0,
            "coverage": len(deck) / len(corpus)}


async def signup(email: str, password: str) -> dict:
    """Sensitive data on every surface redaction must cover: a tool kwarg, an effect
    kwarg, an effect result field, and a tool result field."""
    account = await fx.create_account(email, password=password)
    return {"email": email, "password": password, "account": account}


async def call_home(email: str) -> dict:
    """A literal secret born INSIDE the code, not carried from the kwargs: on replay it is
    reconstructed raw, so the comparison only matches if replay re-applies the rules."""
    return await fx.create_account(email, password="hunter2-literal")


async def confirm_wipe(email: str) -> dict:
    """A tool whose execution depends on a client-side round-trip it awaits."""
    ans = await fx.SESSION.elicit(f"really wipe {email}?")
    return {"email": email, "confirmed": ans["action"] == "accept", "n": ans["value"]}


async def enrol(email: str, password: str = "") -> dict:
    """The instrumented tool: the same kind of work as the others, but saying what it MEANT.

    Every shape the `sem` event kind can take is exercised here, because this is what the
    conformance fixture is recorded from — spans nested inside a span, a point note, a span
    whose body raises (recorded with `outcome: "error"`, and the exception propagates), span
    data carrying a value marker (a datetime) and a value redaction must reach (a password).

    Note what is NOT here: any claim the library checks. `register` is a span called
    "register" because the app says so. Nothing in flight-recorder knows or cares whether it
    registered anything — that judgement is a reader's, made against the raw events the span
    encloses, and the library's job is only to put the claim and the evidence on the same
    tape, in order.
    """
    with fr.span("enrol", email=email, started=datetime.now()):
        with fr.span("load_corpus"):
            rows = list(DB.collection("users").document(email).collection("items")
                        .where("x", ">", 0).stream())
        fr.note("corpus_read", rows=len(rows))

        account = None
        try:
            with fr.span("register", password=password):
                account = await fx.create_account(email, password=password)
                await fx.maybe_fail(99)  # raises: the span ends with outcome "error"
        except fx.ToyError as e:
            fr.note("registration_failed", why=e.args[0])

    return {"email": email, "account": account}


async def enrol_refactored(email: str, password: str = "") -> dict:
    """`enrol`, after somebody deleted one span.

    Byte for byte the same questions to the boundary, in the same order — so replay of an
    `enrol` tape against THIS function is green on every existing signal: same answers, same
    result, same events consumed. The only thing that changed is the code's account of what it
    was doing, and that is exactly the change a semantic divergence exists to name. It is not
    presumed to be a bug: this may be a refactor. The tape says what happened, not what to
    think about it.
    """
    with fr.span("enrol", email=email, started=datetime.now()):
        rows = list(DB.collection("users").document(email).collection("items")
                    .where("x", ">", 0).stream())          # the load_corpus span is gone
        fr.note("corpus_read", rows=len(rows))

        account = None
        try:
            with fr.span("register", password=password):
                account = await fx.create_account(email, password=password)
                await fx.maybe_fail(99)
        except fx.ToyError as e:
            fr.note("registration_failed", why=e.args[0])

    return {"email": email, "account": account}


LITERAL_TOKEN = "T" * 64  # a "credential" born inside the code, never passed in


async def testify(email: str) -> dict:
    """Testimony carrying a secret the call's kwargs never saw.

    The distinction matters: a secret arriving as a kwarg is already caught when the call's
    opening record is written, so it proves nothing about `sem`. Here the tape's only chance to
    catch it is the semantic event itself — which is precisely the case that shows a span's
    `data` is guarded like any other payload rather than slipping past on its way out.
    """
    with fr.span("register", token=LITERAL_TOKEN):
        return {"email": email}


async def summing(email: str) -> str:
    """A span whose body raises and does NOT catch it. The `end` is written anyway, carrying
    `outcome: "error"`, and the exception goes on its way untouched."""
    with fr.span("summing", email=email):
        return await fx.maybe_fail(99)


async def awaited(email: str) -> dict:
    """The same context manager, awaited. One object, both protocols — because an app whose
    domain acts are async should not have to instrument them differently."""
    async with fr.span("awaited", mode="async"):
        return await fx.fetch_remote(email)


async def remote_sum(email: str, a: str, b: str) -> dict:
    x = await fx.fetch_remote(a)
    y = await fx.fetch_remote(b)
    try:
        note = await fx.maybe_fail(x["v"] // 10 + y["v"] // 10)
    except fx.ToyError as e:
        note = f"failed: {e.args[0]} n={e.args[1]}"
    return {"email": email, "sum": x["v"] + y["v"], "cfg": fx.read_config("mode"),
            "note": note}
