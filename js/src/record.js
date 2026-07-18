// Recording. Emits tape format v1 (see spec/tape-v1.md).
//
// WHY THE BOUNDARY IS DECLARED BY WRAPPING
//
// An ES module's namespace is immutable: `import * as fx from './effects.js'; fx.fetch = w`
// throws. There is no way to reach behind an import and swap what a caller already bound, so
// a boundary cannot be declared by naming module functions.
//
// It is declared by wrapping the objects the app HOLDS. `wrap()` returns a transparent Proxy
// that forwards every call to the real thing and records what came back — not a mock, not a
// duplicate. The cardinal rule holds: nothing here evaluates a query, reimplements a client,
// or knows what any value means. It knows names.
//
// The exception is genuinely global state — the clock and the RNG — which is patched on the
// global object, because there the app holds nothing to wrap.

import { AsyncLocalStorage } from 'node:async_hooks';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { toJsonable, redactJsonable, short } from './serial.js';
import { ReplayDivergence, ProbeUnanswerable, ForbiddenValue } from './errors.js';

// Captured at module load, before any shim replaces them. THE RECORDER MUST NEVER USE THE
// SHIMMED GLOBALS: it stamps every line with the time and measures every call's duration,
// and if those calls went through the shims it would write clock events the app never asked
// for — and consume them on replay. The instrument would be recording itself.
const RealDate = globalThis.Date;
const realPerfNow = performance.now.bind(performance);
const realRandomBytes = crypto.randomBytes.bind(crypto);

export const FORMAT_VERSION = 1;

// The per-call event buffer. AsyncLocalStorage is the contextvar equivalent: it follows
// the call across every await, so concurrent tool calls never interleave their events.
const active = new AsyncLocalStorage();

/**
 * The one piece of shared state between recording and replay.
 *
 * The SAME wrapped client and the SAME clock/RNG shims serve both modes — that is what
 * makes replay a resurrection of the original execution rather than a re-enactment of it.
 * In `record` they ask the world and write down the answer; in `replay` they answer from
 * the tape and never touch the world at all. The app cannot tell the difference.
 */
export const hook = { mode: null, feed: null, sems: null };

/**
 * True while a wrapped effect is running the REAL client.
 *
 * A store client calls `performance.now()` for its own timing, an HTTP client calls
 * `Math.random()` for a jitter, a mailer calls `Date.now()` for a message id. None of that is
 * the app asking the world anything: it is the world's own machinery, on the far side of a
 * boundary whose answer is already being written down.
 *
 * Recording it would be worse than noisy: on replay the real client never runs, so it never
 * asks, and the tape's answers to questions nobody asked sit there unconsumed — surfacing as
 * a divergence on the very first event, pointing at the app, which did nothing wrong.
 *
 * So: while inside an effect, emission is suppressed. The effect's own event is emitted
 * outside this context, after it settles.
 */
const insideEffect = new AsyncLocalStorage();

let recorder = null;
let boundary = null;
let gate = null;
let deferrer = null;
const patches = []; // [target, key, original]

function emit(ev) {
  if (insideEffect.getStore()) return; // the far side of a door we already record
  const buf = active.getStore();
  if (buf) buf.push(scrub(ev));
}

// `compileForbid` and `guard` are exported for the OTHER writers in this package — render.js
// writes a session-format tape of its own, with its own file handle. A tripwire that only
// covered the writer it was born in would be a property that holds for one file and not the
// tape sitting next to it, which is not a property at all.
export { scrub, active, patch, isoLocal, compileForbid, guard };

// `data` is a semantic event's payload and is scrubbed like any other: testimony is written
// by the app, about the app's own values, and is exactly as likely to carry a credential as an
// effect's arguments are.
const PAYLOAD_KEYS = ['args', 'kwargs', 'res', 'result', 'data'];

