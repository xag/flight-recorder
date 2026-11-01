// Stock declarations an app may hand its boundary: credentials, masked by shape and by field.
//
// The recorder knows nothing about what any value means, and nothing here changes that: no
// recording consults this module unless the app declares it, exactly as it declares any other
// `scrub` or `forbid`.
//
//   const BOUNDARY = fr.boundaryOf({
//     scrub: fr.scrubs.credentials,             // mask every credential, wherever it sits
//     forbid: fr.scrubs.CREDENTIAL_SHAPES,      // and refuse a line where one survived
//   });
//
// An app whose own fields are named like credentials and hold none (a `key` naming where a
// client keeps its servers, a `token_env` naming a variable) says so with `exempt`:
//
//   scrub: (s) => fr.scrubs.credentials(s, ['key', 'token_env'])
//
// `credentials` masks TEXT: in any string, every credential shape (an `sk-` key, a GitHub or
// GitLab token, an AWS access key id, a Slack token, a JWT, a Bearer or Basic credential, a
// password in a URL, a secret in a query string), and the value of every field whose name says
// it holds a credential: in JSON, YAML or TOML text; in such text held inside a JSON string at
// any depth (`\"KEY\": \"value\"`, which is how a Claude Code transcript holds a file a tool
// read); and in unquoted assignments (`KEY=value`, `export KEY=value`, `set KEY=value`,
// `$env:KEY = value`, `--password=value`), whose value runs to whitespace or the end of the line.
// `credentialsData` masks what that text parses to, and the two agree: parsing masked text gives
// the masked parse.
//
// A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the value's sha256: deterministic,
// so a replay re-derives the same mask, and distinct, so two different keys stay two different
// masks. Both functions are idempotent (a mask is never masked again), which a boundary's
// `scrub` requires. A value under a credential field is kept when it is empty, a variable
// reference (`${TOKEN}`, `{env:TOKEN}`, `%TOKEN%`, `$TOKEN`, `$env:TOKEN`) or already a mask.
//
// Every runtime masks the same text to the same bytes (spec/fixtures/credentials.json). The
// patterns are the Python reference's, which compiles them ASCII-only. JavaScript without the
// `u` flag already reads `\w`, `\b` and case-insensitive matching as ASCII, but its `\s` and
// `.` do not match what Python's do, so neither appears below: whitespace is spelled
// `[ \t\n\r\f\v]` and "any character but a newline" is `[^\n]`.

import { createHash } from 'node:crypto';

const WS = ' \\t\\n\\r\\f\\v';

/** A mask, as `credentials` writes it. */
export const MASK = /<secret:[0-9a-f]{8}>/;
// A mask starting exactly where a value ends (sticky: matched at `lastIndex`, never searched).
const MASK_AT = /<secret:[0-9a-f]{8}>/y;

/** A field whose name says it holds a credential. */
export const SECRET_FIELD = /key(?!words?|board)|secret|token|passw|auth(?!or(?!i))|credential|cookie|session|bearer/i;

/** A variable reference names a secret without holding it. */
const REFERENCE = /^(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%|\$(?:env:)?\w+)$/;
// ...and one that opens an unquoted value, which stops at `}`: `TOKEN=${MY_TOKEN}` is a reference.
const REFERENCE_AT = /\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%/y;

/**
 * Credential shapes, wherever they appear, as pattern sources for a boundary's `forbid`. Group
 * `v` is the part masked; the rest is context that says it is a credential (`Bearer `,
 * `://user:`...`@`, `?api_key=`).
 */
export const CREDENTIAL_SHAPES = Object.freeze([
  String.raw`(?<![\w-])(?<v>sk-(?:ant-)?[A-Za-z0-9_-]{20,})`,
  String.raw`(?<![\w-])(?<v>gh[pousr]_[A-Za-z0-9]{20,})`,
  String.raw`(?<![\w-])(?<v>github_pat_[A-Za-z0-9_]{20,})`,
  String.raw`(?<![\w-])(?<v>glpat-[A-Za-z0-9_-]{20,})`,
  String.raw`(?<![A-Za-z0-9])(?<v>AKIA[0-9A-Z]{16})(?![A-Za-z0-9])`,
  String.raw`(?<![\w-])(?<v>xox[abpr]-[A-Za-z0-9-]{10,})`,
  String.raw`(?<![\w-])(?<v>eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})`,
  // with a digit in it: "use a Bearer token" is prose, not a credential
  String.raw`\b(?:Bearer|Basic)[${WS}]+(?<v>(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{8,})`,
  // a password in a URL, unless it is a reference
  String.raw`://[^:@/${WS}"'<>\\]+:(?<v>(?![$%{])[^:@/${WS}"'<>\\]+)@`,
  String.raw`[?&][\w-]*(?:key|secret|token|passw|auth|session|sig)[\w-]*=(?<v>[^&${WS}"'<>#\\]+)`,
]);
// `d` for the span of group `v`; `g` to sweep every match, as Python's `sub` does.
const SHAPES = CREDENTIAL_SHAPES.map((p) => new RegExp(p, 'dg'));

