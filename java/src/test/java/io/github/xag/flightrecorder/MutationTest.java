package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Mutation and probe: replay the real code against a world that never happened, and judge what
 * comes out with claims rather than with a diff.
 */
class MutationTest {

    private static Recording record(Path dir) throws Exception {
        try (Recorder rec = Recorder.open(dir.toString(), Toy.plainBoundary())) {
            rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
            return Recording.load(rec.path());
        }
    }

    @Test
    void aMutatedAnswerFlowsThroughTheRealCode(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Mutate.on(tape.call(0)).effect("store.get").setResult(Map.of("name", "Zara", "x", 3L));

        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);

        // The mutation reached the result, which means the REAL code ran against the edited world —
        // not a simulation of it.
        assertEquals("Zara", Json.asMap(r.replayedResult).get("name"));
    }

    @Test
    void everyEditMarksTheCallAProbeAndThatSurvivesASave(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        assertFalse(tape.call(0).isProbe());

        Mutate.on(tape.call(0)).effect("store.get").setResult(Map.of("name", "Zara"));
        assertTrue(tape.call(0).isProbe());

        Path out = tmp.resolve("probe.jsonl");
        tape.save(out.toString());
        // Persisted deliberately: a saved mutated call must never later be mistaken for a strict
        // regression pin.
        assertTrue(Recording.load(out.toString()).call(0).isProbe());
    }

    @Test
    void probeReplayStopsComparingArgumentsButStillGatesOnOrder(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        // A mutated upstream answer legitimately changes every downstream question — here the write
        // signature carries the user's name, which the mutation changed.
        Mutate.on(tape.call(0)).effect("store.get").setResult(Map.of("name", "Zara", "x", 3L));

        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);
        assertTrue(r.probe, "an edit puts the call into probe mode by itself");
        assertNull(r.divergence, () -> "probe mode must not report changed arguments as divergence:\n"
                + Replay.format(0, r));
        assertTrue(r.ok(), () -> Replay.format(0, r));
    }

    @Test
    void aProbeIsJudgedByItsClaimsNotByAMatch(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Mutate.Handle probe = Mutate.on(tape.call(0));
        probe.effect("store.get").setResult(Map.of("name", "Zara", "x", 3L));

        List<Invariants.Invariant> claims = List.of(
                Invariants.of("the greeting names whoever the store returned", t -> {
                    Map<String, Object> result = Json.asMap(t.result);
                    assertEquals("Zara", result.get("name"),
                            "the greeting must name the row the store actually returned");
                }),
                Invariants.of("exactly one write is performed", t ->
                        assertEquals(1, t.writes.size())));

        Invariants.Report report = Invariants.checkCall(
                tape.call(0), Toy.resolver(), claims, Toy.plainBoundary(), true);

        assertTrue(report.ok(), report::toString);
        assertEquals(List.of(), report.violations());
    }

    @Test
    void aClaimThatFailsIsReportedWithoutTakingTheRunDown(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);

        List<Invariants.Invariant> claims = List.of(
                Invariants.of("a claim that is false", t -> fail("deliberately false")),
                Invariants.of("a claim that throws something odd", t -> {
                    throw new IllegalStateException("kaboom");
                }),
                Invariants.of("a claim that holds", t -> assertNotNull(t.result)));

        Invariants.Report report = Invariants.checkCall(
                tape.call(0), Toy.resolver(), claims, Toy.plainBoundary(), false);

        // One broken claim must not take down the ones written well, or nobody learns what they
        // said.
        assertEquals(2, report.violations().size());
        assertTrue(report.results.get(2).ok());
        assertFalse(report.ok());
    }

    @Test
    void anInvariantAboutAnUntracedVariableFailsRatherThanPassingVacuously(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Invariants.Report report = Invariants.checkCall(tape.call(0), Toy.resolver(),
                List.of(Invariants.of("level never exceeded 5", t ->
                        assertNotNull(t.trace.last("level"),
                                "`level` was never observed — this claim was not actually checked"))),
                Toy.plainBoundary(), false);

        assertFalse(report.ok(), "an unobservable claim must fail loudly, not pass silently");
        assertTrue(report.violations().get(0).error().contains("never observed"));
    }

    @Test
    void theClockCanBeRunBackwards(@TempDir Path tmp) throws Exception {
        try (Recorder rec = Recorder.open(tmp.toString(), Toy.semBoundary())) {
            Map<String, Object> kw = Recorder.kwargs("user", "alice", "password", "hunter2");
            rec.call("enrol", kw, () -> Toy.enrol(kw));
            Recording tape = Recording.load(rec.path());

            List<String> before = Mutate.on(tape.call(0)).clock().times();
            Mutate.on(tape.call(0)).clock().setTimes("1999-12-31T23:59:59");
            List<String> after = Mutate.on(tape.call(0)).clock().times();

            assertNotEquals(before, after);
            assertEquals("1999-12-31T23:59:59", after.get(0));
            assertTrue(tape.call(0).isProbe());
        }
    }

    @Test
    void aReadCanBeEmptied(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Mutate.on(tape.call(0)).read("stream").setEmpty();

        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);
        assertNull(r.divergence, () -> Replay.format(0, r));
    }

    @Test
    void anEffectCanBeMadeToFail(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Mutate.on(tape.call(0)).effect("store.get").setError("ToyError", "the store is down", 500L);

        // The real code now takes its error path, against a failure that never happened in
        // production and that you could not have provoked there.
        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);
        assertEquals("the store is down", r.replayedError);
    }

    @Test
    void aMutationOntoAnUnanswerablePathIsNotReportedAsADivergence(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Recording.CallView cv = tape.call(0);
        cv.markProbe();
        // Strip the events the code will ask for after the first, so the tape runs out mid-path.
        List<Object> events = Json.asList(cv.raw().get("events"));
        events.subList(1, events.size()).clear();

        Replay.Report r = Replay.replayCall(cv, Toy.resolver(), Toy.plainBoundary(), true);

        // This impeaches neither the code nor the recording, only their pairing — so it must not
        // be dressed up as "the code changed".
        assertNotNull(r.unanswerable, () -> Replay.format(0, r));
        assertNull(r.divergence);
        assertFalse(r.ok());
    }
}