function scrub(ev) {
  const rules = boundary?.redact;
  const sweep = boundary?.scrub;
  if (!rules && !sweep) return ev;
  const out = { ...ev };
  for (const k of PAYLOAD_KEYS) {
    if (k in out) out[k] = redactJsonable(out[k], rules, sweep);
  }
  return out;
}

// --- the tripwire: the backstop `redact` and `scrub` cannot be ----------------------
//
// Masking is declarative and opt-in, so it protects exactly the fields and values you thought
// of, and its failure mode is silent and open: forget `salt` and the tape leaks; add
// `recovery_token` to the model next month and the tape leaks; rename a field and the rule
// quietly stops matching. Nothing tells you. `scrub` narrows the gap but does not close it —
// it catches secrets whose VALUE you can name, and by construction neither rule can reach the
// one you never thought to name at all: a positional argument, a key, an opaque repr, a string
// some effect built by concatenation.
//
// `forbid` states the property those rules cannot: THIS TAPE CARRIES NO CREDENTIAL. It turns
// "I forgot one" from an invisible leak into a noisy failure at record time.
//
// MATCH SHAPES, NOT VALUES: a credential you can enumerate you can already redact. It is the
// one you cannot name that this is for.
//
//     forbid: [/\b[a-f0-9]{64}\b/,                    // a scrypt digest survived redaction
//              /-----BEGIN [A-Z ]*PRIVATE KEY-----/]

/**
 * Compile a `forbid` declaration once, up front.
 *
 * A tripwire that only reports a bad pattern when it first fires is no tripwire at all: the
 * declaration would look installed and be inert until the exact moment it mattered. So patterns
 * are compiled at declaration time and a bad one throws there, in the line that wrote it, while
 * someone is still looking at it.
 */
function compileForbid(patterns) {
  return (patterns ?? []).map((p) => {
    if (p instanceof RegExp) {
      // A global or sticky regex carries `lastIndex` between calls, so the second line it is
      // asked about starts scanning from wherever the first match ended — a tripwire that
      // silently stops looking partway through a session. Recompile without those flags.
      return p.global || p.sticky ? new RegExp(p.source, p.flags.replace(/[gy]/g, '')) : p;
    }
    try {
      return new RegExp(p);
    } catch (e) {
      throw new SyntaxError(`bad forbid pattern ${JSON.stringify(p)}: ${e.message}`);
    }
  });
}

/**
 * The first pattern that matches `text`, or null if it is clean.
 *
 * Scans the SERIALIZED record, not the value tree, and that is the whole point. Redaction is
 * field-name driven and scrubbing is value-driven; a secret reaches the tape through every path
 * neither can see. The one thing all of those have in common is that they end up in the line
 * about to be written. So the tripwire reads that line.
 *
 * Returns the PATTERN, never the match — see ForbiddenValue.
 */
function forbiddenHit(text, patterns) {
  for (const p of patterns) {
    if (p.test(text)) return p.source;
  }
  return null;
}

/** Refuse to write `line` if a pattern matches it. Free for a boundary that declares none. */
function guard(line, patterns, what) {
  if (!patterns || patterns.length === 0) return;
  const hit = forbiddenHit(line, patterns);
  if (hit !== null) {
    throw new ForbiddenValue(
      `${what} matches a forbidden pattern (${JSON.stringify(hit)}) after redaction — nothing ` +
        `was written. A value that must never reach a tape was about to: name the field in the ` +
        `boundary's redact rules, or widen a rule that has stopped matching, and record again.`,
    );
  }
}

// --- the tape ----------------------------------------------------------------------

