"""`flight-serve`: the tape reader as an MCP server over a directory.

The tapes here are real: recorded by `install()` on the toy app in this process, or the
canonical fixtures every runtime records into spec/fixtures. A hand-written tape would test
the reader against the reader's own idea of a tape, which is the one thing it cannot be
trusted to have right. The last test does what a listing checker and a first user both do:
start the server over stdio, ask what it offers, and read a tape through it.
"""

import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path

import pytest

import flight_recorder as fr
from flight_recorder import record as record_mod
from flight_recorder import views
from flight_recorder.serve import TapeDirectory, build_server
from flight_recorder.views import TapeError
from tests import toy_tools
from tests.test_roundtrip import make_boundary

FIXTURES = Path(__file__).resolve().parent.parent / "spec" / "fixtures"


def _call(mcp, name, args):
    res = asyncio.run(mcp.call_tool(name, args))
    contents, structured = res if isinstance(res, tuple) else (res, None)
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    return structured if structured is not None else json.loads(contents[0].text)


@pytest.fixture
def recorded(tmp_path):
    """A directory holding one real tape: two finished calls and one that 'crashed'."""
    fr.install(make_boundary(), toy_tools, directory=str(tmp_path), enabled=True)
    toy_tools.greet("t@example.com", count=2)
    toy_tools.greet("u@example.com", count=1)
    sink = record_mod._recorder.start_call("doomed_tool", {"x": 1})
    sink.append({"k": "now", "v": "2026-01-01T00:00:00"})  # no finalize: the process died
    session = fr.session_path()
    fr.uninstall()
    yield tmp_path, session


def test_every_runtimes_fixture_reads_the_same_way():
    """The views read the frozen envelope, so each runtime's canonical tape indexes alike."""
    shapes = {}
    for tape in sorted(FIXTURES.glob("*-toy.jsonl")):
        view = views.index(tape.read_bytes())
        assert view["runtime"], tape.name
        shapes[tape.name] = tuple((c["fn"], c["events"], tuple(c["spans"]))
                                  for c in view["index"])
    runtimes = {name.split("-")[0] for name in shapes}
    assert runtimes == {"python", "node", "dotnet", "go", "java", "php"}
    # .NET's plain fixture records a third call (signup) the others do not; the two calls all
    # six share must index alike.
    plain = {s[:2] for n, s in shapes.items() if "-sem-" not in n}
    sem = {s for n, s in shapes.items() if "-sem-" in n}
    assert len(plain) == 1 and len(sem) == 1, shapes
    assert any(spans for _, _, spans in next(iter(sem)))


def test_a_recorded_tape_lists_with_its_unfinished_call(recorded):
    root, session = recorded
    (root / f"{session.stem}.call0.trace.jsonl").write_text('{"e":"H"}\n', encoding="utf-8")
    (root / "notes.jsonl").write_text('{"not":"a tape"}\n', encoding="utf-8")
    listing = TapeDirectory(root).listing()
    assert [t["name"] for t in listing["tapes"]] == [session.name]
    row = listing["tapes"][0]
    assert row["runtime"].startswith("python ") and row["unfinished_calls"] == 1
    assert "1 .jsonl file(s)" in listing["not_tapes"]


def test_the_listing_is_newest_first_and_says_what_the_cap_left_out(tmp_path):
    src = (FIXTURES / "python-toy.jsonl").read_bytes()
    for i in range(3):
        p = tmp_path / "prod" / f"t{i}.jsonl"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(src)
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    listing = TapeDirectory(tmp_path).listing(limit=2)
    assert [t["name"] for t in listing["tapes"]] == ["prod/t2.jsonl", "prod/t1.jsonl"]
    assert listing["count"] == 3 and listing["omitted"] == "1 older tape(s) not listed"


def test_a_missing_directory_is_an_empty_listing_not_an_error(tmp_path):
    listing = TapeDirectory(tmp_path / "flight").listing()
    assert listing["tapes"] == [] and "does not exist yet" in listing["note"]


