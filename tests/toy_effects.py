"""Toy effect module for lib tests: the 'external world' as plain functions."""

from __future__ import annotations


class ToyError(Exception):
    pass


async def fetch_remote(key: str) -> dict:
    return {"key": key, "v": len(key) * 10}


async def maybe_fail(n: int) -> str:
    if n > 5:
        raise ToyError("kaput", n)
    return "fine"


def read_config(name: str) -> str:
    return f"cfg:{name}"


async def create_account(email: str, password: str = "") -> dict:
    return {"id": f"acct-{len(email)}", "email": email, "password": password}


class ToySession:
    """Stands in for an MCP session/context object: a client round-trip the tool awaits
    (elicitation, sampling, a ui/* response). Declared as a method effect, the round-trip
    is an input like any other; `self` is identity, not input."""

    async def elicit(self, prompt: str) -> dict:
        return {"action": "accept", "value": len(prompt)}


SESSION = ToySession()
