// Tape v1 conformance checker — the Node mirror of spec/validate.py.
//
// These two files are the same claim written twice, on purpose. The tape is the contract
// between the runtimes, and the only way to know a contract holds is to have two parties
// independently agree about the same artifact: both checkers run against the same fixtures
// in spec/fixtures/, and a disagreement means the tape has forked — which is the single
// failure this whole arrangement exists to prevent.
//
// Like its Python twin it imports nothing from the recorder, so it cannot accidentally
// bless whatever an implementation happens to do. It knows only JSON and the spec.
//
// Returns an array of human-readable violations; empty means conformant.

export const VERSION = 1;
export const MAX_DEPTH = 16;

const MARKERS = new Set(['__dt__', '__date__', '__opaque__']);
// Reserved by the trace encoding: a reader must tolerate them even though a v1 recorder
// never emits them.
const RESERVED_MARKERS = new Set(['__snap__', '__seq__', '__str__', '__esc__']);
const EVENT_KINDS = new Set(['fx', 'db', 'now', 'rand']);

// ISO-8601 as Python's datetime.fromisoformat accepts it, which is what writes these tapes.
// Deliberately strict about shape and permissive about the offset, because whether an
// offset is present is *meaningful* (see the `now` event below) and must not be normalised
// away by a lenient parse.
const ISO = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/;
const HAS_OFFSET = /(Z|[+-]\d{2}:?\d{2})$/;

const isIso = (s) => typeof s === 'string' && ISO.test(s);
const isTzAware = (s) => isIso(s) && HAS_OFFSET.test(s);
const isInt = (v) => Number.isInteger(v);
const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