class Recorder {
  /**
   * @param {string|null} directory  where to append the tape; null = memory only (serverless)
   * @param {object} b               the boundary
   * @param {{publish(name: string, text: string): unknown}|null} sink
   */
  constructor(directory, b, sink = null, sinkTimeoutMs = 3000) {
    this.dir = directory;
    this.sink = sink;
    this.sinkTimeoutMs = sinkTimeoutMs;
    this.seq = 0;

    // Before the header, because the header is a tape line like any other and is written below.
    // A credential pinned as a constant is still a credential on the tape.
    this.forbid = compileForbid(b.forbid);

    // A UNIQUE name, and the entropy is not decoration.
    //
    // Timestamp-to-the-second plus pid looks unique and is not: serverless instances are
    // separate containers that happily reuse low pids (4 is common) and start within the same
    // second. Two functions of the same app then choose the SAME name, and a sink that stores
    // by name has one tape silently overwrite the other.
    //
    // realRandomBytes, not the shim: naming a tape is not the app asking the world for dice.
    // A shimmed draw here would write a rand event nobody asked for, and consume one on replay.
    const stamp = new RealDate().toISOString().replace(/[-:]/g, '').replace(/\..+/, '');
    const nonce = realRandomBytes(4).toString('hex');
    this.name = `flight-${stamp}-${process.pid}-${nonce}.jsonl`;

    if (this.dir) {
      fs.mkdirSync(this.dir, { recursive: true });
      this.path = path.join(this.dir, this.name);
    } else {
      this.path = null; // nothing to write to; the sink is the tape
    }

    // The full session, mirrored in memory. The sink is handed all of it each time, exactly
    // as the Python recorder does — so a sink that overwrites is enough, and a tape is never
    // half-published.
    this.text = '';

    this.write({
      ev: 'session',
      version: FORMAT_VERSION,
      started: isoLocal(new RealDate()),
      node: process.versions.node,
      constants: toJsonable(b.constants ?? {}),
    });
  }

  write(obj) {
    // Append-only, one complete line per write: the only corruption possible is a torn
    // final line, which every reader is required to tolerate.
    const line = JSON.stringify(obj) + '\n';

    // The tripwire sits HERE, at the single choke point every line passes through — the session
    // header, every call, every fx/now/rand/sem event nested inside one. Guarding the value tree
    // instead would leave exactly the paths a field name cannot reach, which is the leak this
    // exists to catch. Nothing has been appended yet, so a refusal reaches neither the file nor
    // the in-memory mirror the sink is handed: the tape does not merely omit the credential,
    // the line never existed.
    guard(line, this.forbid, `the ${obj.ev} record`);

    this.text += line;
    if (this.path) fs.appendFileSync(this.path, line, 'utf8');
  }

  /**
   * Hand the session to the sink.
   *
   * OFF THE CRITICAL PATH, WHEREVER THE HOST ALLOWS IT.
   *
   * Publishing is telemetry, and telemetry must not sit between the user and their response.
   * But on a host that freezes an instance the moment the response goes out, a publish left
   * in flight is not merely late — it is lost.
   *
   * Both are true, and the resolution is not to pick one. It is `defer`: a host hook
   * (`waitUntil` on Vercel and Cloudflare, `ctx.waitUntil` in a Worker, the AWS extension API)
   * that says *keep this instance alive until this promise settles*. Given one, the response
   * goes out immediately and the tape still lands. Given none, awaiting is the only honest
   * fallback — a slower response beats a lost recording.
   *
   * Either way there is a TIMEOUT. A sink that throws is swallowed; a sink that HANGS would
   * otherwise hold the request open until the platform killed the function, turning a slow store
   * into a slow site.
   */
  flush() {
    if (!this.sink) return null;

    // Inside the suppression context: the sink's own client asks the clock and the dice like
    // any other, and none of that is the app asking the world anything.
    const published = insideEffect.run(true, async () => {
      try {
        await this.sink.publish(this.name, this.text);
      } catch (e) {
        console.warn('flight-recorder: sink publish failed —', e.message);
      }
    });

    let timer;
    const bounded = Promise.race([
      published,
      new Promise((resolve) => {
        timer = setTimeout(() => {
          console.warn(`flight-recorder: sink publish exceeded ${this.sinkTimeoutMs}ms — giving up on it`);
          resolve();
        }, this.sinkTimeoutMs);
      }),
    ]).finally(() => clearTimeout(timer));

    return bounded;
  }

