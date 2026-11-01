"""The stock credential scrub (flight_recorder.scrubs), proven over a declared domain.

The two forms agree: a replay parses files the tape holds masked and derives from them what the
live code derived from the masked form, so parsing masked text must give the masked parse. The
domain is every pairing of the field names and values below, in JSON (both escapings) and in
TOML, each value alone and inside a list. Then the scrub is used as what it is for, a
Boundary's `scrub` and `forbid`, on a recorded call and its replay trace.

spec/fixtures/credentials.json holds this implementation's output over the same domain; every
other runtime checks its own output against it byte for byte. Rewrite it with
`uv run python -c "from tests.test_scrubs import write_vectors; write_vectors()"`.
"""

import asyncio
import itertools
import json
import os
import tomllib
from pathlib import Path

import pytest

import flight_recorder as fr
from flight_recorder.scrubs import CREDENTIAL_SHAPES, MASK, credentials, credentials_data
from tests import toy_effects, toy_tools

# An app's own fields named like credentials that hold none; the library takes them as a
# parameter and knows none of them itself.
EXEMPT = ("key", "token_env", "auth")

KEYS = ["ALPACA_API_KEY_ID", "api_token", "Authorization", "password", "session", "key", "token_env",
        "auth", "author", "keywords", "command", "url", "DB"]
# Credential-shaped values are built by concatenation, so this file is not one a scanner reads
# as carrying live-looking keys.
SK = "sk-ant-" + "api03-abcdefghijklmnopqrstu"
GHP = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123"
AKIA = "AKIA" + "ABCDEFGHIJKLMNOP"
VALUES = ["PKRAW0001", "s3cr\"et\\x", "Bearer abc123def456", SK, GHP, AKIA, "postgres://u:pw1234@h/db",
          "https://x.io/mcp?api_key=abc123&x=1", "${MY_TOKEN}", "{env:MY_TOKEN}", "$MY_TOKEN", "", "mcpServers",
          "use a Bearer token", "é", "<secret:0123abcd>",
          # a credential that ends at an escaped quote or a backslash in JSON text: the shape must
          # stop where the JSON string's escape begins, or text and data mask different values
          'curl "https://x.io/?token=abc123" -s', 'x://u:ab\\c@h', 'curl "https://u:pw1234@h/x" -s']
SHAPED = {SK, GHP, AKIA}
DOMAIN = [{k: v} for k, v in itertools.product(KEYS, VALUES)] + \
         [{k: [v]} for k, v in itertools.product(KEYS, VALUES)]
CREDENTIAL_FIELDS = ("ALPACA_API_KEY_ID", "api_token", "Authorization", "password", "session")


@pytest.mark.parametrize("exempt", [(), EXEMPT])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_masked_json_parses_to_the_masked_data(ascii_only, exempt):
    for data in DOMAIN:
        doc = {"servers": {"x": data}}
        text = json.dumps(doc, indent=2, ensure_ascii=ascii_only)
        assert json.loads(credentials(text, exempt)) == credentials_data(doc, exempt), text
        assert credentials(text, exempt) == json.dumps(credentials_data(doc, exempt), indent=2,
                                                       ensure_ascii=ascii_only)


def toml_texts():
    for (key, value), quote in itertools.product(itertools.product(KEYS, VALUES), ['"', "'"]):
        if quote == "'" and ("'" in value or "\\" in value):
            continue  # not a TOML literal string
        literal = json.dumps(value) if quote == '"' else f"'{value}'"
        yield key, f"[env]\n{json.dumps(key)} = {literal}\n"
        yield key, f"env = {{ {json.dumps(key)} = {literal} }}\n"


@pytest.mark.parametrize("exempt", [(), EXEMPT])
def test_masked_toml_parses_to_the_masked_data(exempt):
    for _, text in toml_texts():
        assert tomllib.loads(credentials(text, exempt)) == credentials_data(tomllib.loads(text), exempt), text


def test_a_mask_is_never_masked_again():
    for data in DOMAIN:
        text = json.dumps(data)
        assert credentials(credentials(text)) == credentials(text)
        assert credentials_data(credentials_data(data)) == credentials_data(data)


def test_no_credential_field_or_shape_survives():
    for (key, value) in itertools.product(KEYS, VALUES):
        masked = json.dumps(credentials_data({key: value}, EXEMPT))
        if value in SHAPED or (key in CREDENTIAL_FIELDS and value and not value.startswith(("$", "{", "<"))):
            assert json.dumps(value)[1:-1] not in masked and MASK.search(masked), (key, value)


def test_an_exempted_field_is_kept_and_only_that_one():
    assert credentials_data({"key": "mcpServers"}, EXEMPT) == {"key": "mcpServers"}
    assert credentials_data({"key": "mcpServers"}) != {"key": "mcpServers"}
    # an exemption keeps the field, not a credential shape inside it
    assert SK not in json.dumps(credentials_data({"key": SK}, EXEMPT))