function checkValue(v, path, out, depth = 0) {
  if (depth > MAX_DEPTH) {
    out.push(`${path}: nested deeper than ${MAX_DEPTH}; must degrade to __opaque__`);
    return;
  }
  if (v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return;

  if (Array.isArray(v)) {
    v.forEach((x, i) => checkValue(x, `${path}[${i}]`, out, depth + 1));
    return;
  }

  if (isPlainObject(v)) {
    const keys = Object.keys(v);
    if (keys.length === 1) {
      const k = keys[0];
      if (MARKERS.has(k)) {
        if ((k === '__dt__' || k === '__date__') && !isIso(v[k])) {
          out.push(`${path}: ${k} payload is not ISO-8601: ${JSON.stringify(v[k])}`);
        }
        if (k === '__opaque__') {
          if (typeof v[k] !== 'string') out.push(`${path}: __opaque__ payload must be a string`);
          else if (v[k].length > 200) out.push(`${path}: __opaque__ payload exceeds 200 chars`);
        }
        return;
      }
      if (RESERVED_MARKERS.has(k)) return; // reserved: legal, not interpreted here
    }
    for (const [k, x] of Object.entries(v)) checkValue(x, `${path}.${k}`, out, depth + 1);
    return;
  }

  out.push(`${path}: ${typeof v} is not JSON`);
}

function checkSnapshot(s, path, out) {
  if (!isPlainObject(s)) {
    out.push(`${path}: snapshot must be an object`);
    return;
  }
  for (const key of ['id', 'exists', 'data']) {
    if (!(key in s)) out.push(`${path}: snapshot missing '${key}'`);
  }
  if ('exists' in s && typeof s.exists !== 'boolean') out.push(`${path}.exists: must be a bool`);
  if ('data' in s) checkValue(s.data, `${path}.data`, out);
}

function checkEvent(e, path, out) {
  if (!isPlainObject(e)) {
    out.push(`${path}: event must be an object`);
    return;
  }
  const k = e.k;
  if (!EVENT_KINDS.has(k)) return; // unknown kind: a reader must ignore it

  if (k === 'fx') {
    if (typeof e.fn !== 'string') out.push(`${path}: fx needs a string 'fn'`);
    if (!Array.isArray(e.args)) out.push(`${path}: fx needs an array 'args'`);
    else checkValue(e.args, `${path}.args`, out);
    if (!isPlainObject(e.kwargs)) out.push(`${path}: fx needs an object 'kwargs' ({} in JS)`);
    else checkValue(e.kwargs, `${path}.kwargs`, out);

    const hasRes = 'res' in e;
    const hasErr = 'err' in e;
    if (hasRes === hasErr) out.push(`${path}: fx must carry exactly one of 'res' / 'err'`);
    if (hasRes) checkValue(e.res, `${path}.res`, out);
    if (hasErr && (!isPlainObject(e.err) || typeof e.err.type !== 'string')) {
      out.push(`${path}.err: must be an object with a string 'type'`);
    }
    return;
  }

  if (k === 'db') {
    if (typeof e.op !== 'string') out.push(`${path}: db needs a string 'op'`);
    if (typeof e.sig !== 'string') out.push(`${path}: db needs a string 'sig'`);

    const hasRes = 'res' in e;
    const hasArgs = 'args' in e;
    if (hasRes && hasArgs) out.push(`${path}: db carries 'res' (a read) or 'args' (a write), never both`);
    if (!hasRes && !hasArgs) out.push(`${path}: db must carry 'res' or 'args'`);

    if (hasRes) {
      if (Array.isArray(e.res)) e.res.forEach((s, i) => checkSnapshot(s, `${path}.res[${i}]`, out));
      else checkSnapshot(e.res, `${path}.res`, out);
    }
    if (hasArgs) checkValue(e.args, `${path}.args`, out);
    return;
  }

  if (k === 'now') {
    // ISO-8601, and NOT required to be timezone-aware — this is an app-visible value, not
    // recorder metadata. The app called now() and got back whatever it got back; Python's
    // datetime.now() is naive, and comparing naive with aware raises. A replay that
    // normalised it to aware would change behaviour, which replay may never do.
    if (!isIso(e.v)) out.push(`${path}: now.v must be an ISO-8601 string, got ${JSON.stringify(e.v)}`);
    return;
  }

  if (k === 'rand') {
    if (e.m !== 'sample') out.push(`${path}: rand.m must be 'sample' in v1, got ${JSON.stringify(e.m)}`);
    for (const key of ['n', 'kk']) {
      if (!isInt(e[key])) out.push(`${path}: rand.${key} must be an int`);
    }
    if (!Array.isArray(e.idx) || !e.idx.every(isInt)) {
      out.push(`${path}: rand.idx must be an array of ints`);
    } else if (isInt(e.n)) {
      const bad = e.idx.filter((i) => !(i >= 0 && i < e.n));
      if (bad.length) out.push(`${path}: rand.idx ${JSON.stringify(bad)} out of range for population ${e.n}`);
      if (isInt(e.kk) && e.idx.length !== e.kk) {
        out.push(`${path}: rand.idx has ${e.idx.length} positions but kk=${e.kk}`);
      }
    }
  }
}

function validateLine(obj, i, out, { first }) {
  if (!isPlainObject(obj)) {
    out.push(`line ${i}: not an object`);
    return;
  }
  const ev = obj.ev;

  if (first) {
    if (ev !== 'session') {
      out.push(`line ${i}: the first line must be the session header, got ev=${JSON.stringify(ev)}`);
      return;
    }
  } else if (ev === 'session') {
    out.push(`line ${i}: a second session header`);
    return;
  }

  if (ev === 'session') {
    if (obj.version !== VERSION) out.push(`line ${i}: version must be ${VERSION}, got ${JSON.stringify(obj.version)}`);
    if (!isTzAware(obj.started)) out.push(`line ${i}: session.started must be timezone-aware ISO-8601`);
    if (!isPlainObject(obj.constants)) out.push(`line ${i}: session.constants must be an object`);
    else checkValue(obj.constants, `line ${i}.constants`, out);

    const runtimes = ['python', 'node'].filter((k) => k in obj);
    if (runtimes.length !== 1) {
      out.push(`line ${i}: session must name exactly one runtime (python|node), got [${runtimes}]`);
    }
    return;
  }

  if (ev === 'call') {
    if (!isInt(obj.seq) || obj.seq < 1) out.push(`line ${i}: call.seq must be an int >= 1`);
    if (typeof obj.fn !== 'string') out.push(`line ${i}: call.fn must be a string`);
    if (!isPlainObject(obj.kwargs)) out.push(`line ${i}: call.kwargs must be an object`);
    else checkValue(obj.kwargs, `line ${i}.kwargs`, out);
    if ('result' in obj) checkValue(obj.result, `line ${i}.result`, out);

    if (!('error' in obj)) out.push(`line ${i}: call must carry 'error' (null when it did not raise)`);
    else if (obj.error !== null && typeof obj.error !== 'string') {
      out.push(`line ${i}: call.error must be a string or null`);
    }

    if (!isTzAware(obj.ts)) out.push(`line ${i}: call.ts must be timezone-aware ISO-8601`);
    if (typeof obj.ms !== 'number') out.push(`line ${i}: call.ms must be a number`);

    if (!Array.isArray(obj.events)) out.push(`line ${i}: call.events must be an array`);
    else obj.events.forEach((e, j) => checkEvent(e, `line ${i}.events[${j}]`, out));
    return;
  }

  // unknown ev (e.g. the reserved "inflight"): a reader must tolerate it.
}

/** Validate a whole tape. Returns violations; empty means conformant. */
export function validateTape(text) {
  const out = [];
  const lines = text.split('\n').filter((ln) => ln.trim());
  if (!lines.length) return ['empty tape: the session header is mandatory'];

  const seqs = [];
  lines.forEach((ln, i) => {
    let obj;
    try {
      obj = JSON.parse(ln);
    } catch (e) {
      // Only the final line may be torn (the process died mid-write).
      if (i === lines.length - 1) return;
      out.push(`line ${i}: not JSON (${e.message})`);
      return;
    }
    validateLine(obj, i, out, { first: i === 0 });
    if (isPlainObject(obj) && obj.ev === 'call' && isInt(obj.seq)) seqs.push(obj.seq);
  });

  const expected = seqs.map((_, i) => i + 1);
  if (String(seqs) !== String(expected)) {
    out.push(`call.seq must be 1-based and monotonic; got [${seqs}]`);
  }

  return out;
}
