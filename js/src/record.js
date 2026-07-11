// Recording — the Node half. Emits tape format v1 (see spec/tape-v1.md).
//
// HOW THIS DIFFERS FROM THE PYTHON RECORDER, AND WHY
//
// Python declares its boundary by naming module functions, and patches them with setattr.
// That is not available here: an ES module's namespace is immutable, so
// `import * as fx from './effects.js'; fx.fetch = wrapped` throws. There is no way to
// reach behind an ESM import and swap what a caller already bound.
//
// So the boundary in JS is declared by *wrapping the objects the app holds*: the app asks
// for a recorded client and uses that. This is not mocking and not duplication — `wrap()`
// returns a transparent Proxy that forwards every call to the real thing and records what
// came back. The cardinal rule survives intact: nothing here evaluates a query,
// reimplements a client, or knows what any value means. It knows names.
//
// The exception is genuinely global state — the clock and the RNG — which is patched on
// the global object, because there the app holds nothing to wrap.

import { AsyncLocalStorage } from 'node:async_hooks';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { toJsonable, redactJsonable, short } from './serial.js';

export const FORMAT_VERSION = 1;

// The per-call event buffer. AsyncLocalStorage is the contextvar equivalent: it follows
// the call across every await, so concurrent tool calls never interleave their events.
const active = new AsyncLocalStorage();

let recorder = null;
let boundary = null;
let gate = null;
const patches = []; // [target, key, original]

function emit(ev) {
  const buf = active.getStore();
  if (buf) buf.push(scrub(ev));
}

const PAYLOAD_KEYS = ['args', 'kwargs', 'res', 'result'];

function scrub(ev) {
  const rules = boundary?.redact;
  if (!rules) return ev;
  const out = { ...ev };
  for (const k of PAYLOAD_KEYS) {
    if (k in out) out[k] = redactJsonable(out[k], rules);
  }
  return out;
}

// --- the tape ----------------------------------------------------------------------

class Recorder {
  constructor(directory, b) {
    this.dir = directory;
    fs.mkdirSync(this.dir, { recursive: true });

    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+/, '');
    this.path = path.join(this.dir, `flight-${stamp}-${process.pid}.jsonl`);
    this.seq = 0;

    this.write({
      ev: 'session',
      version: FORMAT_VERSION,
      started: isoLocal(new Date()),
      node: process.versions.node,
      constants: toJsonable(b.constants ?? {}),
    });
  }

  write(obj) {
    // Append-only, one complete line per write: the only corruption possible is a torn
    // final line, which every reader is required to tolerate.
    fs.appendFileSync(this.path, JSON.stringify(obj) + '\n', 'utf8');
  }

  writeCall(fn, kwargs, events, result, error, ms) {
    this.seq += 1;
    this.write({
      ev: 'call',
      seq: this.seq,
      fn,
      kwargs: redactJsonable(toJsonable(kwargs), boundary?.redact),
      events,
      result: redactJsonable(toJsonable(result), boundary?.redact),
      error,
      ts: isoLocal(new Date()),
      ms: Math.round(ms * 100) / 100,
    });
  }
}

/** ISO-8601 with the local UTC offset — the tape wants aware timestamps for metadata. */
function isoLocal(d) {
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, '0');
  const local = new Date(d.getTime() + off * 60000).toISOString().slice(0, -1);
  return `${local}${sign}${pad(off / 60)}:${pad(off % 60)}`;
}

// --- tools: the call boundary ------------------------------------------------------

/**
 * Wrap a tool. One recorded line per call — and that line IS the execution, because the
 * code is deterministic given the answers the world gave it.
 *
 * `kwargs` is the tool's single argument object, which is how MCP tools are called anyway.
 */
export function tool(name, fn) {
  const wrapped = async function (args = {}, ...rest) {
    if (!recorder || (gate && !gate(name, args))) return fn.call(this, args, ...rest);

    const events = [];
    const t0 = performance.now();

    return active.run(events, async () => {
      let result;
      let error = null;
      try {
        result = await fn.call(this, args, ...rest);
        return result;
      } catch (e) {
        error = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
        throw e;
      } finally {
        // Recording must never be the reason a call fails. A tape we could not write is
        // strictly less bad than an app that fell over because we tried.
        try {
          recorder.writeCall(name, args, events, result, error, performance.now() - t0);
        } catch (e) {
          console.warn('flight-recorder: could not write the call —', e.message);
        }
      }
    });
  };
  wrapped.__flight_wrapped__ = fn;
  Object.defineProperty(wrapped, 'name', { value: name });
  return wrapped;
}

