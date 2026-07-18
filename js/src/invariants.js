// Invariants — the "right?" question, alongside replay's "same?".
//
// A recording asserts "same as before". It cannot condemn the first observation of a bug: a bug
// replays bit-for-bit forever, faithfully, and the tape is just as pleased with it as with a fix.
// Only a spec can call an execution wrong — so an invariant is a claim about EVERY execution,
// written once and checked against any recording.
//
// An invariant CONSUMES the tape, and the tape is shared, so this is not language-bound the way
// record and replay are: a recording made by any implementation can be judged by these. But a
// Node consumer wants to write the claim in JavaScript, over the replayed execution of its own
// code, so here it is.
//
// Under a MUTATED (probe) recording this becomes a property test over the boundary: the tape
// drives the real code into a world that never happened, and the invariant judges what it did
// there. That is why a probe's verdict rests on the invariants alone — see `ok` below.

import { loadTape, pickCall, replayCall } from './replay.js';
import { fromJsonable } from './serial.js';

/**
 * What the replayed code did — the surface an invariant asserts over.
 *
 * `result` is what the REPLAYED code produced, not what was recorded: the recorded result is the
 * thing being questioned, so asserting over it would only ever confirm the tape. It is `null`
 * when the call raised — a tool that legitimately throws hands a result-reading invariant a
 * `null`, so guard those with `t.error`.
 */
export class Trajectory {
  constructor({ result = null, error = null, kwargs = {}, events = [], sems = [], trace = null, writes = [] }) {
    this.result = result;
    this.error = error;
    this.kwargs = kwargs;
    /** What the replayed code WOULD have written — writes are compared, never executed. */
    this.writes = writes;
    /** The recorded boundary events — what the world answered, in order. */
    this.events = events;
    /** The claims the replayed code made, in order: `[name, phase]` pairs. */
    this.sems = sems;
    /** The replayed execution's internals, when the check was run with `trace`. */
    this.trace = trace;
  }
}

/** A named claim, asserted by throwing when it does not hold. */
export function invariant(name, assert) {
  return { name, assert };
}

/**
 * Replay one call and judge the trajectory.
 *
 * @param {object} o
 * @param {string|object} o.tape       a tape path, tape text, or a loaded tape
 * @param {Function} o.fn              the real code to re-run
 * @param {object[]} o.invariants      the claims to check
 * @param {number} [o.index]           which call, by position
 * @param {number} [o.seq]             which call, by seq
 * @param {string} [o.fnName]          which call, by tool name
 * @param {object} [o.call]            a call object directly — the mutated-tape flow
 * @param {object} [o.boundary]        revivers/redaction, as replay needs them
 * @param {boolean} [o.probe]          the tape was edited; judge on the invariants alone
 * @param {string[]} [o.trace]         file patterns to trace, enabling `t.trace`
 */
export async function checkInvariants({
  tape,
  fn,
  invariants = [],
  index = null,
  seq = null,
  fnName = null,
  call = null,
  boundary = {},
  probe = false,
  trace = null,
}) {
  let target = call;
  if (!target) {
    const loaded = typeof tape === 'string' ? loadTape(tape) : tape;
    target =
      index != null ? loaded.calls[index] : pickCall(loaded, { seq, fn: fnName ?? undefined });
    if (!target) throw new Error(`no call at index ${index}`);
  }

  const report = await replayCall({
    call: target,
    fn,
    boundary,
    // A call marked `probe` on the tape is a probe whether or not the caller said so — the
    // mutation flow marks the tape, and forgetting the flag here would compare arguments
    // against a world that was deliberately edited.
    probe: probe || Boolean(target.probe),
    trace,
  });

  const traj = new Trajectory({
    result: report.error != null ? null : (report.result ?? null),
    error: report.error ?? null,
    kwargs: fromJsonable(target.kwargs ?? {}),
    events: target.events ?? [],
    sems: report.semsReplayed ?? [],
    trace: report.trace ?? null,
    writes: report.writes ?? [],
  });

  const violations = [];
  let held = 0;
  for (const inv of invariants) {
    try {
      // An invariant may be async: a claim that awaits something is still a claim, and
      // silently not awaiting it would pass every time.
      await inv.assert(traj);
      held += 1;
    } catch (e) {
      violations.push({ invariant: inv.name, message: e instanceof Error ? e.message : String(e) });
    }
  }

  return {
    fn: target.fn ?? '',
    // A probe's replay result is NOT expected to match — the world was edited, so a different
    // answer is the point. Its verdict rests on the invariants plus whether the tape could
    // answer the path at all. A strict (non-probe) check also requires the replay to reproduce.
    ok: violations.length === 0 && (probe || target.probe ? report.divergence == null : report.ok),
    held,
    violations,
    replay: report,
    trajectory: traj,
  };
}

/** A readable verdict, for a human or a failing test's message. */
export function formatReport(report) {
  if (report.replay.divergence) {
    return `${report.fn}: could not check — ${report.replay.divergence.message}`;
  }
  if (report.ok) return `${report.fn}: ${report.held} invariant(s) held`;
  const lines = [`${report.fn}: ${report.violations.length} violation(s)`];
  for (const v of report.violations) lines.push(`  - ${v.invariant}: ${v.message}`);
  return lines.join('\n');
}