  writeCall(fn, kwargs, events, result, error, ms) {
    this.seq += 1;
    this.write({
      ev: 'call',
      seq: this.seq,
      fn,
      kwargs: redactJsonable(toJsonable(kwargs), boundary?.redact, boundary?.scrub),
      // An effect whose slot was reserved but never settled: the app fired it and did not
      // await it. It gave no answer, so it influenced nothing, and a half-event would be an
      // invalid one (fx carries exactly one of res/err).
      events: events.filter((e) => e.k !== 'fx' || 'res' in e || 'err' in e),
      // A call that RAISED has no return value, and that is not the same as one that
      // returned `undefined` — so it records null, as Python's does. Without this, the
      // __undef__ marker would quietly claim every failed call returned undefined, and the
      // two runtimes would disagree about what a failed call looks like.
      result: error !== null ? null : redactJsonable(toJsonable(result), boundary?.redact, boundary?.scrub),
      error,
      ts: isoLocal(new RealDate()),
      ms: Math.round(ms * 100) / 100,
    });
  }
}

/** ISO-8601 with the local UTC offset — the tape wants aware timestamps for metadata. */
function isoLocal(d) {
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? '+' : '-';
  const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, '0');
  const local = new RealDate(d.getTime() + off * 60000).toISOString().slice(0, -1);
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
    const t0 = realPerfNow();

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
          recorder.writeCall(name, args, events, result, error, realPerfNow() - t0);

          // Publishing is telemetry: hand it to the host if the host will hold the instance
          // open for it (waitUntil), and only block on it if it will not. See Recorder.flush.
          const published = recorder.flush();
          if (published) {
            if (deferrer) deferrer(published);
            else await published;
          }
        } catch (e) {
          // The tripwire is the one recorder failure that is allowed past this net, and it is
          // deliberate. Swallowing it would hand the caller its result and log a warning that
          // says a credential was about to be written down — which is a discovery nobody makes
          // from a `console.warn` in a production log. Refusing is the whole point, so the call
          // fails: better a failed call than a tape you have to go and delete.
          if (e instanceof ForbiddenValue) throw e;
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
        // REPLAY: answer from the tape. The real client is never touched — no network, no
        // database, no waiting for the bug to happen again.
        if (hook.mode === 'replay') {
          return hook.feed.answerEffect(tag(propKey), args.map((a) => toJsonable(a)));
        }

        if (!recorder || !active.getStore()) return value.apply(t, args);

        const ev = {
          k: 'fx',
          fn: tag(propKey),
          args: args.map((a) => toJsonable(a)),
          kwargs: {}, // JS has no kwargs; the spec fixes this at {}
        };

        // RESERVE THE EVENT'S SLOT NOW, in the order the question is ASKED — and fill in the
        // answer when it comes back.
        //
        // Emitting on settlement instead would record events in COMPLETION order. Any
        // concurrent fan-out — `Promise.all(ids.map(id => kv.get(id)))`, which is ordinary code
        // — would then produce a tape whose order no replay can reproduce, because replay asks
        // in issue order.
        const buf = active.getStore();
        const slot = recorder && buf ? buf.push(scrub(ev)) - 1 : -1;
        const settle = (patch) => {
          if (slot >= 0) buf[slot] = scrub({ ...ev, ...patch });
        };

        let res;
        try {
          // The real client runs INSIDE the suppression context, so its own clock/RNG calls
          // — and any it makes across an await — do not reach the tape. The handlers below
          // are attached outside it, so the effect's own event is still recorded.
          res = insideEffect.run(true, () => value.apply(t, args));
        } catch (e) {
          settle({ err: errEvent(e) });
          throw e;
        }

        if (res && typeof res.then === 'function') {
          return res.then(
            (r) => {
              settle({ res: toJsonable(r) });
              return r;
            },
            (e) => {
              settle({ err: errEvent(e) });
              throw e;
            },
          );
        }

        settle({ res: toJsonable(res) });
        return res;
      };
    },
  });
}

