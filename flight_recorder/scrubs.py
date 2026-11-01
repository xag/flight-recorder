"""Stock declarations an app may hand its Boundary: credentials, masked by shape and by field.

The recorder knows nothing about what any value means, and nothing here changes that: no
recording consults this module unless the app declares it, exactly as it declares any other
`scrub` or `forbid`. It ships beside the recorder because every app that records a
configuration file, an environment or a conversation transcript meets the same credentials,
and two apps had already written the same sweep by hand when a third leaked a broker's key onto
three tapes for want of one.

    BOUNDARY = fr.Boundary(
        ...,
        scrub=scrubs.credentials,              # mask every credential, wherever it sits
        forbid=scrubs.CREDENTIAL_SHAPES,       # and refuse a line where one survived
    )

An app whose own fields are named like credentials and hold none (a `key` naming where a client
keeps its servers, a `token_env` naming a variable) says so with `exempt`:

    scrub=lambda s: scrubs.credentials(s, exempt={"key", "token_env"})

`credentials` masks TEXT: in any string, every credential shape (an `sk-` key, a GitHub or
GitLab token, an AWS access key id, a Slack token, a JWT, a Bearer or Basic credential, a
password in a URL, a secret in a query string), and in JSON, YAML or TOML text, the value of
every field whose name says it holds a credential. `credentials_data` masks what that text
parses to, and the two agree: parsing masked text gives the masked parse. That agreement is
what lets a replay that parses a masked file derive from it exactly what the live code derived
from the masked form.

A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the value's sha256: deterministic, so
a replay re-derives the same mask, and distinct, so two different keys stay two different masks.
Both functions are idempotent (a mask is never masked again), which `Boundary.scrub` requires.
A value under a credential field is kept when it is empty, a variable reference (`${TOKEN}`,
`{env:TOKEN}`, `%TOKEN%`) or already a mask: a reference names a secret without holding it.

The patterns are ASCII: `\\w`, `\\s` and `\\b` mean the same here as in every other runtime's
regex engine, so the six implementations mask the same text to the same bytes
(spec/fixtures/credentials.json).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

MASK = re.compile(r"<secret:[0-9a-f]{8}>")

# A field whose name says it holds a credential.
SECRET_FIELD = re.compile(
    r"key(?!words?|board)|secret|token|passw|auth(?!or(?!i))|credential|cookie|session|bearer",
    re.IGNORECASE | re.ASCII)

# A variable reference names a secret without holding it.
REFERENCE = re.compile(r"\A(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%)\Z", re.ASCII)

# Credential shapes, wherever they appear. Group `v` is the part masked; the rest is context
# that says it is a credential (`Bearer `, `://user:`...@`, `?api_key=`).
CREDENTIAL_SHAPES = [
    r"(?<![\w-])(?P<v>sk-(?:ant-)?[A-Za-z0-9_-]{20,})",
    r"(?<![\w-])(?P<v>gh[pousr]_[A-Za-z0-9]{20,})",
    r"(?<![\w-])(?P<v>github_pat_[A-Za-z0-9_]{20,})",
    r"(?<![\w-])(?P<v>glpat-[A-Za-z0-9_-]{20,})",
    r"(?<![A-Za-z0-9])(?P<v>AKIA[0-9A-Z]{16})(?![A-Za-z0-9])",
    r"(?<![\w-])(?P<v>xox[abpr]-[A-Za-z0-9-]{10,})",
    r"(?<![\w-])(?P<v>eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})",
    # with a digit in it: "use a Bearer token" is prose, not a credential
    r"\b(?:Bearer|Basic)\s+(?P<v>(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{8,})",
    # a password in a URL, unless it is a reference. Neither this nor the query value below
    # runs through a backslash: in JSON text that is where an escape begins (`\"`), and a value
    # that swallowed it would mask a different string in the text than in its parse.
    r"://[^:@/\s\"'<>\\]+:(?P<v>(?![$%{])[^:@/\s\"'<>\\]+)@",
    r"[?&][\w-]*(?:key|secret|token|passw|auth|session|sig)[\w-]*=(?P<v>[^&\s\"'<>#\\]+)",
]
_SHAPES = [re.compile(p, re.ASCII) for p in CREDENTIAL_SHAPES]

# A `key: "value"` (JSON, YAML) or `key = "value"` (TOML) pair in text.
_PAIR = re.compile(r"""(?P<k>"(?:[^"\\\n]|\\.)*"|'[^'\n]*'|(?<![\w.-])[A-Za-z0-9_.-]+)(?P<sep>\s*[:=]\s*)"""
                   r"""(?P<v>"(?:[^"\\\n]|\\.)*"|'[^'\n]*')""", re.ASCII)


def secret(value: str) -> str:
    """The mask of one value: `<secret:` + the first 8 hex of its UTF-8 sha256 + `>`."""
    return f"<secret:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}>"


def secret_field(name: str, exempt: Iterable[str] = ()) -> bool:
    """Whether a field of this name holds a credential, unless the app exempted the name."""
    return name not in exempt and bool(SECRET_FIELD.search(name))


def _keeps(value: str) -> bool:
    """A value left as it is under a credential field: empty, a reference, already a mask."""
    return not value or bool(REFERENCE.match(value)) or bool(MASK.fullmatch(value))


def _unquote(token: str) -> str:
    if token.startswith("'"):
        return token[1:-1]
    try:
        return json.loads(token)
    except ValueError:
        return token[1:-1]


def _shapes(text: str) -> str:
    for shape in _SHAPES:
        def one(m: re.Match) -> str:
            start, end = m.span("v")
            whole = m.group(0)
            at = start - m.start()
            return whole[:at] + secret(m.group("v")) + whole[at + end - start:]
        text = shape.sub(one, text)
    return text


def credentials(text: str, exempt: Iterable[str] = ()) -> str:
    """`text` with every credential field's value and every credential shape masked."""
    exempt = frozenset(exempt)

    def pair(m: re.Match) -> str:
        k = m.group("k")
        # A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
        name = _unquote(k) if k[0] in "\"'" else k.rsplit(".", 1)[-1]
        value = _unquote(m.group("v"))
        if not secret_field(name, exempt) or _keeps(value):
            return m.group(0)
        quote = m.group("v")[0]
        return f"{k}{m.group('sep')}{quote}{secret(value)}{quote}"

    return _shapes(_PAIR.sub(pair, text))


def credentials_data(value: Any, exempt: Iterable[str] = (), key: str | None = None) -> Any:
    """`credentials` for parsed data: a string under a credential field is masked whole, any
    other string as text. Maps and sequences are walked; other scalars are kept."""
    exempt = frozenset(exempt)
    if isinstance(value, dict):
        return {k: credentials_data(v, exempt, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [credentials_data(v, exempt) for v in value]
    if isinstance(value, str):
        if key is not None and secret_field(key, exempt) and not _keeps(value):
            return secret(value)
        return credentials(value, exempt)
    return value
