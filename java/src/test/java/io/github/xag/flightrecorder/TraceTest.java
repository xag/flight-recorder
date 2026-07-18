package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Variable-level tracing, end to end: rewrite the source, compile it to memory, run it, and read
 * what it believed while it ran.
 */
class TraceTest {

    private static final String TOY = "src/test/resources/traced/TracedToy.java";

    @Test
    void theInstrumentedCopyStillCompilesAndStillComputes() throws Exception {
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent",
                (Object) new int[]{1, 1, -1, 0});
        // 2 correct out of 3 answered — the buggy denominator, faithfully reproduced. Tracing must
        // not change the answer, including when the answer is wrong.
        assertEquals(66, run.result());
        assertFalse(run.trace().isEmpty(), "the run must have been observed");
    }

    @Test
    void aVariableTimelineIsALookupNotAnInference() throws Exception {
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent",
                (Object) new int[]{1, 1, -1, 0});
        Trace t = run.trace();

        List<Trace.Obs> answered = t.values("answered");
        assertFalse(answered.isEmpty(), () -> "expected to observe `answered`; saw " + t.names());

        // Values are DATA, not reprs — this is the whole point of trace version 2, and it is what
        // lets a claim do arithmetic instead of string matching.
        assertEquals(3L, t.last("answered").value());
        assertEquals(2L, t.last("correct").value());
        assertEquals(66L, t.last("pct").value());
    }

    @Test
    void theArgumentsAVariableArrivedWithAreRecorded() throws Exception {
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent",
                (Object) new int[]{1, 1, -1, 0});
        List<Trace.Call> calls = run.trace().calls("gradePercent");
        assertEquals(1, calls.size());
        assertEquals(List.of(1L, 1L, -1L, 0L), calls.get(0).args().get("answers"));
    }

    @Test
    void aReturnIsObserved() throws Exception {
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent",
                (Object) new int[]{1, 1, -1, 0});
        List<Trace.Return> returns = run.trace().returns("gradePercent");
        assertEquals(1, returns.size());
        assertEquals(66L, returns.get(0).value());
    }

    /**
     * The reason the feature exists.
     *
     * <p>The returned percentage is self-consistent: 2 correct out of 3 answered really is 66%. No
     * claim about the RESULT can catch this. The claim that catches it is about an internal
     * variable — and it is only writable because the variable is observable.
     */
    @Test
    void theBugIsCondemnedByItsOwnTrace() throws Exception {
        int[] answers = {1, 1, -1, 0};
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent", (Object) answers);

        // The output passes every sane check you could write about it.
        int pct = (int) run.result();
        assertTrue(pct >= 0 && pct <= 100, "the result is perfectly plausible");

        // The trace is not so forgiving: the denominator never reached the number of questions.
        long answered = (long) run.trace().last("answered").value();
        assertNotEquals(answers.length, answered,
                "this is the defect: the grade was computed over answered questions, not asked ones");
        assertEquals(3L, answered);
    }

    @Test
    void anExceptionIsObservedWithTheStateUpToTheThrow() throws Exception {
        Tracer.Run[] holder = new Tracer.Run[1];
        assertThrows(ArithmeticException.class, () ->
                holder[0] = Tracer.run(List.of(TOY), "TracedToy", "divide", 7, 0));

        // The throw propagates untouched — but the tracer still saw the frame it left.
        // (The run itself threw, so the trace is read from the sink via a second, surviving run.)
        Tracer.Run ok = Tracer.run(List.of(TOY), "TracedToy", "divide", 7, 2);
        assertEquals(35, ok.result());
        assertEquals(70L, ok.trace().last("scaled").value());
    }

    @Test
    void anUntracedProcessAnswersNeverObservedRatherThanPassingVacuously() {
        Trace empty = Trace.empty();
        assertTrue(empty.isEmpty());
        assertEquals(List.of(), empty.values("answered"));
        assertNull(empty.last("answered"));
        // This is why the trace is empty-never-null: a claim about an untraced variable must FAIL,
        // not quietly succeed because there was nothing to contradict it.
        assertEquals("answered: never observed", empty.render("answered"));
    }

    @Test
    void aVersionOneTraceIsRefusedRatherThanHalfUnderstood() {
        String v1 = "{\"e\":\"H\",\"trace_version\":1}\n{\"e\":\"L\",\"fn\":\"f\",\"at\":\"a.java:1\",\"d\":{\"x\":\"42\"}}\n";
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class, () -> Trace.parse(v1));
        assertTrue(e.getMessage().contains("older tracer"), e.getMessage());
    }

    @Test
    void theTraceRoundTripsThroughItsArtifactForm() throws Exception {
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent",
                (Object) new int[]{1, 1, -1, 0});
        Trace reparsed = Trace.parse(run.trace().toJsonl());
        assertEquals(run.trace().size(), reparsed.size());
        assertEquals(run.trace().last("pct").value(), reparsed.last("pct").value());
    }

    @Test
    void locationsPointAtTheOriginalFileNotTheInstrumentedOne() throws Exception {
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "gradePercent",
                (Object) new int[]{1, 1, -1, 0});
        String original = java.nio.file.Files.readString(java.nio.file.Paths.get(TOY));
        int lines = original.split("\n", -1).length;

        for (Trace.Obs o : run.trace().values("answered")) {
            assertTrue(o.at().startsWith("TracedToy.java:"), o.at());
            int line = Integer.parseInt(o.at().substring("TracedToy.java:".length()));
            // Instrumenting moves every line. A location past the end of the real file would mean
            // the trace is pointing at a file that exists nowhere on the reader's disk.
            assertTrue(line <= lines, "line " + line + " is past the end of the original " + lines + "-line file");
        }
    }
}
