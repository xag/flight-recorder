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


def make_boundary(redact, forbid=(), scrub=None) -> fr.Boundary:
    return fr.Boundary(
        effects=[(toy_effects, ["fetch_remote", "maybe_fail", "read_config",
                                "create_account"])],
        chains=[fr.ChainTarget(toy_tools, "DB")],
        clock_modules=[toy_tools],
        random_modules=[toy_tools],
        error_revivers={"ToyError": lambda args: toy_effects.ToyError(*args)},
        redact=redact,
        scrub=scrub,
        forbid=forbid,
    )


class ToyAdapter(fr.ReplayAdapter):
    def __init__(self, redact, scrub=None):
        self.boundary = make_boundary(redact, scrub=scrub)
        self.trace_root = os.path.dirname(toy_tools.__file__)

    def resolve(self, fn_name, feed):
        fn = getattr(toy_tools, fn_name)
        return getattr(fn, "__flight_wrapped__", fn)


class CaptureSink:
    def __init__(self):
        self.published = []

    def publish(self, name: str, data: bytes) -> None:
        self.published.append(data)


def record(tmp_path, redact, run, sink=None, forbid=(), scrub=None):
    fr.install(make_boundary(redact, forbid, scrub), toy_tools, directory=str(tmp_path),
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


# --- forbid: the tripwire that backstops redaction (issue #17) ----------------------------
#
# Redaction protects the fields you named. `forbid` states the property it cannot: THIS TAPE
# CARRIES NO CREDENTIAL — checked against the fully-redacted line the recorder is about to
# write, failing loud instead of writing. Match a SHAPE, not a value: a secret you can
# enumerate you could already have redacted.

KEY = "sk-live-9f8e7d6c5b4a39281706"
KEY_SHAPE = r"sk-live-[0-9a-f]+"


def nothing_on_disk_carries(tmp_path, secret):
    """The property the whole feature exists for, asserted the only way worth asserting it:
    over every byte the recorder left behind — session file, crash sidecar, anything."""
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert secret not in p.read_text(encoding="utf-8", errors="ignore"), p


def test_a_forgotten_field_is_caught_instead_of_leaked(tmp_path):
    # The headline failure: nobody declared `password`. Today the tape leaks and nothing
    # tells you. With the tripwire it is a noisy failure at record time, and nothing is
    # written — not even the sidecar, which is refused before the file is opened.
    sink = CaptureSink()
    with pytest.raises(fr.ForbiddenValue):
        record(tmp_path, set(), lambda: asyncio.run(toy_tools.signup("t@x.com", KEY)),
               sink=sink, forbid=[KEY_SHAPE])
    nothing_on_disk_carries(tmp_path, KEY)
    assert all(KEY not in d.decode("utf-8") for d in sink.published)


def test_a_rule_that_stopped_matching_is_caught(tmp_path):
    # The silent one: the rule is declared, spelled for a field that no longer exists (it was
    # renamed, or it was always a typo). It masks nothing, and says nothing.
    with pytest.raises(fr.ForbiddenValue):
        record(tmp_path, {"passwrd"}, lambda: asyncio.run(toy_tools.signup("t@x.com", KEY)),
               forbid=[KEY_SHAPE])
    nothing_on_disk_carries(tmp_path, KEY)


def test_a_value_no_field_name_could_ever_reach_leaks_past_a_correct_rule(tmp_path):
    # The structural gap, demonstrated before it is closed — this is the test that says WHY
    # `forbid` has to exist. `remote_sum` declares `a` and the recorder masks it, faithfully.
    # Then the tool hands the very same value to fetch_remote POSITIONALLY, where it lands in
    # the event's `args` with no name on it — and redaction, being field-name driven, cannot
    # follow it there. The rule did exactly what it was told, and the tape leaks anyway.
    session = record(tmp_path, {"a"},
                     lambda: asyncio.run(toy_tools.remote_sum("t@x.com", KEY, "k")))
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    assert call["kwargs"]["a"] == fr.REDACTED          # the named field: masked
    fx_ev = next(e for e in call["events"] if e["k"] == "fx")
    assert fx_ev["args"] == [KEY]                      # the nameless copy: on the tape, raw


def test_a_value_no_field_name_could_ever_reach_is_still_caught(tmp_path):
    # ...and the tripwire reads the line the recorder is about to write, so it sees what no
    # field name could. Same call, same rule, now refused.
    with pytest.raises(fr.ForbiddenValue):
        record(tmp_path, {"a"}, lambda: asyncio.run(toy_tools.remote_sum("t@x.com", KEY, "k")),
               forbid=[KEY_SHAPE])
    nothing_on_disk_carries(tmp_path, KEY)


def test_the_tripwire_is_silent_when_redaction_did_its_job(tmp_path):
    # It judges the tape AFTER scrubbing, so a masked secret is not a hit. A tripwire that
    # fired on a correctly-redacted recording would be turned off within the week.
    session = record(tmp_path, {"password"},
                     lambda: asyncio.run(toy_tools.signup("t@x.com", KEY)),
                     forbid=[KEY_SHAPE])
    nothing_on_disk_carries(tmp_path, KEY)
    assert fr.REDACTED in session.read_text(encoding="utf-8")
    report = fr.replay_call(session, 0, ToyAdapter({"password"}), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_the_failure_names_the_rule_and_never_the_secret(tmp_path):
    # This message goes to a log, a stack trace, an issue. A tripwire that quotes the
    # credential it caught has become the leak it was there to prevent.
    with pytest.raises(fr.ForbiddenValue) as exc:
        record(tmp_path, set(), lambda: asyncio.run(toy_tools.signup("t@x.com", KEY)),
               forbid=[KEY_SHAPE])
    assert KEY not in str(exc.value)
    assert KEY_SHAPE in str(exc.value)


def test_a_forbidden_value_in_the_header_fails_the_install(tmp_path):
    # The header is a write like any other: constants and extras land on the tape too, and
    # they land before any call is recorded. An install that cannot open a safe session must
    # not open an unsafe one — and must leave nothing patched behind.
    boundary = make_boundary({}, forbid=[KEY_SHAPE])
    boundary.header_extras = {"build": lambda: f"built-with-{KEY}"}
    with pytest.raises(fr.ForbiddenValue):
        fr.install(boundary, toy_tools, directory=str(tmp_path), enabled=True)
    try:
        nothing_on_disk_carries(tmp_path, KEY)
        assert fr.hook.mode == "off"
        assert not hasattr(toy_tools.greet, "__flight_wrapped__")  # rolled back
    finally:
        fr.uninstall()


def test_forbid_is_opt_in(tmp_path):
    # Declaring no tripwire records exactly as before: this is an assertion an app makes,
    # not a policy the recorder imposes on every boundary that already exists.
    session = record(tmp_path, set(), lambda: asyncio.run(toy_tools.signup("t@x.com", KEY)))
    assert KEY in session.read_text(encoding="utf-8")


# --- scrub: value-level redaction, for the secret no field name can reach (issue #22) -----
#
# The three layers are complements, not substitutes. `redact` masks by NAME and protects the
# fields you thought of. `forbid` asserts the tape carries no credential and, when one gets
# through, refuses to write — which is the correct answer and the end of the road: the call
# cannot be recorded at all. `scrub` masks by VALUE, sweeping every leaf string wherever it
# sits, so the positional argument, the interpolated key and the sentence of prose are masked
# and the call is still recorded, still replayable. An assertion is not a fix; this is the fix.

TOKEN = "tk-4f2a9c81"
# Idempotent by construction, which is the whole contract: "[TOKEN]" contains no TOKEN, so a
# value that came off the tape already masked scrubs to itself.
mask_token = lambda s: s.replace(TOKEN, "[TOKEN]")


def test_a_secret_with_no_field_name_is_masked_wherever_it_sits(tmp_path):
    # Nothing is declared by name here — `redact` is empty. Every mask on this tape was put
    # there by the sweep, in the three places a field name cannot follow a value to.
    session = record(tmp_path, set(),
                     lambda: asyncio.run(toy_tools.leak_everywhere("t@x.com", TOKEN)),
                     scrub=mask_token)
    nothing_on_disk_carries(tmp_path, TOKEN)
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    fx_evs = [e for e in call["events"] if e["k"] == "fx"]
    assert fx_evs[0]["args"] == ["[TOKEN]"]                       # positional: no name at all
    assert fx_evs[1]["args"] == ["session:[TOKEN]:v1"]            # a substring inside a key
    assert call["result"]["body"].startswith("Hello t@x.com — your token is [TOKEN].")
    assert call["kwargs"]["token"] == "[TOKEN]"                   # and the kwarg too


def test_a_scrubbed_recording_still_replays(tmp_path):
    # The claim idempotence exists to make, tested rather than asserted in prose. Replay is
    # handed the MASKED token off the tape, rebuilds the key and the body out of it, and the
    # sweep maps the recorded derivation onto exactly that — so the questions still match.
    session = record(tmp_path, set(),
                     lambda: asyncio.run(toy_tools.leak_everywhere("t@x.com", TOKEN)),
                     scrub=mask_token)
    report = fr.replay_call(session, 0, ToyAdapter(set(), scrub=mask_token), None)
    assert report.ok, (report.divergence, report.result_diff, report.write_divergences)
    assert report.replayed_result["keyed"] == "session:[TOKEN]:v1"


def test_the_scrub_must_be_applied_on_both_sides(tmp_path):
    # A secret born INSIDE the code is the case that proves the replay side has to sweep too.
    # `call_home` builds its password from a literal, not from the tape, so the replayed code
    # produces it RAW every time. The recorded question is masked; the replayed one is not,
    # unless replay scrubs its own side before comparing. This is why the sweep is applied on
    # both paths, and it is the reason it has to be idempotent.
    lit = "hunter2-literal"
    mask_lit = lambda s: s.replace(lit, "[LIT]")
    session = record(tmp_path, set(), lambda: asyncio.run(toy_tools.call_home("t@x.com")),
                     scrub=mask_lit)
    nothing_on_disk_carries(tmp_path, lit)

    blind = fr.replay_call(session, 0, ToyAdapter(set()), None)   # adapter declares no sweep
    assert not blind.ok and lit in (blind.divergence or "")
    seeing = fr.replay_call(session, 0, ToyAdapter(set(), scrub=mask_lit), None)
    assert seeing.ok, (seeing.divergence, seeing.result_diff)


def test_the_gap_forbid_could_only_refuse_is_now_masked(tmp_path):
    # Directly above, `test_a_value_no_field_name_could_ever_reach_is_still_caught` records
    # this same call and it RAISES: the tripwire sees the nameless copy of `a` and refuses,
    # and the call goes unrecorded. Same call, same tripwire, plus a sweep — and now the
    # recording exists, carries no credential, and replays.
    session = record(tmp_path, set(),
                     lambda: asyncio.run(toy_tools.remote_sum("t@x.com", KEY, "k")),
                     scrub=lambda s: s.replace(KEY, "[KEY]"), forbid=[KEY_SHAPE])
    nothing_on_disk_carries(tmp_path, KEY)
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    assert next(e for e in call["events"] if e["k"] == "fx")["args"] == ["[KEY]"]
    report = fr.replay_call(session, 0,
                            ToyAdapter(set(), scrub=lambda s: s.replace(KEY, "[KEY]")), None)
    assert report.ok, (report.divergence, report.result_diff)


def test_a_raising_scrub_degrades_to_the_mask(tmp_path):
    # The failure direction is "masked", never "leaked" and never "broke the recorded call".
    # A sweep that blows up on every string is the worst case, and the tool still returns.
    def broken(s):
        raise RuntimeError("boom")

    got = {}
    session = record(tmp_path, set(),
                     lambda: got.setdefault(
                         "r", asyncio.run(toy_tools.leak_everywhere("t@x.com", TOKEN))),
                     scrub=broken)
    assert got["r"]["body"].endswith("Do not share it.")   # the call itself: untouched
    nothing_on_disk_carries(tmp_path, TOKEN)
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    assert call["kwargs"]["token"] == fr.REDACTED
    assert call["result"]["body"] == fr.REDACTED


def test_scrub_composes_with_redact_rather_than_replacing_it(tmp_path):
    # Both layers, both visible on one tape: the named field carries the field rule's mask,
    # and the three copies no name could reach carry the sweep's.
    session = record(tmp_path, {"token"},
                     lambda: asyncio.run(toy_tools.leak_everywhere("t@x.com", TOKEN)),
                     scrub=mask_token)
    nothing_on_disk_carries(tmp_path, TOKEN)
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    assert call["kwargs"]["token"] == fr.REDACTED                 # by name
    fx_evs = [e for e in call["events"] if e["k"] == "fx"]
    assert fx_evs[0]["args"] == ["[TOKEN]"]                       # by value
    assert fx_evs[1]["args"] == ["session:[TOKEN]:v1"]


def test_a_transform_output_still_meets_the_sweep(tmp_path):
    # A field rule that returns a string is not a licence for the sweep to look away — it is
    # easy to write a "safe" transform that quietly keeps the secret inside its output.
    session = record(tmp_path, {"token": lambda v: f"len:{v}"},
                     lambda: asyncio.run(toy_tools.leak_everywhere("t@x.com", TOKEN)),
                     scrub=mask_token)
    nothing_on_disk_carries(tmp_path, TOKEN)
    call = json.loads(session.read_text(encoding="utf-8").splitlines()[1])
    assert call["kwargs"]["token"] == "len:[TOKEN]"


def test_no_scrub_is_a_no_op(tmp_path):
    # Opt-in, like the other two: a boundary that declares nothing records exactly as before.
    session = record(tmp_path, set(),
                     lambda: asyncio.run(toy_tools.leak_everywhere("t@x.com", TOKEN)))
    assert TOKEN in session.read_text(encoding="utf-8")
