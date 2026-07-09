"""Toy tool module for lib tests: one sync tool over a chained client + clock + random,
one async tool over effect functions. DB is deliberately semantics-free — it answers every
query from canned rows, which is all record/replay fidelity testing needs."""

from __future__ import annotations

import random
from datetime import datetime

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


async def remote_sum(email: str, a: str, b: str) -> dict:
    x = await fx.fetch_remote(a)
    y = await fx.fetch_remote(b)
    try:
        note = await fx.maybe_fail(x["v"] // 10 + y["v"] // 10)
    except fx.ToyError as e:
        note = f"failed: {e.args[0]} n={e.args[1]}"
    return {"email": email, "sum": x["v"] + y["v"], "cfg": fx.read_config("mode"),
            "note": note}