def test_names_outside_the_directory_are_refused(recorded, tmp_path):
    root, _ = recorded
    outside = tmp_path.parent / "elsewhere.jsonl"
    outside.write_bytes((FIXTURES / "python-toy.jsonl").read_bytes())
    try:
        with pytest.raises(TapeError, match="outside"):
            TapeDirectory(root).read("../elsewhere.jsonl")
    finally:
        outside.unlink()
    with pytest.raises(TapeError, match="no tape named"):
        TapeDirectory(root).read("absent.jsonl")


def test_a_format_version_the_reader_does_not_implement_is_refused():
    tape = b'{"ev":"session","version":2,"started":"2026-01-01T00:00:00+00:00"}\n'
    with pytest.raises(TapeError, match="version 2"):
        views.index(tape)


def test_a_half_written_last_line_is_skipped_not_fatal():
    tape = (FIXTURES / "python-toy.jsonl").read_bytes().rstrip(b"\n") + b'\n{"ev":"call","fn":'
    assert views.index(tape)["calls"] == views.index((FIXTURES / "python-toy.jsonl")
                                                     .read_bytes())["calls"]


def test_the_tools_read_a_tape_from_index_to_one_value(recorded):
    root, session = recorded
    mcp = build_server(root)
    listing = _call(mcp, "list_tapes", {})
    name = listing["tapes"][0]["name"]

    idx = _call(mcp, "read_tape", {"name": name})
    assert [c["fn"] for c in idx["index"]] == ["greet", "greet"]
    assert idx["unfinished"][0]["fn"] == "doomed_tool" and idx["unfinished"][0]["events"] == 1

    one = _call(mcp, "read_tape", {"name": name, "call": 0})
    assert one["fn"] == "greet" and one["showing"].endswith(f"of {idx['index'][0]['events']}")
    kinds = {e["k"] for e in one["events"]}
    assert {"db", "now", "rand"} <= kinds

    n = next(e["n"] for e in one["events"] if "res" in e)
    whole = _call(mcp, "read_tape_value", {"name": name, "call": 0, "event": n})
    _, calls = fr.load_session(session)
    assert json.loads(whole["value"]) == calls[0]["events"][n]["res"]


def test_the_event_view_clips_and_reports_it():
    big = {"k": "fx", "fn": "m.f", "res": "x" * 1000}
    tape = (b'{"ev":"session","version":1,"python":"3.12"}\n'
            + json.dumps({"ev": "call", "fn": "t", "events": [big] * 3}).encode() + b"\n")
    one = views.call(tape, 0, "1:3")
    assert one["showing"] == "1:3 of 3" and one["events"][0]["res_bytes"] == 1000
    assert "2 payload(s) clipped" in one["clipped"] and "1 event(s)" in one["omitted"]
    capped = views.value(tape, 0, 0, limit=100)
    assert len(capped["value"]) == 100 and "not shown" in capped["truncated"]


def test_the_tools_are_declared_read_only(tmp_path):
    tools = asyncio.run(build_server(tmp_path).list_tools())
    assert {t.name for t in tools} == {"list_tapes", "read_tape", "read_tape_value"}
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)


def test_the_server_starts_over_stdio_and_reads_a_tape(tmp_path):
    """What a listing checker does, and then what a user does: spawn `flight-serve`,
    initialize, list the tools, read a tape."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (tmp_path / "go-toy.jsonl").write_bytes((FIXTURES / "go-toy.jsonl").read_bytes())

    async def ask():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "flight_recorder.serve", str(tmp_path)],
            env={**os.environ})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                read_back = await session.call_tool("read_tape", {"name": "go-toy.jsonl"})
                return (init.serverInfo, sorted(t.name for t in tools.tools),
                        read_back.structuredContent)

    info, tools, idx = asyncio.run(ask())
    assert info.name == "flight-recorder"
    assert info.version == importlib.metadata.version("xag-flight-recorder")
    assert tools == ["list_tapes", "read_tape", "read_tape_value"]
    assert idx["runtime"].startswith("go ") and idx["calls"] >= 1
