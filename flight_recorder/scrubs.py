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
password in a URL, a secret in a query string), and the value of every field whose name says
it holds a credential: in JSON, YAML or TOML text; in such text held inside a JSON string at any
depth (`\\"KEY\\": \\"value\\"`, which is how a Claude Code transcript holds a file a tool read);
and in unquoted assignments (`KEY=value`, `export KEY=value`, `set KEY=value`,
`$env:KEY = value`, `--password=value`), whose value runs to whitespace or the end of the line. `credentials_data` masks what that text
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
REFERENCE = re.compile(r"\A(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%|\$(?:env:)?\w+)\Z", re.ASCII)
# ...and one that opens an unquoted value, which stops at `}`: `TOKEN=${MY_TOKEN}` is a reference.
_REFERENCE_AT = re.compile(r"\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%", re.ASCII)

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

# A `key: "value"` (JSON, YAML) or `key = "value"` (TOML) pair in text. A quoted key never opens
# at an escaped quote: inside a JSON string every `\"` would otherwise start a token running to
# the string's end, and a transcript (one long string of escaped JSON) took time quadratic in
# its length, 9 s for 60 KB.
_PAIR = re.compile(r"""(?P<k>(?<!\\)"(?:[^"\\\n]|\\.)*"|'[^'\n]*'|(?<![\w.-])[A-Za-z0-9_.-]+)(?P<sep>\s*[:=]\s*)"""
                   r"""(?P<v>"(?:[^"\\\n]|\\.)*"|'[^'\n]*')""", re.ASCII)

# The same pair written inside a JSON string, at any depth: `\"KEY\": \"value\"` is a JSON
# document held in a string (a tool result in a Claude Code transcript), `\\\"KEY\\\": ...` that
# string held in another. All four quotes carry the same run of backslashes. Key and value hold
# no quote, backslash or line break, so the value in the text is the value in its parse, and
# both mask to the same `<secret:...>`.
_ESCAPED_PAIR = re.compile(r"""(?<!\\)(?P<q>\\+)"(?P<k>[^"\\\n]+)(?P=q)"(?P<sep>[ \t]*[:=][ \t]*)"""
                           r"""(?P<q2>(?P=q))"(?P<v>[^"\\\n]*)(?P=q)\"""", re.ASCII)
# ...and with a bare key: `KEY=\"value\"`, `password: \"value\"`, a dotenv or YAML line held in
# a JSON string.
_ESCAPED_VALUE = re.compile(r"""(?<![\w.-])(?P<k>[A-Za-z0-9_.-]+)(?P<sep>[ \t]*[:=][ \t]*)"""
                            r"""(?P<q>\\+)"(?P<v>[^"\\\n]*)(?P=q)\"""", re.ASCII)

# An unquoted assignment: `KEY=value`, `export KEY=value`, `set KEY=value`, `$env:KEY = value`,
# a .env line, a `--password=...` flag. The value runs to whitespace or the end of the line, and
# stops at a quote, a backslash (where a JSON escape such as `\n` begins), or `&;|<>,)}]`. A
# value starting with `=` is a comparison (`token == x`), not an assignment. Quoted values are
# the pair's (`KEY="value"`).
_ASSIGN = re.compile(r"""(?<![\w.-])(?P<k>[A-Za-z0-9_.-]+)(?P<sep>[ \t]*=[ \t]*)"""
                     r"""(?P<v>(?!=)[^\s"'`\\&;|<>,)}\]]+)""", re.ASCII)


def secret(value: str) -> str:
    """The mask of one value: `<secret:` + the first 8 hex of its UTF-8 sha256 + `>`."""
    return f"<secret:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}>"


def secret_field(name: str, exempt: Iterable[str] = ()) -> bool:
    """Whether a field of this name holds a credential, unless the app exempted the name."""
    # A name holding a mask is a value an earlier pass masked, read back as the key of the next
    # pair (`token="<secret:...>": "v"`); `secret` in the mask is not the field saying anything.
    return name not in exempt and not MASK.search(name) and bool(SECRET_FIELD.search(name))