// A `key: "value"` (JSON, YAML) or `key = "value"` (TOML) pair in text.
// A quoted key cannot open at an escaped quote: without that, every `\"` in a long string of
// escaped JSON could open a token running to the string's end, and the pass was quadratic.
const QUOTED = String.raw`"(?:[^"\\\n]|\\[^\n])*"|'[^'\n]*'`;
const PAIR_SOURCE =
  String.raw`(?<k>(?<!\\)${QUOTED}|(?<![\w.-])[A-Za-z0-9_.-]+)(?<sep>[${WS}]*[:=][${WS}]*)(?<v>${QUOTED})`;
// `g` to search from a position, `y` to match only AT one; `d` for the groups' spans.
const PAIR = [new RegExp(PAIR_SOURCE, 'dg'), new RegExp(PAIR_SOURCE, 'dy')];

// The same pair written inside a JSON string, at any depth: `\"KEY\": \"value\"` is a JSON
// document held in a string, `\\\"KEY\\\": ...` that string held in another. All four quotes
// carry the same run of backslashes (the backreference). Key and value hold no quote, backslash
// or line break, so the value in the text is the value in its parse.
// `q2` is the value's own run of backslashes, where a pair the value opens would begin.
const ESCAPED_PAIR_SOURCE =
  String.raw`(?<!\\)(?<q>\\+)"(?<k>[^"\\\n]+)\k<q>"(?<sep>[ \t]*[:=][ \t]*)(?<q2>\k<q>)"(?<v>[^"\\\n]*)\k<q>"`;
const ESCAPED_PAIR = [new RegExp(ESCAPED_PAIR_SOURCE, 'dg'), new RegExp(ESCAPED_PAIR_SOURCE, 'dy')];
// ...and with a bare key: `KEY=\"value\"`, `password: \"value\"`, a dotenv or YAML line held
// in a JSON string.
const ESCAPED_VALUE = new RegExp(
  String.raw`(?<![\w.-])(?<k>[A-Za-z0-9_.-]+)(?<sep>[ \t]*[:=][ \t]*)(?<q>\\+)"(?<v>[^"\\\n]*)\k<q>"`,
  'g',
);
// An unquoted assignment: `KEY=value`, `export KEY=value`, `set KEY=value`, `$env:KEY = value`, a
// .env line, a `--password=...` flag. The value runs to whitespace or the end of the line, and
// stops at a quote, a backtick, a backslash (where a JSON escape begins), or `&;|<>,)}]`. A value
// starting with `=` is a comparison (`token == x`), not an assignment.
const ASSIGN = new RegExp(
  String.raw`(?<![\w.-])(?<k>[A-Za-z0-9_.-]+)(?<sep>[ \t]*=[ \t]*)(?<v>(?!=)[^${WS}"'\x60\\&;|<>,)}\]]+)`,
  'dg',
);

/** The field a bare key names: a dotted key (`env.API_KEY`) is a table path, its field the last part. */
const lastPart = (k) => k.slice(k.lastIndexOf('.') + 1);

/** The mask of one value: `<secret:` + the first 8 hex of its UTF-8 sha256 + `>`. */
export function secret(value) {
  return `<secret:${createHash('sha256').update(value, 'utf8').digest('hex').slice(0, 8)}>`;
}

/** Whether a field of this name holds a credential, unless the app exempted the name. */
export function secretField(name, exempt = []) {
  // A name holding a mask is a value an earlier pass masked, read back as the key of the next
  // pair (`token="<secret:...>": "v"`); `secret` in the mask is not the field saying anything.
  return !new Set(exempt).has(name) && !MASK.test(name) && SECRET_FIELD.test(name);
}

/**
 * A value left as it is under a credential field: empty, a reference, or one already holding a
 * mask. The last is what makes a second pass a no-op: a reference kept on the first pass
 * (`$AKIA...`, `${...}`) can have a shape masked inside it, and what is left (`$<secret:...>`) is
 * no longer a reference but must not be masked again.
 */
const keeps = (value) => !value || REFERENCE.test(value) || MASK.test(value);

function unquote(token) {
  if (token.startsWith("'")) return token.slice(1, -1);
  try {
    return JSON.parse(token);
  } catch {
    return token.slice(1, -1);
  }
}

function shapes(text) {
  for (const shape of SHAPES) {
    let out = '';
    let last = 0;
    shape.lastIndex = 0;
    for (let m = shape.exec(text); m !== null; m = shape.exec(text)) {
      const [start, end] = m.indices.groups.v;
      out += text.slice(last, start) + secret(m.groups.v);
      last = end;
    }
    text = out + text.slice(last);
  }
  return text;
}