/**
 * Record a raised error.
 *
 * `args` carries the exception's CONSTRUCTIVE VALUES — what you would pass to rebuild it. That is
 * what Python records (`e.args`, the exception's own tuple) and what the revivers in `boundaryOf`
 * are handed: `errorRevivers: { NotFound: ([msg]) => new NotFound(msg) }`.
 *
 * A JavaScript `Error` has no `.args`, so this used to record `[]` — and the consequence was quiet
 * and bad. The message went onto the tape only as the first line of `repr`, which is the STACK; the
 * documented reviver above could never receive a message; and the generic fallback rebuilt the
 * error with the stack AS its message. Any code that reads `e.message` — putting it in a log line,
 * an error field, an HTTP body — then produced 300 characters of stack trace on replay where the
 * recording had a sentence, and diverged for a reason that had nothing to do with the app.
 *
 * A JS Error's constructive value IS its message (`new Error(msg)`). So that is what goes in
 * `args`, which makes the two runtimes agree and the reviver contract true.
 */
function errEvent(e) {
  const args = e instanceof Error
    ? (Array.isArray(e.args) ? e.args : [e.message])
    : [];
  return {
    type: e?.name ?? typeof e,
    repr: String(e?.stack ?? e).slice(0, 300),
    args: toJsonable(args),
  };
}

// --- semantic events: the app's testimony, in-stream next to the evidence ------------
//
// A tape records what the world answered. It says nothing about what the execution MEANT —
// and meaning is what a reader is actually looking for. So an app may declare, in its own
// free-text vocabulary, that a stretch of execution constituted a domain-level act, and have
// that declaration recorded in-stream, interleaved with the raw events it encloses.
//
// The cardinal rule is untouched: INSTRUMENT, NEVER DUPLICATE. The library gains no semantics.
// Names and payloads are opaque; nothing validates them, nothing interprets them. A semantic
// event is the app's testimony about its own execution, written down next to the evidence — the
// raw boundary events inside the span. This library records both and judges neither.
//
// Order is the meaning: enclosure is derived from the sequence of begin/end events, so there
// are no parent pointers, and a sid only has to be unique within the call. Spans are call-scoped.
//
// Under replay the same calls fire again, from the same code, and are CAPTURED rather than
// written: a recorded sem is never fed back (it was never an answer), so the replayed code makes
// its claims afresh into hook.sems, and a reader compares the two accounts.

// Where a sem goes: the active call's buffer while recording (indicated, as everywhere in this
// runtime, by an active buffer rather than a mode flag), the replay capture during replay, and
// nowhere at all when neither is true — which is the ordinary case in production.
function semSink() {
  if (hook.mode === 'replay') return hook.sems ?? null;
  return active.getStore() ?? null;
}

function emitSem(name, phase, data, sid, outcome) {
  const sink = semSink();
  if (!sink) return null;
  if (sid == null) {
    sink.sid = (sink.sid ?? 0) + 1;
    sid = sink.sid;
  }
  const ev = { k: 'sem', name, phase, sid };
  if (outcome != null) ev.outcome = outcome;
  if (data && Object.keys(data).length) ev.data = toJsonable(data);
  // Through scrub, so `data` meets `redact`/`scrub` exactly as an effect's arguments do (record),
  // and a value the tape was forbidden to hold never reaches a printed report (replay).
  sink.push(scrub(ev));
  return sid;
}

/**
 * Mark that something meaningful just happened, at a point: `note('turn_skipped', { reason })`.
 *
 * A strict no-op when no recording is active for this call — not installed, or the gate said no.
 * That is the contract, not an optimisation: this sits in production code paths, so it must cost
 * nothing and have no failure modes when off.
 */