def _keeps(value: str) -> bool:
    """A value left as it is under a credential field: empty, a reference, or one already holding
    a mask. The last is what makes a second pass a no-op: a reference kept on the first pass
    (`$AKIA...`, `${...}`) can have a shape masked inside it, and what is left (`$<secret:...>`)
    is no longer a reference but must not be masked again."""
    return not value or bool(REFERENCE.match(value)) or bool(MASK.search(value))


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
            return None
        quote = m.group("v")[0]
        return f"{k}{m.group('sep')}{quote}{secret(value)}{quote}"

    def escaped(m: re.Match) -> str:
        value, q = m.group("v"), m.group("q")
        if not secret_field(m.group("k"), exempt) or _keeps(value):
            return None
        return f'{q}"{m.group("k")}{q}"{m.group("sep")}{q}"{secret(value)}{q}"'

    def escaped_value(m: re.Match) -> str:
        value, q = m.group("v"), m.group("q")
        if not secret_field(m.group("k").rsplit(".", 1)[-1], exempt) or _keeps(value):
            return m.group(0)
        return f'{m.group("k")}{m.group("sep")}{q}"{secret(value)}{q}"'

    # The passes run until the text stops changing. A later pass can change how an earlier one
    # reads the text: a shape or an assignment masks the key of a pair that had swallowed the
    # next pair's opening quote, and the next round reads that pair. Repeating makes the result
    # a fixed point, so a second scrub is a no-op by construction (a replay re-scrubs what it
    # read off the tape and must find it unchanged). It ends: every round that changes the text
    # replaces at least one character outside the masks with a mask, and a mask is never
    # masked again. Text that holds no credential costs one round.
    while True:
        before = text
        text = _chained(_PAIR, pair, text, "v")
        text = _chained(_ESCAPED_PAIR, escaped, text, "q2")
        text = _ESCAPED_VALUE.sub(escaped_value, text)
        text = _assignments(text, exempt)
        text = _shapes(text)
        if text == before:
            return text


def _chained(pattern: re.Pattern, mask, text: str, value: str) -> str:
    """`pattern.sub(mask, text)`, except that a pair left as it is (`mask` returns None) gives
    its value back when that value opens a pair of its own.

    In `x = "api_key": "v1"` the pair `x = "api_key"` is not a credential, and a scan that
    resumed after its value never saw `"api_key": "v1"`: the value of a field named api_key
    stayed on the tape, and if a shape later masked `x` a second pass masked it. So the scan
    tries one pair anchored at the value's start, and only if none opens there does it skip
    the value whole, as before: resuming inside a quoted value would read its escaped quotes
    as the openings of new tokens. `value` names the group where the value's token begins."""
    out, pos, anchor, skip = [], 0, None, 0
    while True:
        m = pattern.match(text, anchor) if anchor is not None else pattern.search(text, pos)
        if m is None:
            if anchor is not None:
                anchor = None
                out.append(text[pos:skip])
                pos = skip
                continue
            out.append(text[pos:])
            return "".join(out)
        anchor = None
        replaced = mask(m)
        if replaced is None:
            out.append(text[pos:m.start(value)])
            pos = anchor = m.start(value)
            skip = m.end()
            continue
        out.append(text[pos:m.start()] + replaced)
        pos = m.end()


def _assignments(text: str, exempt: frozenset) -> str:
    """The unquoted assignments masked, scanning left to right.

    Unlike the other passes, an assignment left as it is gives back its value to the scan: the
    search resumes right after its name, not after its value. In `a=token=x` the value of `a`
    holds an assignment of its own, and in `AKIA...=Bearer = v` a shape masked later turns the
    first name into a mask and frees `Bearer = v`; a scan that had swallowed it would mask it
    only on a second pass, and a scrub must be a no-op on its own output. A value directly
    followed by a mask is the rest of a value already masked (`$env:<secret:...>`) and is kept.
    """
    out, pos = [], 0
    while True:
        m = _ASSIGN.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        name, value = m.group("k").rsplit(".", 1)[-1], m.group("v")
        if (not secret_field(name, exempt) or _keeps(value)
                or MASK.match(text, m.end("v")) or _REFERENCE_AT.match(text, m.start("v"))):
            out.append(text[pos:m.end("k")])
            pos = m.end("k")
            continue
        out.append(text[pos:m.start("v")] + secret(value))
        pos = m.end("v")


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
