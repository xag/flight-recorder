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
import { traced, Trace } from './trace.js';

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

  /**
   * Step over recorded `sem` events. They are never answers.
   *
   * A sem is the app's testimony about what it was doing, not something the world told it, so
   * there is nothing here to feed back: the replayed code re-runs its own note()/span() calls
   * and testifies afresh. Stepping over them advances `i`, which counts them as consumed — so
   * "every event consumed" keeps meaning "the code asked the recording everything it holds", and
   * instrumenting an app never costs a false replay failure.
   */
  skipSems() {
    while (this.i < this.events.length && this.events[this.i].k === 'sem') this.i += 1;
  }

  popExpect(kind, { fn } = {}) {
    this.skipSems();
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
    const args = fromJsonable(err.args) ?? [];

    const revive = this.revivers[err.type];
    if (revive) {
      try {
        return revive(args);
      } catch {
        /* fall through to the generic error below */
      }
    }

    // The MESSAGE, not the repr. `repr` is the recorded stack trace, and an error rebuilt with a
    // stack for a message is a different error: code that reads `e.message` — and most code does,
    // to log it or to return it — gets 300 characters of `at ClientRequest.<anonymous>` where the
    // recording had a sentence, and diverges over the instrument rather than over itself.
    //
    // `args[0]` is the message (see errEvent). `repr` remains the fallback for a tape written
    // before that was true, and becomes the stack, which is what it always was.
    const e = new Error(args[0] ?? err.repr ?? err.type);
    e.name = err.type;
    if (err.repr) e.stack = err.repr;
    return e;
  }
}

/**
 * The semantic trace: what was claimed, and in what order. Names and phases only — payloads are
 * a reader's business, and comparing them would make an added field to a span's data look like a
 * change of meaning.
 */
function semPairs(events) {
  return (events ?? []).filter((e) => e.k === 'sem').map((e) => [e.name, e.phase]);
}

/** The first place two accounts of what the code was doing differ, or null if they agree. */
function semDivergence(recorded, replayed) {
  const show = (p) => (p == null ? 'nothing' : `${JSON.stringify(p[0])} ${p[1]}`);
  const n = Math.max(recorded.length, replayed.length);
  for (let k = 0; k < n; k += 1) {
    const a = recorded[k];
    const b = replayed[k];
    if (JSON.stringify(a) !== JSON.stringify(b)) {
      return (
        `semantic divergence at ${k}: recorded ${show(a)}, replayed ${show(b)} — ` +
        `the code's account of what it was doing has changed`
      );
    }
  }
  return null;
}

/**
 * Re-run one recorded call against the real code.
 *
 * @param {object} o
 * @param {object} o.call       the recorded call line
 * @param {Function} o.fn       the REAL (unwrapped) tool function
 * @param {object} [o.boundary] the boundary — for its redact rules and error revivers
 * @param {boolean} [o.probe]   the tape was mutated: do not compare arguments
 * @param {boolean} [o.semStrict] fold semantic divergence into `ok`: the replayed code must make
 *   the same claims, in the same order, as the recording holds. Off by default, so instrumenting
 *   an app cannot turn an existing pinned suite red.
 * @param {(string|RegExp)[]} [o.trace] files to observe from the inside — every local, on every
 *   executed line. This is the point of replaying at all: a recording tells you what the world
 *   answered, a trace tells you what the code then believed. Costs a pause per line, so name the
 *   code you are investigating, not the world.
 * @returns {Promise<object>} a report
 */
export async function replayCall({ call, fn, boundary = {}, probe = false, semStrict = false, trace = null }) {
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

  // Where the replayed code's own note()/span() calls land — a recorded sem is never fed back.
  const sems = [];

  hook.mode = 'replay';
  hook.feed = feed;
  hook.sems = sems;

  let result;
  let error = null;
  let divergence = null;

  let traceOf = new Trace([]);

  try {
    // The event buffer is what tells the wrapped client it is inside a call at all.
    const run = () => active.run([], () => fn(kwargs));

    if (trace) {
      const t = await traced(run, { include: trace });
      traceOf = t.trace;
      if (t.error) throw t.error;
      result = t.result;
    } else {
      result = await run();
    }
  } catch (e) {
    if (e instanceof ReplayDivergence || e instanceof ProbeUnanswerable) {
      divergence = e;
    } else {
      error = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
    }
  } finally {
    hook.mode = null;
    hook.feed = null;
    hook.sems = null;
    restoreTo(mark);
  }

  // The sems trailing the last boundary answer — an outermost span's `end`, most often — were
  // never reached by a popExpect, and leaving them unread would report a shorter path than the
  // recorded one on a tape where nothing of the sort happened.
  feed.skipSems();

  // A THIRD signal, deliberately not folded into the other two by default: a boundary divergence
  // says the recording is stale, a wrong result says the code is wrong, and this says the code's
  // own account of what it was doing has changed — a refactor, or a bug, and the tape does not
  // presume to know which. `semStrict` opts a suite in, once its vocabulary has settled.
  const semsRecorded = semPairs(call.events);
  const semsReplayed = semPairs(sems);
  const semDiv = semDivergence(semsRecorded, semsReplayed);
  const semFields = { semsRecorded, semsReplayed, semDivergence: semDiv, semStrict };

  if (divergence) {
    return { ok: false, divergence, trace: traceOf, resultMatch: false, errorMatch: false, unconsumed: feed.events.length - feed.i, ...semFields };
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
    return { ok: false, divergence, trace: traceOf, resultMatch: false, errorMatch: false, unconsumed, ...semFields };
  }

  // Mirror the recorder exactly: a call that raised has no return value (null), which is not
  // the same as one that returned `undefined`.
  const replayedResult = error !== null ? null : redactJsonable(toJsonable(result), redact, boundary.scrub ?? null);
  const resultMatch = JSON.stringify(replayedResult) === JSON.stringify(call.result ?? null);
  const errorMatch = (error ?? null) === (call.error ?? null);

  return {
    ok: resultMatch && errorMatch && (!semStrict || semDiv === null),
    result,
    error,
    trace: traceOf,
    recordedResult: call.result,
    replayedResult,
    resultMatch,
    errorMatch,
    divergence: null,
    unconsumed: 0,
    ...semFields,
  };
}

export { Feed, ReplayDivergence, ProbeUnanswerable };