export function note(name, data = {}) {
  emitSem(name, 'point', data);
}

/**
 * Record that a stretch of execution constituted a domain act, and enclose the raw events it
 * produced. `fn` runs; every boundary event it emits is inside the span:
 *
 *     await fr.span('assign_turn', { chore }, async () => { ... });
 *     const x = fr.span('compute', () => heavy());   // data is optional
 *
 * `fn` may be sync or async; its result is returned. The `end` event carries `outcome: "error"`
 * when `fn` throws, and the error propagates untouched — a span that vanished from the tape when
 * the code inside it failed would hide exactly the execution somebody came to read.
 *
 * A no-op when recording is off, in which case this is just `fn()`.
 */
export function span(name, data, fn) {
  if (typeof data === 'function') {
    fn = data;
    data = {};
  }
  const sid = emitSem(name, 'begin', data);
  if (sid == null) return fn();

  const end = (outcome) => emitSem(name, 'end', null, sid, outcome);
  let out;
  try {
    out = fn();
  } catch (e) {
    end('error');
    throw e;
  }
  if (out !== null && typeof out === 'object' && typeof out.then === 'function') {
    return out.then(
      (r) => {
        end('ok');
        return r;
      },
      (e) => {
        end('error');
        throw e;
      },
    );
  }
  end('ok');
  return out;
}

// --- the clock and the RNG: the only truly global doors ------------------------------

function patch(target, key, replacement) {
  patches.push([target, key, target[key]]);
  target[key] = replacement;
}

// --- the clock ----------------------------------------------------------------------

/**
 * Shim BOTH ways of asking the wall clock: `Date.now()` and `new Date()`.
 *
 * Shimming only `Date.now()` would be a trap. Code that reaches for `new Date()` — which is
 * most code — would re-roll the clock on replay, silently, and the resulting divergence would
 * point at a value rather than at the door it came through.
 *
 * `new Date(...)` WITH arguments is deterministic and passes straight through: it is
 * arithmetic, not a question to the world.
 */
export function installClock() {
  const realNow = RealDate.now.bind(RealDate);

  const nowMs = () => {
    if (hook.mode === 'replay') return RealDate.parse(hook.feed.popExpect('now').v);
    const ms = realNow();
    emit({ k: 'now', v: new RealDate(ms).toISOString() });
    return ms;
  };

  class ShimDate extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(nowMs());
      else super(...args);
    }

    static now() {
      return nowMs();
    }

    /**
     * Without this, replacing the global Date breaks `instanceof` for every Date created by
     * code holding a reference to the original — including this library's own — so type checks
     * in the observed app would start failing.
     */
    static [Symbol.hasInstance](x) {
      return x instanceof RealDate;
    }
  }

  patch(globalThis, 'Date', ShimDate);

  // The monotonic clock is a DIFFERENT door, not the same one in other clothes: arbitrary
  // origin, no wall time. Handing back a wall time would be a category error, so it gets its
  // own event kind.
  const realPerfNow = performance.now.bind(performance);
  patch(performance, 'now', () => {
    if (hook.mode === 'replay') return hook.feed.popExpect('perf').v;
    const v = realPerfNow();
    emit({ k: 'perf', v });
    return v;
  });
}

// --- randomness ---------------------------------------------------------------------

/** Replay a recorded byte draw, checking the tape can still answer the question asked. */
function replayBytes(n) {
  const ev = hook.feed.popExpect('rand');
  const buf = Buffer.from(ev.hex ?? '', 'hex');
  // Under a MUTATED tape the recorded draw may no longer fit. Saying so plainly beats
  // handing back a buffer of the wrong length and letting the nonsense surface a thousand
  // lines later as something that looks like a bug in the app.
  if (buf.length !== n) {
    throw new ProbeUnanswerable(
      `the code asked for ${n} random bytes but the tape holds ${buf.length} ` +
        `(edit the rand event's n/hex to match)`,
    );
  }
  return buf;
}