def test_the_shapes_as_forbid_pass_a_scrubbed_text():
    # The pairing the module docstring recommends: whatever `credentials` leaves, no shape fires
    # on, so declaring both never refuses a recording the scrub already cleaned.
    shapes = fr.Boundary(forbid=CREDENTIAL_SHAPES).forbid_patterns()
    for data in DOMAIN:
        text = json.dumps(data)
        assert fr.forbidden_hit(credentials(text), shapes) is None, text
    assert fr.forbidden_hit(json.dumps({"note": SK}), shapes) is not None


# --- what actually leaked: a pair inside a JSON string, and unquoted assignments ---------------
#
# On 2026-11-01 three craft-laws tapes held a broker's key. A session had read a file holding
# `"ALPACA_API_SECRET_KEY": "..."`; the transcript holds that tool result as a JSON string, so the
# pair reached the tape as `\"ALPACA_API_SECRET_KEY\": \"...\"`, a form the pair pattern did not
# know. The domain below is the pairs whose value a JSON string carries without escapes (no
# quote, backslash or line break; non-ASCII kept as itself), at each depth of nesting.

NESTABLE = [v for v in VALUES if not set(v) & set('"\\\n')]
NESTED_DOMAIN = [{k: v} for k, v in itertools.product(KEYS, NESTABLE)]


def nest(text: str, depth: int) -> str:
    for _ in range(depth):
        text = json.dumps(text, ensure_ascii=False)
    return text


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("exempt", [(), EXEMPT])
def test_a_pair_inside_a_json_string_masks_as_its_parse_does(depth, exempt):
    for data in NESTED_DOMAIN:
        for inner in (json.dumps({"servers": {"x": data}}, indent=2, ensure_ascii=False),
                      "".join(t for _, t in toml_texts_for(data))):
            outer = nest(inner, depth)
            masked = credentials(outer, exempt)
            assert json.loads(masked) == credentials(nest(inner, depth - 1), exempt), outer
            assert credentials(masked, exempt) == masked


def toml_texts_for(data):
    (key, value), = data.items()
    yield key, f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}\n"


def test_the_transcript_line_that_leaked_is_masked():
    # The shape that leaked, built as a Claude Code transcript builds it: a trace line read
    # with a tool, held as the tool result's content, in one JSONL row of the transcript; and
    # that row once more inside the tape that recorded the transcript.
    key_value = "PKRAW" + "0001TESTVALUE"
    trace_line = json.dumps({"e": "L", "d": {"env": {"ALPACA_API_SECRET_KEY": key_value,
                                                     "PATH": "/usr/bin"}}})
    row = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": trace_line}]}})
    for text in (row, json.dumps(row)):
        masked = credentials(text)
        assert key_value not in masked and "/usr/bin" in masked, masked
        assert credentials(masked) == masked


ASSIGNABLE = [v for v in VALUES if v and not set(v) & set(" \t\r\n\"'`\\&;|<>,)}]") and v[0] != "="]
ASSIGNMENTS = [
    lambda k, v: f"{k}={v}",
    lambda k, v: f"export {k}={v}",
    lambda k, v: f"set {k}={v}",
    lambda k, v: f"$env:{k} = {v}",
    lambda k, v: f'{k}="{v}"',
    lambda k, v: f"{k}='{v}'",
    lambda k, v: f'$env:{k} = "{v}"',
    lambda k, v: f"--{k}={v}",
]


def test_an_assignment_masks_its_value_when_the_name_holds_a_credential():
    for (key, value), form in itertools.product(itertools.product(KEYS, ASSIGNABLE), ASSIGNMENTS):
        text = form(key, value) + " # done"
        masked = credentials(text)
        from flight_recorder.scrubs import _keeps, secret, secret_field
        if secret_field(key) and not _keeps(value):
            assert masked == form(key, secret(value)) + " # done", text
        else:
            assert masked == form(key, credentials(value)) + " # done", text


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_a_dotenv_file_masks_alike_at_every_depth(depth):
    dotenv = "\n".join(form(k, v) for (k, v), form in
                       itertools.product(itertools.product(KEYS, ASSIGNABLE), ASSIGNMENTS)) + "\n"
    outer = nest(dotenv, depth)
    masked = credentials(outer)
    expect = credentials(dotenv)
    for _ in range(depth):
        masked = json.loads(masked)
    assert masked == expect
    assert credentials(credentials(outer)) == credentials(outer)


def test_a_comparison_is_not_an_assignment():
    for text in ("if token == other:", "token != x", "password==hash(p)", "token<=3"):
        assert credentials(text) == text


