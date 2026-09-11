"""flight-serve — an MCP server that reads a directory of tapes.

`install()` writes tapes to a directory (`flight/` by default) and `LocalSink` deposits them in
one; either way a person debugging reaches for the same three questions, in order: which
tapes are there, what happened in this one, and what exactly crossed the boundary at this
step. This server answers them over MCP, so the reader can be a model in any MCP client:

    list_tapes()                         the tapes, newest first, with what recorded them
    read_tape(name)                      one line per call, plus calls that never finished
    read_tape(name, call=N)              every event of call N, payloads clipped
    read_tape_value(name, call, event)   one payload, whole

The views are `flight_recorder.views`, which read the frozen envelope of the tape spec, so a
tape recorded by any of the six runtimes reads the same here. That is also why there is one
server and not six: an MCP client launches a process, and nothing about reading a tape depends
on the language of the app that recorded it.

Read-only, and confined to the directory it was given: a name that resolves outside it is
refused, and nothing is ever written — not even this server's own tape, since a recorder
pointed at the directory it serves would deposit into the evidence it is reading.

The server needs the `mcp` SDK, which the library does not: `pip install
"xag-flight-recorder[serve]"`. The views themselves are stdlib only.
"""

# No `from __future__ import annotations` here: FastMCP 1.10 (the [serve] floor) inspects the
# tool functions' annotations as classes, and a string annotation crashes it at startup.
import argparse
import json
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Optional

from flight_recorder import views
from flight_recorder.views import TapeError, runtime

# A listing is a table of contents; past this it stops being read and starts being scrolled.
LIST_LIMIT = 200


def _is_tape_file(p: Path) -> bool:
    # `.callN.trace.jsonl` is a replay's variable trace, written beside the tape it replayed;
    # it is JSONL but not a tape, and listing it would double every tape that was ever replayed.
    return p.is_file() and p.suffix == ".jsonl" and not p.name.endswith(".trace.jsonl")


def _header(p: Path) -> Optional[dict]:
    """The session header, read from the first line only; None for a file that is not a tape."""
    try:
        with p.open("rb") as f:
            first = json.loads(f.readline() or b"null")
    except (OSError, json.JSONDecodeError):
        return None
    return first if isinstance(first, dict) and first.get("ev") == "session" else None


def _sidecars(tape: Path) -> list[Path]:
    """The `.inflight` sidecars of calls that never finished: the process died mid-call."""
    return sorted(tape.parent.glob(f"{tape.stem}.call*.inflight"))


