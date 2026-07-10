"""Redaction (issue #12): fields named in Boundary.redact are masked before anything is
written to the session file or handed to a sink, and replay re-applies the same rules to
its side of every comparison — so a redacted recording still round-trips bit-for-bit."""

import asyncio
import json
import os

import pytest

import flight_recorder as fr
from tests import toy_effects, toy_tools

SECRET = "s3cr3t-hunter2"


def make_boundary(redact) -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects, ["fetch_remote", "maybe_fail", "read_config",
                                "create_account"])],
        chains=[fr.ChainTarget(toy_tools, "DB")],
        clock_modules=[toy_tools],
        random_modules=[toy_tools],
        error_revivers={"ToyError": lambda args: toy_effects.ToyError(*args)},
        redact=redact,
    )


class ToyAdapter(fr.ReplayAdapter):
    def __init__(self, redact):
        self.boundary = make_boundary(redact)
        self.trace_root = os.path.dirname(toy_tools.__file__)

    def resolve(self, fn_name, feed):
        fn = getattr(toy_tools, fn_name)
        return getattr(fn, "__flight_wrapped__", fn)


class CaptureSink:
    def __init__(self):
        self.published = []

    def publish(self, name: str, data: bytes) -> None:
        self.published.append(data)


def record(tmp_path, redact, run, sink=None):
    fr.install(make_boundary(redact), toy_tools, directory=str(tmp_path),
               enabled=True, sink=sink)
    try:
        run()
        return fr.session_path()
    finally:
        fr.uninstall()


def test_secret_never_reaches_the_file_or_the_sink(tmp_path):
    sink = CaptureSink()
    session = record(tmp_path, {"password"},
                     lambda: asyncio.run(toy_tools.signup("t@example.com", SECRET)),
                     sink=sink)
    text = session.read_text(encoding="utf-8")
    assert SECRET not in text
    assert fr.REDACTED in text
    assert sink.published and all(SECRET not in d.decode("utf-8") for d in sink.published)
    # ...and the masking hit every surface: tool kwargs, tool result, effect kwargs,
    # effect result — visible in the parsed record, not just absent as a substring.
    call = json.loads(text.splitlines()[1])
    assert call["kwargs"]["password"] == fr.REDACTED
    assert call["result"]["password"] == fr.REDACTED
    assert call["result"]["account"]["password"] == fr.REDACTED
    fx_ev = next(e for e in call["events"] if e["k"] == "fx")
    assert fx_ev["kwargs"]["password"] == fr.REDACTED
    assert fx_ev["res"]["password"] == fr.REDACTED


def test_redacted_recording_round_trips(tmp_path):
    session = record(tmp_path, {"password"},
                     lambda: asyncio.run(toy_tools.signup("t@example.com", SECRET)))
    report = fr.replay_call(session, 0, ToyAdapter({"password"}), None)
    assert report.ok, (report.divergence, report.result_diff, report.write_divergences)
    assert report.replayed_result["password"] == fr.REDACTED


def test_literal_secret_born_inside_the_code_still_matches(tmp_path):
    # The replayed code rebuilds the secret raw (it is a literal, not a tape value); only
    # the replay-side re-scrub keeps the comparison meaningful.
    session = record(tmp_path, {"password"},
                     lambda: asyncio.run(toy_tools.call_home("t@example.com")))
    assert "hunter2-literal" not in session.read_text(encoding="utf-8")
    report = fr.replay_call(session, 0, ToyAdapter({"password"}), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_custom_transform_tokenizes_and_round_trips(tmp_path):
    # Idempotent by construction: replay re-applies the rule to already-tokenized values.
    tok = lambda v: v if str(v).startswith("tok:") else f"tok:{len(str(v))}"
    session = record(tmp_path, {"password": tok},
                     lambda: asyncio.run(toy_tools.signup("t@example.com", SECRET)))
    text = session.read_text(encoding="utf-8")
    assert SECRET not in text and f"tok:{len(SECRET)}" in text
    report = fr.replay_call(session, 0, ToyAdapter({"password": tok}), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_raising_transform_degrades_to_the_mask(tmp_path):
    def broken(v):
        raise RuntimeError("boom")
    session = record(tmp_path, {"password": broken},
                     lambda: asyncio.run(toy_tools.signup("t@example.com", SECRET)))
    text = session.read_text(encoding="utf-8")
    assert SECRET not in text and fr.REDACTED in text


def test_chain_write_field_masked_and_round_trips(tmp_path):
    # greet writes {"greeted_at": now}; the write's recorded args are masked, and the
    # replayed write is scrubbed before comparison AND before landing in feed.writes.
    session = record(tmp_path, {"greeted_at"},
                     lambda: toy_tools.greet("t@example.com", count=2))
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    write = next(e for e in call["events"] if e["k"] == "db" and "args" in e)
    assert write["args"][0]["greeted_at"] == fr.REDACTED
    report = fr.replay_call(session, 0, ToyAdapter({"greeted_at"}), None)
    assert report.ok, (report.divergence, report.result_diff, report.write_divergences)


def test_chain_read_field_masked_and_round_trips(tmp_path):
    # study_status never reads "name", so masking it inside the recorded rows changes
    # nothing the code computes — the doors stay redacted, the verdict stays MATCH.
    session = record(tmp_path, {"name"},
                     lambda: toy_tools.study_status("t@example.com", level=2))
    assert "alpha" not in session.read_text(encoding="utf-8")
    report = fr.replay_call(session, 0, ToyAdapter({"name"}), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_gate_sees_raw_values(tmp_path):
    # Redaction happens at write time, not at gate time: a gate may admit a call BY the
    # very field the recording then masks.
    admitted = []

    def gate(tool, kwargs):
        admitted.append(kwargs.get("password"))
        return kwargs.get("password") == SECRET

    fr.install(make_boundary({"password"}), toy_tools, directory=str(tmp_path),
               enabled=gate)
    try:
        asyncio.run(toy_tools.signup("t@example.com", SECRET))
        session = fr.session_path()
        assert admitted == [SECRET]
        assert SECRET not in session.read_text(encoding="utf-8")
    finally:
        fr.uninstall()


def test_no_rules_is_a_no_op(tmp_path):
    session = record(tmp_path, {}, lambda: asyncio.run(
        toy_tools.signup("t@example.com", SECRET)))
    assert SECRET in session.read_text(encoding="utf-8")
