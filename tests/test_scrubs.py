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
          "https://x.io/mcp?api_key=abc123&x=1", "${MY_TOKEN}", "{env:MY_TOKEN}", "", "mcpServers",
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
        for text in [json.dumps(data, indent=2), toml] + prose:
            cases.append({"exempt": exempt, "text": text, "masked": credentials(text, exempt)})
        cases.append({"exempt": exempt, "data": data, "masked_data": credentials_data(data, exempt)})
    return {"mask": MASK.pattern, "cases": cases}


def write_vectors() -> None:
    VECTORS.write_text(json.dumps(vectors(), ensure_ascii=False) + "\n", encoding="utf-8")


def test_the_vectors_every_runtime_checks_are_this_runtimes_output():
    assert json.loads(VECTORS.read_text(encoding="utf-8")) == vectors()
