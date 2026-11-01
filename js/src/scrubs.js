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
// password in a URL, a secret in a query string), and in JSON, YAML or TOML text, the value of
// every field whose name says it holds a credential. `credentialsData` masks what that text
// parses to, and the two agree: parsing masked text gives the masked parse.
//
// A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the value's sha256: deterministic,
// so a replay re-derives the same mask, and distinct, so two different keys stay two different
// masks. Both functions are idempotent (a mask is never masked again), which a boundary's
// `scrub` requires. A value under a credential field is kept when it is empty, a variable
// reference (`${TOKEN}`, `{env:TOKEN}`, `%TOKEN%`) or already a mask.
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
const MASK_WHOLE = /^<secret:[0-9a-f]{8}>$/;

/** A field whose name says it holds a credential. */
export const SECRET_FIELD = /key(?!words?|board)|secret|token|passw|auth(?!or(?!i))|credential|cookie|session|bearer/i;

/** A variable reference names a secret without holding it. */
const REFERENCE = /^(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%)$/;

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
const QUOTED = String.raw`"(?:[^"\\\n]|\\[^\n])*"|'[^'\n]*'`;
const PAIR = new RegExp(
  String.raw`(?<k>${QUOTED}|(?<![\w.-])[A-Za-z0-9_.-]+)(?<sep>[${WS}]*[:=][${WS}]*)(?<v>${QUOTED})`,
  'g',
);

/** The mask of one value: `<secret:` + the first 8 hex of its UTF-8 sha256 + `>`. */
export function secret(value) {
  return `<secret:${createHash('sha256').update(value, 'utf8').digest('hex').slice(0, 8)}>`;
}

/** Whether a field of this name holds a credential, unless the app exempted the name. */
export function secretField(name, exempt = []) {
  return !new Set(exempt).has(name) && SECRET_FIELD.test(name);
}

/** A value left as it is under a credential field: empty, a reference, already a mask. */
const keeps = (value) => !value || REFERENCE.test(value) || MASK_WHOLE.test(value);

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
  const masked = text.replace(PAIR, (whole, ...rest) => {
    const { k, sep, v } = rest.at(-1);
    // A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
    const name = k[0] === '"' || k[0] === "'" ? unquote(k) : k.slice(k.lastIndexOf('.') + 1);
    const value = unquote(v);
    if (!secretField(name, ex) || keeps(value)) return whole;
    return `${k}${sep}${v[0]}${secret(value)}${v[0]}`;
  });
  return shapes(masked);
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