class TapeDirectory:
    """A directory of tapes, read-only. Every name it hands out or accepts is a path relative
    to the root, with forward slashes, so a sink's prefix subdirectories read the same on
    every platform."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, name: str) -> Path:
        p = (self.root / name).resolve()
        if not p.is_relative_to(self.root):
            raise TapeError(f"{name!r} is outside the tape directory")
        if not _is_tape_file(p):
            raise TapeError(f"no tape named {name!r} in {self.root} — list_tapes shows what there is")
        return p

    def listing(self, limit: int = LIST_LIMIT) -> dict:
        if not self.root.is_dir():
            return {"directory": str(self.root), "tapes": [], "count": 0,
                    "note": "the directory does not exist yet; a recorder creates it on its first call"}
        files = sorted((p for p in self.root.rglob("*.jsonl") if _is_tape_file(p)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        rows, not_tapes = [], 0
        for p in files:
            header = _header(p)
            if header is None:
                not_tapes += 1
                continue
            if len(rows) >= limit:
                continue
            st = p.stat()
            row: dict[str, Any] = {
                "name": p.relative_to(self.root).as_posix(),
                "bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                                    .isoformat(timespec="seconds"),
                "started": header.get("started"),
                "runtime": runtime(header),
            }
            if n := len(_sidecars(p)):
                row["unfinished_calls"] = n
            rows.append(row)
        tapes = len(files) - not_tapes
        out: dict[str, Any] = {"directory": str(self.root), "count": tapes, "tapes": rows}
        if tapes > len(rows):
            out["omitted"] = f"{tapes - len(rows)} older tape(s) not listed"
        if not_tapes:
            out["not_tapes"] = f"{not_tapes} .jsonl file(s) with no session header skipped"
        return out

    def read(self, name: str) -> bytes:
        return self._resolve(name).read_bytes()

    def unfinished(self, name: str) -> list[dict]:
        """What each crashed call managed to record before the process died."""
        out = []
        for sidecar in _sidecars(self._resolve(name)):
            try:
                lines = sidecar.read_text(encoding="utf-8").splitlines()
                hdr = json.loads(lines[0]) if lines else {}
                out.append({"sidecar": sidecar.name, "fn": hdr.get("fn"),
                            "started": hdr.get("started"),
                            "events": max(len(lines) - 1, 0)})
            except (OSError, json.JSONDecodeError) as e:
                out.append({"sidecar": sidecar.name, "unreadable": str(e)})
        return out


def build_server(root: Path | str = "flight", host: str = "127.0.0.1", port: int = 8000):
    """The server, unstarted. Tests call the tools on it directly; `serve` runs it."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    tapes = TapeDirectory(root)
    mcp = FastMCP(
        "flight-recorder", host=host, port=port,
        instructions=(
            f"Tapes recorded by flight-recorder, read from {tapes.root}. A tape is one process's "
            "recorded session: each tool call the app made, with every nondeterministic input "
            "that crossed its boundary (effects, database reads, clock, randomness) and the "
            "app's own semantic spans. Start with list_tapes, then read_tape(name) for one line "
            "per call, read_tape(name, call=N) for that call's events, and read_tape_value for a "
            "payload the event view clipped. Read-only."))
    # FastMCP takes no version and reports the SDK's own, which tells a client nothing about
    # which reader it is talking to. The low-level server carries the field; fill it.
    try:
        mcp._mcp_server.version = version("xag-flight-recorder")
    except PackageNotFoundError:
        pass
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read_only)
    def list_tapes() -> dict[str, Any]:
        """List the tapes in the directory, newest first: each one's name (pass it to
        read_tape), size, when it was last written, when its session started, which runtime
        recorded it, and how many calls it left unfinished (the process died mid-call).
        Capped at 200 rows; the cap says how many it left out."""
        return tapes.listing()

    @mcp.tool(annotations=read_only)
    def read_tape(name: str, call: int = -1, events: str = "") -> dict[str, Any]:
        """Read one tape. Without `call`: the session (start time, runtime, declared
        constants) and one line per recorded call with its function, duration, error, event
        count and semantic span names, plus any calls that never finished — start here.
        With `call=N` (0-based, from that index): every event of call N in order, each
        payload clipped to 240 characters, which shows what crossed the boundary and in
        what order. `events` narrows call N to a range like "10:40". For one clipped
        payload in full, use read_tape_value."""
        blob = tapes.read(name)
        if call < 0:
            view = views.index(blob)
            if gone := tapes.unfinished(name):
                view["unfinished"] = gone
            return view
        return views.call(blob, call, events or None)

    @mcp.tool(annotations=read_only)
    def read_tape_value(name: str, call: int, event: int, field: str = "res") -> dict[str, Any]:
        """One payload from one event of one call, whole: the detail behind a value
        read_tape clipped. `call` and `event` are the indexes read_tape shows; `field` is
        res (what the boundary answered), args, kwargs, data (a semantic span's data) or
        err. Capped at 20,000 characters, and the cap says what it cut."""
        return views.value(tapes.read(name), call, event, field)

    return mcp


def serve(root: Path | str = "flight", transport: str = "stdio",
          host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the server until the client hangs up (stdio) or Ctrl-C (HTTP)."""
    build_server(root, host=host, port=port).run(transport=transport)  # type: ignore[arg-type]


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="flight-serve",
        description="An MCP server that reads a directory of flight-recorder tapes.")
    ap.add_argument("dir", nargs="?", default="flight",
                    help="the directory holding the tapes (default: flight, where install() "
                         "writes them)")
    ap.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1", help="for the HTTP transports")
    ap.add_argument("--port", type=int, default=8000, help="for the HTTP transports")
    args = ap.parse_args(argv)
    try:
        import mcp  # noqa: F401
    except ImportError:
        print('flight-serve needs the MCP SDK: pip install "xag-flight-recorder[serve]"',
              file=sys.stderr)
        return 1
    serve(args.dir, transport=args.transport, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