/**
 * Shim every door randomness comes through in Node — not just the convenient one.
 *
 * Math.random, crypto.randomBytes (sync AND callback), randomUUID, randomInt,
 * randomFillSync/randomFill, and webcrypto's getRandomValues. Leaving any of them open would
 * mean an app that happens to use it re-rolls on replay and gets a divergence that explains
 * nothing.
 */
export function installRandom() {
  // Math.random
  const realMathRandom = Math.random.bind(Math);
  patch(Math, 'random', () => {
    if (hook.mode === 'replay') return hook.feed.popExpect('rand').v;
    const v = realMathRandom();
    emit({ k: 'rand', m: 'float', v });
    return v;
  });

  // crypto.randomBytes — both the sync and the callback forms
  const realBytes = crypto.randomBytes.bind(crypto);
  patch(crypto, 'randomBytes', (n, cb) => {
    if (!cb) {
      if (hook.mode === 'replay') return replayBytes(n);
      const buf = realBytes(n);
      emit({ k: 'rand', m: 'bytes', n, hex: buf.toString('hex') });
      return buf;
    }

    // The callback form is a door like any other: an app that reaches for it must be recorded
    // too, or it re-rolls on replay and the divergence points at a value rather than at the door.
    if (hook.mode === 'replay') {
      let buf;
      try {
        buf = replayBytes(n);
      } catch (e) {
        return process.nextTick(() => cb(e));
      }
      return process.nextTick(() => cb(null, buf));
    }
    return realBytes(n, (err, buf) => {
      if (!err) emit({ k: 'rand', m: 'bytes', n, hex: buf.toString('hex') });
      cb(err, buf);
    });
  });

  // crypto.randomUUID
  const realUuid = crypto.randomUUID.bind(crypto);
  patch(crypto, 'randomUUID', (opts) => {
    if (hook.mode === 'replay') {
      const h = hook.feed.popExpect('rand').hex;
      return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20, 32)].join('-');
    }
    const uuid = realUuid(opts);
    emit({ k: 'rand', m: 'bytes', n: 16, hex: uuid.replace(/-/g, '') });
    return uuid;
  });

  // crypto.randomInt — a scalar draw, so it records the value, not bytes
  const realInt = crypto.randomInt.bind(crypto);
  patch(crypto, 'randomInt', (...args) => {
    const cb = typeof args.at(-1) === 'function' ? args.pop() : null;

    const draw = () => {
      if (hook.mode === 'replay') return hook.feed.popExpect('rand').v;
      const v = realInt(...args);
      emit({ k: 'rand', m: 'int', v });
      return v;
    };

    if (!cb) return draw();
    try {
      const v = draw();
      return process.nextTick(() => cb(null, v));
    } catch (e) {
      return process.nextTick(() => cb(e));
    }
  });

  // crypto.randomFillSync — fills a buffer in place; same shape as randomBytes
  const realFillSync = crypto.randomFillSync.bind(crypto);
  patch(crypto, 'randomFillSync', (buf, offset = 0, size = buf.byteLength - offset) => {
    if (hook.mode === 'replay') {
      const bytes = replayBytes(size);
      Buffer.from(buf.buffer, buf.byteOffset + offset, size).set(bytes);
      return buf;
    }
    realFillSync(buf, offset, size);
    const hex = Buffer.from(buf.buffer, buf.byteOffset + offset, size).toString('hex');
    emit({ k: 'rand', m: 'bytes', n: size, hex });
    return buf;
  });

  // webcrypto getRandomValues — what portable code reaches for
  if (globalThis.crypto?.getRandomValues) {
    const realGRV = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    patch(globalThis.crypto, 'getRandomValues', (arr) => {
      const size = arr.byteLength;
      if (hook.mode === 'replay') {
        const bytes = replayBytes(size);
        new Uint8Array(arr.buffer, arr.byteOffset, size).set(bytes);
        return arr;
      }
      realGRV(arr);
      const hex = Buffer.from(arr.buffer, arr.byteOffset, size).toString('hex');
      emit({ k: 'rand', m: 'bytes', n: size, hex });
      return arr;
    });
  }
}