// --- effects: a transparent recording proxy ----------------------------------------

/**
 * Wrap a client so the named methods are recorded as `fx` events.
 *
 * Returns a Proxy: everything not named passes straight through, untouched and unwatched.
 * `this` is bound to the real target, so a client whose methods call each other internally
 * keeps working — and those internal hops are NOT double-recorded, because the inner call
 * goes to the raw method, not back through the proxy.
 */
export function wrap(target, methods, { prefix = '' } = {}) {
  const names = new Set(methods);
  const tag = (m) => (prefix ? `${prefix}.${m}` : m);

  return new Proxy(target, {
    get(t, propKey, receiver) {
      const value = Reflect.get(t, propKey, receiver);
      if (typeof propKey !== 'string' || !names.has(propKey) || typeof value !== 'function') {
        return value;
      }

      return function (...args) {
        if (!recorder || !active.getStore()) return value.apply(t, args);

        const ev = {
          k: 'fx',
          fn: tag(propKey),
          args: args.map((a) => toJsonable(a)),
          kwargs: {}, // JS has no kwargs; the spec fixes this at {}
        };

        let res;
        try {
          res = value.apply(t, args);
        } catch (e) {
          ev.err = errEvent(e);
          emit(ev);
          throw e;
        }

        // A promise is recorded when it settles — the answer is what the world gave, and
        // it has not given it yet.
        if (res && typeof res.then === 'function') {
          return res.then(
            (r) => {
              ev.res = toJsonable(r);
              emit(ev);
              return r;
            },
            (e) => {
              ev.err = errEvent(e);
              emit(ev);
              throw e;
            },
          );
        }

        ev.res = toJsonable(res);
        emit(ev);
        return res;
      };
    },
  });
}

function errEvent(e) {
  const args = e instanceof Error && Array.isArray(e.args) ? e.args : [];
  return {
    type: e?.name ?? typeof e,
    repr: String(e?.stack ?? e).slice(0, 300),
    args: toJsonable(args),
  };
}

// --- the clock and the RNG: the only truly global doors ------------------------------

function patch(target, key, replacement) {
  patches.push([target, key, target[key]]);
  target[key] = replacement;
}

function installClock() {
  const realNow = Date.now.bind(Date);
  patch(Date, 'now', () => {
    const ms = realNow();
    emit({ k: 'now', v: new Date(ms).toISOString() });
    return ms;
  });
}

function installRandom() {
  const realBytes = crypto.randomBytes.bind(crypto);
  patch(crypto, 'randomBytes', (n, cb) => {
    if (cb) return realBytes(n, cb); // async form: out of scope, passed straight through
    const buf = realBytes(n);
    emit({ k: 'rand', m: 'bytes', n, hex: buf.toString('hex') });
    return buf;
  });

  const realUuid = crypto.randomUUID.bind(crypto);
  patch(crypto, 'randomUUID', (opts) => {
    const uuid = realUuid(opts);
    emit({ k: 'rand', m: 'bytes', n: 16, hex: uuid.replace(/-/g, '') });
    return uuid;
  });
}

// --- install ------------------------------------------------------------------------

/**
 * Declare the boundary. The one app-specific artifact.
 *
 * @param {object} o
 * @param {object} [o.constants]  "module.NAME" → value, snapshotted into the header
 * @param {object} [o.redact]     field name → null (mask) or an idempotent transform
 * @param {boolean} [o.clock]     record Date.now()
 * @param {boolean} [o.random]    record crypto.randomBytes() / randomUUID()
 */
export function boundaryOf(o = {}) {
  return {
    constants: o.constants ?? {},
    redact: o.redact ?? {},
    clock: o.clock ?? true,
    random: o.random ?? true,
  };
}

/**
 * Turn recording on.
 *
 * `gate` (fn, args) => bool decides per call, so production can record 1-in-N or only the
 * calls that matter. A gate that never says yes must leave no tape behind at all, which is
 * why the file is opened by the first admitted call rather than here.
 */
export function install(b, { directory = '.flight', enabled = true, gate: g = null } = {}) {
  if (!enabled) return null;

  boundary = b;
  gate = g;
  recorder = new Recorder(directory, b);

  if (b.clock) installClock();
  if (b.random) installRandom();

  return recorder.path;
}

export function uninstall() {
  while (patches.length) {
    const [target, key, original] = patches.pop();
    target[key] = original;
  }
  recorder = null;
  boundary = null;
  gate = null;
}

/** The tape being written, or null. */
export function tapePath() {
  return recorder?.path ?? null;
}