# Found by differential fuzzing against the Go port: texts the scrub masked differently on a
# second pass. A scrub must be a no-op on its own output, or a replay that re-scrubs what it
# read off the tape compares against something else.
SECOND_PASS = [
    f"'{AKIA}=Bearer  = Bearer ,",          # a shape masked later freed an assignment
    f"token = $env:{AKIA}" + chr(92) + '"KEY',  # a kept reference had a shape masked inside
    f'{{"token": "${AKIA}"}}',
    f'{{"token": "${{{AKIA}}}"}}',
    f'{AKIA} = "api_key": "v1"',            # a pair left as it is swallowed the next pair's key
    'token="token": "{"x"',                 # a masked value read back as a key holds "secret"
    "$token='pw' = \"api_key\"Bearer",
    # a later pass changed how an earlier one read the text (the passes now run to a fixed point)
    f'{AKIA}:""key":"y"',
    'token=v:"auth:"y"',
    'token=-=""KEY":"1"',
    'session=d:' + chr(92) + '"passw:' + chr(92) + '"y' + chr(92) + '"',
    'token=_x="token="token": "\t)&{',
    'token="token": "_;x=" token="token": "0123456789',
]

# The fragments the differential fuzzing of the ports used, for a seeded property below.
_FRAGMENTS = [chr(92), chr(92) * 2, chr(92) + '"', chr(92) * 3 + '"', '"', '""', "'", '`', '$', '$env:',
              '${X}', '%X%', '--', '==', '=', ' = ', ':', ': ', 'token', 'secretBearer', 'api_key',
              'password', 'a.b.', 'KEY', 'v1', AKIA, 'Bearer ', 'é', '\n', ' ', '\t', '<', '>', '&', '?',
              '://', '@', '{', '}', ')', ',', ';', '<secret:0123abcd>', '"api_key"', '${', 'a=', 'token=',
              "'pw'", 'x = ', '": "', '"token": "', 'x="', 'auth:', 'passw:']


def test_the_scrub_is_a_fixed_point_over_dense_fragments():
    import random
    rng = random.Random(17)
    for i in range(20000):
        text = "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 16)))
        exempt = ("password",) if i % 2 else ()
        once = credentials(text, exempt)
        assert credentials(once, exempt) == once, text


def test_a_pair_left_as_it_is_gives_back_a_value_that_opens_a_pair():
    assert credentials('x = "api_key": "v1"') == 'x = "api_key": ' + f'"{secret_of("v1")}"'
    escaped = chr(92) + '"'
    text = f"{escaped}x{escaped}: {escaped}api_key{escaped}: {escaped}v1{escaped}"
    assert "v1" not in credentials(text)


def test_a_long_string_of_escaped_json_scrubs_in_linear_time():
    # A transcript is one long string of escaped JSON. When every escaped quote could open a
    # quoted token running to the string's end, 60 KB took 9 s, and 1 MB would take 45 minutes.
    import random
    import time
    rng = random.Random(3)
    inner = "".join(rng.choice(['"', "a", "b", " ", ":"]) for _ in range(50000))
    for text in (json.dumps(inner), json.dumps(json.dumps(inner))):
        t = time.perf_counter()
        credentials(text)
        assert time.perf_counter() - t < 1.0


def test_the_scrub_is_a_no_op_on_its_own_output():
    for text in SECOND_PASS:
        once = credentials(text)
        assert credentials(once) == once, text
        assert AKIA not in once


def test_an_unchanged_assignment_gives_its_value_back_to_the_scan():
    assert credentials("a=token=abc123") == "a=token=" + secret_of("abc123")


def test_a_reference_opening_an_unquoted_value_is_kept():
    for text in ("TOKEN=${MY_TOKEN}", "TOKEN={env:MY_TOKEN}", "TOKEN=%MY_TOKEN%", "TOKEN=$MY_TOKEN"):
        assert credentials(text) == text


def secret_of(value):
    from flight_recorder.scrubs import secret
    return secret(value)


def test_a_copy_of_the_environment_is_masked_by_its_names(monkeypatch):
    # A traced local holding `{**os.environ}` is the app's own data, not machinery the trace
    # opened; parsed, it is masked by the fields' names. (A Boundary's `scrub` sweeps leaf
    # strings without their keys: as a scrub, `credentials` sees only the value.)
    sentinel = "fr-scrub-env-" + "4b9e2d"
    monkeypatch.setenv("FR_SCRUB_API_TOKEN", sentinel)
    monkeypatch.setenv("FR_SCRUB_SECRET_KEY", sentinel)
    monkeypatch.setenv("FR_SCRUB_PLAIN", "kept-as-is")
    masked = credentials_data({**os.environ})
    assert sentinel not in json.dumps(masked)
    assert masked["FR_SCRUB_PLAIN"] == "kept-as-is"
    assert MASK.fullmatch(masked["FR_SCRUB_API_TOKEN"])