/** `text` with every credential field's value and every credential shape masked. */
export function credentials(text, exempt = []) {
  const ex = new Set(exempt);
  // A pass masks a value only when its field's name holds a credential and the value is not one
  // kept as it is; every other match is written back unchanged. The passes run in this order.
  const masks = (name, value) => secretField(name, ex) && !keeps(value);
  const pair = ({ k, sep, v }) => {
    // A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
    const name = k[0] === '"' || k[0] === "'" ? unquote(k) : lastPart(k);
    const value = unquote(v);
    return masks(name, value) ? `${k}${sep}${v[0]}${secret(value)}${v[0]}` : null;
  };
  const escaped = ({ q, k, sep, v }) => (masks(k, v) ? `${q}"${k}${q}"${sep}${q}"${secret(v)}${q}"` : null);
  const escapedValue = (whole, ...rest) => {
    const { k, sep, q, v } = rest.at(-1);
    return masks(lastPart(k), v) ? `${k}${sep}${q}"${secret(v)}${q}"` : whole;
  };

  // The passes run until the text stops changing. A later pass can change how an earlier one
  // reads the text: a shape or an assignment masks the key of a pair that had swallowed the next
  // pair's opening quote, and the next round reads that pair. Repeating makes the result a fixed
  // point, so a second scrub is a no-op by construction (a replay re-scrubs what it read off the
  // tape and must find it unchanged). It ends: every round that changes the text replaces at
  // least one character outside the masks with a mask, and a mask is never masked again. Text
  // that holds no credential costs one round.
  for (;;) {
    const before = text;
    text = chained(PAIR, text, 'v', pair);
    text = chained(ESCAPED_PAIR, text, 'q2', escaped);
    text = text.replace(ESCAPED_VALUE, escapedValue);
    text = assignments(text, masks);
    text = shapes(text);
    if (text === before) return text;
  }
}

/**
 * A global replace of `search`'s matches by `mask(groups)`, except that a pair left as it is
 * (`mask` returns null) gives its value back when that value opens a pair of its own.
 *
 * In `x = "api_key": "v1"` the pair `x = "api_key"` is not a credential, and a scan that resumed
 * after its value never saw `"api_key": "v1"`. So the scan tries one pair anchored at the value's
 * start (`group` names where the value's token begins), and only if none opens there does it
 * skip the value whole: resuming inside a quoted value would read its escaped quotes as the
 * openings of new tokens.
 */
function chained([search, sticky], text, group, mask) {
  let out = '';
  let pos = 0;
  let anchor = null;
  let skip = 0;
  for (;;) {
    let m;
    if (anchor !== null) {
      sticky.lastIndex = anchor; // the lookbehinds still see the text before it
      m = sticky.exec(text);
    } else {
      search.lastIndex = pos;
      m = search.exec(text);
    }
    if (m === null) {
      if (anchor !== null) {
        anchor = null;
        out += text.slice(pos, skip);
        pos = skip;
        continue;
      }
      return out + text.slice(pos);
    }
    anchor = null;
    const replaced = mask(m.groups);
    if (replaced === null) {
      const [start] = m.indices.groups[group];
      out += text.slice(pos, start);
      pos = anchor = start;
      skip = m.index + m[0].length;
      continue;
    }
    out += text.slice(pos, m.index) + replaced;
    pos = m.index + m[0].length;
  }
}

/**
 * The unquoted assignments masked, scanning left to right.
 *
 * Unlike the other passes, an assignment left as it is gives its value back to the scan: the
 * search resumes right after its name, not after its value. In `a=token=x` the value of `a` holds
 * an assignment of its own, and in `AKIA...=Bearer = v` a shape masked later turns the first name
 * into a mask and frees `Bearer = v`; a scan that had swallowed it would mask it only on a second
 * pass, and a scrub must be a no-op on its own output. A value directly followed by a mask is the
 * rest of a value already masked (`$env:<secret:...>`) and is kept, as is one a reference opens.
 */
function assignments(text, masks) {
  let out = '';
  let pos = 0;
  for (;;) {
    ASSIGN.lastIndex = pos; // the lookbehind still sees the text before `pos`
    const m = ASSIGN.exec(text);
    if (m === null) return out + text.slice(pos);
    const { k, v } = m.groups;
    const [, endK] = m.indices.groups.k;
    const [startV, endV] = m.indices.groups.v;
    MASK_AT.lastIndex = endV;
    REFERENCE_AT.lastIndex = startV;
    if (!masks(lastPart(k), v) || MASK_AT.test(text) || REFERENCE_AT.test(text)) {
      out += text.slice(pos, endK);
      pos = endK;
      continue;
    }
    out += text.slice(pos, startV) + secret(v);
    pos = endV;
  }
}

const isPlain = (v) => {
  const proto = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
};

/**
 * `credentials` for parsed data: a string under a credential field is masked whole, any other
 * string as text. Plain objects, Maps and arrays are walked; other values are kept.
 */
export function credentialsData(value, exempt = [], key = null) {
  const ex = exempt instanceof Set ? exempt : new Set(exempt);
  if (Array.isArray(value)) return value.map((x) => credentialsData(x, ex));
  if (value instanceof Map) {
    return new Map([...value].map(([k, x]) => [k, credentialsData(x, ex, String(k))]));
  }
  if (value !== null && typeof value === 'object' && isPlain(value)) {
    const out = {};
    for (const [k, x] of Object.entries(value)) out[k] = credentialsData(x, ex, k);
    return out;
  }
  if (typeof value === 'string') {
    if (key !== null && secretField(key, ex) && !keeps(value)) return secret(value);
    return credentials(value, ex);
  }
  return value;
}
