package io.github.xag.flightrecorder;

import java.util.List;

/**
 * The recorder's exception taxonomy.
 *
 * <p>These four are kept distinct on purpose, because they answer four different questions and a
 * caller who conflates them draws the wrong conclusion:
 *
 * <ul>
 *   <li>{@link ForbiddenValue} — the recorder was about to write a secret. Nothing was written.
 *       This is the one recorder failure that is never swallowed.
 *   <li>{@link ReplayDivergence} — the code asked the world a different question than it asked
 *       when recorded. <b>The code changed.</b>
 *   <li>{@link ProbeUnanswerable} — a mutation sent execution down a path this recording cannot
 *       answer. <b>Nothing is wrong with the code</b>; the tape is incompletely edited.
 *   <li>{@link ReplayedEffectError} — the stand-in thrown during replay for a recorded error whose
 *       real type the boundary did not declare a reviver for.
 * </ul>
 */
public final class Errors {

    private Errors() {}

    /**
     * Thrown when a {@code forbid} pattern matches the record the recorder was about to write.
     *
     * <p>It names the RULE, never the match. An error message carrying the secret would defeat the
     * guard's whole purpose — it would move the credential from a tape nobody reads into a log
     * everybody does.
     */
    public static final class ForbiddenValue extends RuntimeException {
        public final String pattern;
        public final String what;

        public ForbiddenValue(String pattern, String what) {
            super(what + " matches a forbidden pattern (\"" + pattern + "\") after redaction — "
                    + "nothing was written; name the field in Boundary.redacting(), or widen a rule "
                    + "that stopped matching, and record again");
            this.pattern = pattern;
            this.what = what;
        }
    }

    /** The code asked a different question than the tape records. The recording is stale, or the
     *  behaviour changed — either way this is a finding about the CODE. */
    public static final class ReplayDivergence extends RuntimeException {
        public ReplayDivergence(String message) { super(message); }
    }

    /**
     * The mutation sent execution somewhere the recording has no answer for.
     *
     * <p>Deliberately NOT a divergence: a divergence says the code changed, and here the code is
     * fine and the tape is simply not edited far enough. Reporting these as the same thing is how
     * a probe session turns into a wild goose chase.
     */
    public static final class ProbeUnanswerable extends RuntimeException {
        public ProbeUnanswerable(String message) { super(message); }
    }

    /**
     * The generic stand-in for a recorded error during replay.
     *
     * <p>Code very often branches on an exception's type ({@code catch (ToyException e)}), so
     * replay rebuilds the recorded error with its real type when
     * {@link Boundary#reviving} declares one. This is what it throws when nothing was declared —
     * faithful about the type it could not rebuild, rather than silently substituting one.
     */
    public static final class ReplayedEffectError extends RuntimeException {
        public final String type;
        public final String repr;
        public final List<Object> args;

        public ReplayedEffectError(String type, String repr, List<Object> args) {
            super(type + ": " + repr);
            this.type = type;
            this.repr = repr;
            this.args = args == null ? List.of() : args;
        }
    }
}
