// Replay — resurrection, not re-enactment.
//
// The recorded answers are fed back and the REAL code re-runs the original execution: no
// network, no database, no waiting for the bug to happen again. Nothing is mocked; the same
// wrapped client and the same clock/RNG shims the recording used simply source their
// answers from the tape instead of the world (see `hook` in record.js).
//
// Two things it must do, and they are equally important:
//
//   1. Answer. Pop the recorded answers in order and hand them back.
//   2. Refuse to answer the wrong question. Every event is checked against what the code
//      is actually asking. If the code asks something else — a different effect, in a
//      different order, with different arguments — that IS the finding: it is the exact
//      point where behaviour changed. A replay that silently answered anyway would be
//      worse than useless, because it would look like it worked.

import fs from 'node:fs';

import { hook, active, installClock, installRandom, patchMark, restoreTo } from './record.js';
import { toJsonable, fromJsonable, redactJsonable } from './serial.js';
import { ReplayDivergence, ProbeUnanswerable } from './errors.js';

/** Read a tape. Tolerates a torn final line, which is the only corruption possible. */
export function loadTape(pathOrText) {
  const text = fs.existsSync(pathOrText) ? fs.readFileSync(pathOrText, 'utf8') : pathOrText;

  const objs = [];
  const lines = text.split('\n').filter((l) => l.trim());
  lines.forEach((ln, i) => {
    try {
      objs.push(JSON.parse(ln));
    } catch (e) {
      if (i !== lines.length - 1) throw e; // only the last line may be torn
    }
  });

  const header = objs.find((o) => o.ev === 'session');
  if (!header) throw new Error('tape has no session header');
  if (header.version !== 1) throw new Error(`unsupported tape version ${header.version}`);

  return { header, calls: objs.filter((o) => o.ev === 'call') };
}

/** Pick one call: by `seq`, or the first matching `fn`, or the only one there is. */
export function pickCall(tape, { seq, fn } = {}) {
  const { calls } = tape;
  if (seq != null) {
    const c = calls.find((x) => x.seq === seq);
    if (!c) throw new Error(`no call with seq=${seq}`);
    return c;
  }
  if (fn) {
    const c = calls.find((x) => x.fn === fn);
    if (!c) throw new Error(`no call to ${fn}`);
    return c;
  }
  if (calls.length !== 1) throw new Error(`tape has ${calls.length} calls — pass seq or fn`);
  return calls[0];
}

class Feed {
  /**
   * @param {object[]} events  the recorded answers, in the order the world gave them
   * @param {object} o
   * @param {boolean} [o.probe]   the tape was edited; do not compare arguments
   * @param {object} [o.revivers] error type name → (args) => Error
   * @param {object} [o.redact]   the boundary's rules, so a redacted tape still compares
   */
  constructor(events, { probe = false, revivers = {}, redact = {}, scrub = null } = {}) {
    this.events = events;
    this.i = 0;
    this.probe = probe;
    this.revivers = revivers;
    this.redact = redact;
    this.scrub = scrub;
  }

  get exhausted() {
    return this.i >= this.events.length;
  }

  /** What the code asked next, when the tape had nothing (or something else) to say. */
  _diverge(want, got) {
    const at = `at event ${this.i} of ${this.events.length}`;
    return new ReplayDivergence(
      `the code asked a different question than the recording holds, ${at}\n` +
        `  recorded: ${got}\n` +
        `  replayed: ${want}`,
    );
  }

  popExpect(kind, { fn } = {}) {
    if (this.exhausted) {
      throw this._diverge(
        `${kind}${fn ? ` ${fn}` : ''}`,
        '(nothing — the recording had no more answers to give)',
      );
    }
    const ev = this.events[this.i];

    if (ev.k !== kind || (fn != null && ev.fn !== fn)) {
      throw this._diverge(
        `${kind}${fn ? ` ${fn}` : ''}`,
        `${ev.k}${ev.fn ? ` ${ev.fn}` : ''}`,
      );
    }

    this.i += 1;
    return ev;
  }

