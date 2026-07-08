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