// --- install ------------------------------------------------------------------------

/**
 * Declare the boundary. The one app-specific artifact.
 *
 * @param {object} o
 * @param {object} [o.constants]  "module.NAME" → value, snapshotted into the header
 * @param {object} [o.redact]     field name → null (mask) or an idempotent transform
 * @param {Array<string|RegExp>} [o.forbid]  patterns that must never appear on the tape
 * @param {boolean} [o.clock]     record Date.now()
 * @param {boolean} [o.random]    record crypto.randomBytes() / randomUUID()
 */
export function boundaryOf(o = {}) {
  return {
    constants: o.constants ?? {},
    redact: o.redact ?? {},
    // Applied to EVERY string in a payload, wherever it sits — positional args, keys, prose.
    // Field rules cannot see those, and a redacted input would otherwise poison every value
    // derived from it. Must be idempotent.
    scrub: o.scrub ?? null,
    // The tripwire that backstops both of the above — see compileForbid. Compiled here, at the
    // one place a boundary is declared, so a pattern that does not parse is a loud failure in
    // the line that wrote it rather than a surprise at the first recorded call.
    forbid: compileForbid(o.forbid),
    clock: o.clock ?? true,
    random: o.random ?? true,
    // type name -> (args) => Error. Replay must rebuild a recorded error with its real
    // TYPE, because the code very likely branches on it (catch + instanceof); a generic
    // Error would take a different branch and quietly stop being the execution on the tape.
    errorRevivers: o.errorRevivers ?? {},
  };
}

/**
 * Turn recording on.
 *
 * `gate` (fn, args) => bool decides per call, so production can record 1-in-N or only the
 * calls that matter. A gate that never says yes must leave no tape behind at all, which is
 * why the file is opened by the first admitted call rather than here.
 */
export function install(
  b,
  {
    directory = '.flight',
    enabled = true,
    gate: g = null,
    sink = null,
    /**
     * The host's "keep me alive until this settles" hook — Vercel's and Cloudflare's
     * `waitUntil`. Given one, publishing leaves the critical path: the response goes out
     * immediately and the tape still lands. Without one, the publish is awaited, because on a
     * host that freezes the instance at response time a fire-and-forget publish is lost.
     */
    defer = null,
    /** A sink that hangs must never hold a request open. */
    sinkTimeoutMs = 3000,
  } = {},
) {
  if (!enabled) return null;

  boundary = b;
  gate = g;
  deferrer = typeof defer === 'function' ? defer : null;
  // `directory: null` records to memory alone — which is what a serverless host wants, since
  // its filesystem dies with the invocation. There, the sink IS the tape.
  recorder = new Recorder(directory, b, sink, sinkTimeoutMs);

  if (b.clock) installClock();
  if (b.random) installRandom();

  return recorder.path ?? recorder.name;
}

export function uninstall() {
  restoreTo(0);
  recorder = null;
  boundary = null;
  gate = null;
  deferrer = null;
}

/**
 * Where the patch stack currently is, and how to unwind back to it.
 *
 * replayCall() shims the clock and the RNG for the duration of a replay, and must unwind exactly
 * what it added. Calling uninstall() instead would also tear down the RECORDER — so a replay would
 * silently switch off recording for the rest of the process, and every call after it would go
 * unrecorded without a word. Mark, then unwind to the mark: an active recording session is left
 * alone.
 */
export function patchMark() {
  return patches.length;
}

export function restoreTo(mark) {
  while (patches.length > mark) {
    const [target, key, original] = patches.pop();
    target[key] = original;
  }
}

/** The tape being written, or null. */
export function tapePath() {
  return recorder?.path ?? null;
}
