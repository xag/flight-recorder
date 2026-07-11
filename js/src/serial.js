// Boundary value (de)serialization — the JS half of spec/tape-v1.md's "Value encoding".
//
// Everything crossing the boundary is encoded as JSON with revivable markers for dates;
// anything exotic degrades to an opaque marker rather than breaking the recorded call.
// The failure direction is always "the recording is a bit poorer", never "the app broke
// because it was being recorded".

const MAX_DEPTH = 16;
export const REDACTED = '[REDACTED]';

const MARKERS = new Set(['__dt__', '__date__', '__opaque__']);

function safeRepr(v, limit = 200) {
  let s;
  try {
    if (typeof v === 'function') s = `<function ${v.name || 'anonymous'}>`;
    else if (typeof v === 'symbol') s = v.toString();
    else if (typeof v === 'bigint') s = `${v}n`;
    else if (v instanceof Error) s = `${v.name}: ${v.message}`;
    else s = Object.prototype.toString.call(v);
  } catch {
    return '<unreprable>';
  }
  return s.length <= limit ? s : s.slice(0, limit - 1) + '…';
}

const opaque = (v) => ({ __opaque__: safeRepr(v) });

/**
 * Encode one boundary value.
 *
 * KNOWN, DELIBERATE LOSS: JavaScript has two nothings and the tape has one. `undefined`
 * encodes as `null`. Introducing an `__undef__` marker would be a fork in all but name —
 * the Python reader would revive it as a plain dict and every invariant would have to know
 * about it — and the alternative, dropping the key entirely, silently changes an object's
 * shape. `null` is the honest lossy choice, and it is written down here rather than
 * discovered later. If a boundary is ever found where the distinction is load-bearing,
 * adding the marker is a non-breaking change (see "Changing this" in the spec).
 */
export function toJsonable(v, depth = 0) {
  if (depth > MAX_DEPTH) return opaque(v);
  if (v === undefined || v === null) return null;

  const t = typeof v;
  if (t === 'string' || t === 'boolean') return v;
  if (t === 'number') return Number.isFinite(v) ? v : opaque(v); // NaN/±Infinity are not JSON

  if (v instanceof Date) {
    return Number.isNaN(v.getTime()) ? opaque(v) : { __dt__: v.toISOString() };
  }

  if (Array.isArray(v)) return v.map((x) => toJsonable(x, depth + 1));

  // Buffers/typed arrays: hex, tagged opaque. They are entropy or payloads, not structure.
  if (ArrayBuffer.isView(v)) {
    return { __opaque__: `<bytes ${v.byteLength}: ${Buffer.from(v.buffer, v.byteOffset, Math.min(v.byteLength, 32)).toString('hex')}>` };
  }

  if (v instanceof Map) {
    const out = {};
    for (const [k, x] of v) out[String(k)] = toJsonable(x, depth + 1);
    return out;
  }
  if (v instanceof Set) return [...v].map((x) => toJsonable(x, depth + 1));

  if (t === 'object') {
    // Only plain-ish objects are structure. A class instance is opaque: its shape is not
    // the surface a consumer reads, and reviving it faithfully is impossible anyway.
    const proto = Object.getPrototypeOf(v);
    if (proto !== null && proto !== Object.prototype) return opaque(v);

    const out = {};
    for (const [k, x] of Object.entries(v)) out[String(k)] = toJsonable(x, depth + 1);
    return out;
  }

  return opaque(v); // function, symbol, bigint
}

/** Revive a boundary value. `__opaque__` is a one-way door by design — it revives as its text. */
export function fromJsonable(v) {
  if (Array.isArray(v)) return v.map(fromJsonable);
  if (v !== null && typeof v === 'object') {
    const keys = Object.keys(v);
    if (keys.length === 1) {
      if ('__dt__' in v) return new Date(v.__dt__);
      if ('__date__' in v) return new Date(v.__date__);
      if ('__opaque__' in v) return v.__opaque__;
    }
    const out = {};
    for (const [k, x] of Object.entries(v)) out[k] = fromJsonable(x);
    return out;
  }
  return v;
}

/**
 * Apply field-name redaction rules to a jsonable tree.
 *
 * A rule that throws degrades to REDACTED: the failure direction is "masked", never
 * "leaked" and never "broke the recorded call".
 *
 * Rules MUST be idempotent. Replay re-derives the question it is about to ask, scrubs it
 * the same way, and compares against the tape — so a value that is already a mask has to
 * scrub to itself, or a redacted recording could never be replayed.
 */
export function redactJsonable(v, rules) {
  if (!rules || !Object.keys(rules).length) return v;

  if (Array.isArray(v)) return v.map((x) => redactJsonable(x, rules));

  if (v !== null && typeof v === 'object') {
    const out = {};
    for (const [k, x] of Object.entries(v)) {
      if (Object.hasOwn(rules, k)) {
        const rule = rules[k];
        if (rule === null || rule === undefined) {
          out[k] = REDACTED;
        } else {
          try {
            out[k] = rule(x);
          } catch {
            out[k] = REDACTED;
          }
        }
      } else {
        out[k] = redactJsonable(x, rules);
      }
    }
    return out;
  }

  return v;
}

/** Compact stable rendering of a chained-call argument, for `db` signatures. */
export function short(v, limit = 60) {
  let s;
  try {
    s = JSON.stringify(toJsonable(v));
  } catch {
    s = safeRepr(v);
  }
  if (s === undefined) s = 'undefined';
  return s.length <= limit ? s : s.slice(0, limit - 1) + '…';
}

export { MARKERS, MAX_DEPTH, safeRepr };