  /** Answer an effect from the tape, having first checked it is the effect being asked. */
  answerEffect(fn, args) {
    const ev = this.popExpect('fx', { fn });

    // The arguments are part of the question. A recording answers the question it was
    // asked, and if the code now asks with different arguments it is a different execution.
    //
    // Except under probe: a mutated upstream answer legitimately changes every downstream
    // question, so comparing arguments there would flag the mutation itself as a
    // divergence. The event's name and order still gate.
    if (!this.probe) {
      // Scrubbed exactly as the recording was — which is why redaction transforms must be
      // idempotent: a value that came off the tape is already a mask and must scrub to
      // itself, or a redacted recording could never be replayed.
      const recorded = JSON.stringify(ev.args ?? []);
      const replayed = JSON.stringify(redactJsonable(args, this.redact, this.scrub));
      if (recorded !== replayed) {
        throw this._diverge(`fx ${fn}(${replayed})`, `fx ${fn}(${recorded})`);
      }
    }

    if ('err' in ev) throw this._reviveError(ev.err);
    return fromJsonable(ev.res);
  }

  /**
   * Rebuild the recorded error. Its TYPE is what matters: the code very likely catches it
   * (`catch (e) { if (e instanceof ToyError) ... }`), and a replay that threw a generic
   * Error there would take a different branch and quietly stop being the execution it is
   * meant to be reproducing. Hence the boundary declares its revivers.
   */
  _reviveError(err) {
    const revive = this.revivers[err.type];
    if (revive) {
      try {
        return revive(fromJsonable(err.args) ?? []);
      } catch {
        /* fall through to the generic error below */
      }
    }
    const e = new Error(err.repr ?? err.type);
    e.name = err.type;
    return e;
  }
}

/**
 * Re-run one recorded call against the real code.
 *
 * @param {object} o
 * @param {object} o.call       the recorded call line
 * @param {Function} o.fn       the REAL (unwrapped) tool function
 * @param {object} [o.boundary] the boundary — for its redact rules and error revivers
 * @param {boolean} [o.probe]   the tape was mutated: do not compare arguments
 * @returns {Promise<object>} a report
 */
export async function replayCall({ call, fn, boundary = {}, probe = false }) {
  const feed = new Feed(call.events ?? [], {
    probe: probe || Boolean(call.probe),
    revivers: boundary.errorRevivers ?? {},
    redact: boundary.redact ?? {},
    scrub: boundary.scrub ?? null,
  });

  const redact = boundary.redact ?? {};
  const kwargs = fromJsonable(call.kwargs ?? {});

  // The clock and the RNG must be shimmed for replay too, or the code re-rolls the dice and
  // is no longer the execution on the tape. Mark the patch stack so we unwind exactly what we
  // add — a replay must not tear down a recording session that was already running.
  const mark = patchMark();
  installClock();
  installRandom();

  hook.mode = 'replay';
  hook.feed = feed;

  let result;
  let error = null;
  let divergence = null;

  try {
    // The event buffer is what tells the wrapped client it is inside a call at all.
    result = await active.run([], () => fn(kwargs));
  } catch (e) {
    if (e instanceof ReplayDivergence || e instanceof ProbeUnanswerable) {
      divergence = e;
    } else {
      error = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
    }
  } finally {
    hook.mode = null;
    hook.feed = null;
    restoreTo(mark);
  }

  if (divergence) {
    return { ok: false, divergence, resultMatch: false, errorMatch: false, unconsumed: feed.events.length - feed.i };
  }

  // The code asked FEWER questions than the recording holds answers for. That is a
  // divergence too, and a sneaky one: everything "passed", the code just quietly stopped
  // doing some of its work.
  const unconsumed = feed.events.length - feed.i;
  if (unconsumed > 0) {
    const next = feed.events[feed.i];
    divergence = new ReplayDivergence(
      `the code stopped asking ${unconsumed} question(s) the recording answered — ` +
        `next unconsumed: ${next.k}${next.fn ? ` ${next.fn}` : ''}`,
    );
    return { ok: false, divergence, resultMatch: false, errorMatch: false, unconsumed };
  }

  // Mirror the recorder exactly: a call that raised has no return value (null), which is not
  // the same as one that returned `undefined`.
  const replayedResult = error !== null ? null : redactJsonable(toJsonable(result), redact, boundary.scrub ?? null);
  const resultMatch = JSON.stringify(replayedResult) === JSON.stringify(call.result ?? null);
  const errorMatch = (error ?? null) === (call.error ?? null);

  return {
    ok: resultMatch && errorMatch,
    result,
    error,
    recordedResult: call.result,
    replayedResult,
    resultMatch,
    errorMatch,
    divergence: null,
    unconsumed: 0,
  };
}

export { Feed, ReplayDivergence, ProbeUnanswerable };