# --- as a Boundary declares it: tape, trace, replay --------------------------------------------

PROSE_KEY = "sk-" + "proj-0123456789abcdefghijklmnop"


class ScrubbedAdapter(fr.ReplayAdapter):
    def __init__(self, boundary):
        self.boundary = boundary
        self.trace_root = os.path.dirname(toy_tools.__file__)

    def resolve(self, fn_name, feed):
        fn = getattr(toy_tools, fn_name)
        return getattr(fn, "__flight_wrapped__", fn)


def test_a_boundary_declaring_the_scrub_writes_no_credential_and_still_replays(tmp_path):
    boundary = fr.Boundary(effects=[(toy_effects, ["fetch_remote"])],
                           scrub=credentials, forbid=CREDENTIAL_SHAPES)
    fr.install(boundary, toy_tools, directory=str(tmp_path), enabled=True)
    try:
        asyncio.run(toy_tools.leak_everywhere("t@x.com", PROSE_KEY))
        session = fr.session_path()
    finally:
        fr.uninstall()
    report = fr.replay_call(session, 0, ScrubbedAdapter(boundary), tmp_path / "trace.jsonl")
    assert report.ok, (report.divergence, report.result_diff)
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert PROSE_KEY not in p.read_text(encoding="utf-8", errors="ignore"), p
    assert MASK.search((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))


# --- the vectors every runtime checks ----------------------------------------------------------

VECTORS = Path(__file__).resolve().parent.parent / "spec" / "fixtures" / "credentials.json"


# The craft-laws shape: a transcript row whose tool result is a trace line holding a key.
TRANSCRIPT_ROW = json.dumps({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(
        {"e": "L", "d": {"env": {"ALPACA_API_SECRET_KEY": "PKRAW" + "0001TESTVALUE",
                                 "PATH": "/usr/bin"}}})}]}})


def vectors() -> dict:
    """One document per text form holding the domain's scalar pairs, so a runtime that disagrees
    can be diffed line by line; the same pairs as data; and prose for the shapes. The list form
    and TOML's inline tables stay in the property tests above: here they would double the file
    and test nothing the scalar pairs do not."""
    prose = [json.dumps({"note": "café", "password": "é", "url": "https://x.io/?token=é1"}, ensure_ascii=False),
             "Authorization: Bearer abc123def456 and use a Bearer token",
             f"key {SK} then {GHP}, {AKIA}x and x{AKIA}",
             "git clone https://u:pw1234@h/r.git; https://u:${PW}@h; mysql://u:%PW%@h",
             "GET /v1?api_key=abc123&sig=9z#frag and xoxb-1234567890-abc",
             "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
             "export TOKEN='abc' && password = \"hunter22\"; author: \"Ada\""]
    cases = []
    # The exemptions only bear on the fields they name, so their pass is over those fields.
    for exempt, keys in (([], KEYS), (list(EXEMPT), list(EXEMPT) + ["api_token"])):
        pairs = [p for p in DOMAIN if next(iter(p)) in keys and isinstance(next(iter(p.values())), str)]
        data = {f"p{i}": pair for i, pair in enumerate(pairs)}
        toml = "".join(text.replace("[env]\n", "") for key, text in toml_texts()
                       if key in keys and text.startswith("[env]"))
        nested_pairs = {f"p{i}": pair for i, pair in enumerate(p for p in NESTED_DOMAIN if next(iter(p)) in keys)}
        nested = json.dumps(nested_pairs, indent=2, ensure_ascii=False)
        # each pair in one form, the forms taken in turn: every form meets every kind of value
        # in the property test above; here the file stays small enough to diff.
        dotenv = "\n".join(ASSIGNMENTS[i % len(ASSIGNMENTS)](k, v) for i, (k, v) in
                           enumerate(itertools.product(keys, ASSIGNABLE))) + "\n"
        texts = [json.dumps(data, indent=2), toml, nest(nested, 1), dotenv, nest(dotenv, 1)]
        for text in texts + prose + SECOND_PASS + [TRANSCRIPT_ROW, nest(TRANSCRIPT_ROW, 1),
                                                   "a=token=abc123 TOKEN=${MY_TOKEN} TOKEN=%MY_TOKEN%"]:
            cases.append({"exempt": exempt, "text": text, "masked": credentials(text, exempt)})
        cases.append({"exempt": exempt, "data": data, "masked_data": credentials_data(data, exempt)})
    return {"mask": MASK.pattern, "cases": cases}


def write_vectors() -> None:
    VECTORS.write_text(json.dumps(vectors(), ensure_ascii=False) + "\n", encoding="utf-8")


def test_the_vectors_every_runtime_checks_are_this_runtimes_output():
    assert json.loads(VECTORS.read_text(encoding="utf-8")) == vectors()
