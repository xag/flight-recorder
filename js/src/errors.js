/**
 * The code asked the world a different question than the tape holds an answer to.
 *
 * Not an error in the ordinary sense: it is the precise point at which the code's behaviour
 * changed. A tape is a complete record of one execution, so a replay that diverges from it has,
 * by definition, stopped being that execution — and this says exactly where.
 */
export class ReplayDivergence extends Error {
  constructor(message) {
    super(message);
    this.name = 'ReplayDivergence';
  }
}

/**
 * A mutated tape cannot answer the question the code now asks.
 *
 * Distinct from a divergence: nothing is wrong with the code. You edited the tape to visit
 * a world that never happened, and the edit is incomplete — the recorded answer no longer
 * fits the question. The fix is to the tape, not to the program.
 */
export class ProbeUnanswerable extends Error {
  constructor(message) {
    super(message);
    this.name = 'ProbeUnanswerable';
  }
}
