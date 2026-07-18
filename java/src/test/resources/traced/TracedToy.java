/**
 * The subject of the tracing tests. This file is NOT compiled by the build — it is read as text,
 * rewritten, and compiled to memory by {@link io.github.xag.flightrecorder.Tracer}.
 *
 * <p>It holds a deliberate bug of the only kind variable-level tracing exists for: one whose
 * output is entirely self-consistent. Nothing about the returned number looks wrong, no assertion
 * over the result catches it, and the mistake is visible only in a local nobody returns.
 */
public class TracedToy {

    /**
     * Grades a quiz, as a percentage.
     *
     * <p>A negative answer means the question was left unanswered. The percentage is computed over
     * the questions that were ANSWERED rather than over the questions that were ASKED — so a
     * candidate who skips everything they do not know scores as if those questions did not exist.
     * The number that comes back is always plausible and always internally consistent.
     */
    public static int gradePercent(int[] answers) {
        int answered = 0;
        int correct = 0;
        for (int i = 0; i < answers.length; i++) {
            int a = answers[i];
            if (a < 0) {
                continue;
            }
            answered = answered + 1;
            if (a == 1) {
                correct = correct + 1;
            }
        }
        int pct = correct * 100 / answered;
        return pct;
    }

    /** A function that throws, so the trace can be checked to carry the state up to the throw. */
    public static int divide(int a, int b) {
        int scaled = a * 10;
        int out = scaled / b;
        return out;
    }
}
