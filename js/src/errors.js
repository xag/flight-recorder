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

/**
 * A `forbid` pattern matched the record the recorder was about to write.
 *
 * Raised at record time, before any bytes reach the file, the in-memory mirror or the sink — so
 * the credential does not land, anywhere, ever. This is the one failure in the recorder that is
 * deliberately NOT best-effort. Everywhere else the direction is "the recording is a bit poorer,
 * the app survives", because a recorder must not break the app it observes. Here it inverts: a
 * tape being written with a live credential on it is not a poorer recording, it is an
 * exfiltration path, and the app is already in the state you swore it would never be in.
 * Failing the call is the quiet option.
 *
 * The message names the RULE and never the match. It ends up in logs and stack traces, and a
 * tripwire that quotes the secret it caught has become the leak it was there to prevent.
 */
export class ForbiddenValue extends Error {
  constructor(message) {
    super(message);
    this.name = 'ForbiddenValue';
  }
}
