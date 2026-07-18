package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * The tripwire guards <b>every</b> artifact the recorder writes — not just the tape.
 *
 * <p>There are four write paths, and a guard on three of them is a guard on none: the one left
 * unwatched is where the secret goes.
 */
class ForbidTest {

    private static final String SECRET = "sk-live-0123456789abcdef";
    private static final String PATTERN = "sk-live-[A-Za-z0-9]+";

    // ------------------------------------------------- 1. the tape's own lines

    @Test
    void aForbiddenValueInACallRecordIsRefusedAndNothingIsWritten(@TempDir Path tmp) throws Exception {
        Boundary b = new Boundary().forbidden(PATTERN);
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            Errors.ForbiddenValue e = assertThrows(Errors.ForbiddenValue.class, () ->
                    rec.call("leak", Recorder.kwargs("token", SECRET), () -> "ok"));

            // The error names the RULE, never the match — an error carrying the secret would defeat
            // the guard's whole purpose.
            assertTrue(e.getMessage().contains(PATTERN));
            assertFalse(e.getMessage().contains(SECRET), "the error must not carry the secret");
        }
    }

    @Test
    void aForbiddenValueInTheHeaderLeavesNoSessionFileAtAll(@TempDir Path tmp) throws Exception {
        Boundary b = new Boundary().forbidden(PATTERN).constant("app.KEY", SECRET);
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            assertThrows(Errors.ForbiddenValue.class, () ->
                    rec.call("anything", Map.of(), () -> "ok"));
        }
        // A hit in the header means the file is never created, not created and then abandoned.
        try (var s = Files.list(tmp)) {
            assertEquals(0, s.filter(p -> p.toString().endsWith(".jsonl")).count());
        }
    }

    // ------------------------------------------- 2. events, before they buffer

    @Test
    void anInFlightEventIsGuardedBeforeItEntersTheBuffer(@TempDir Path tmp) throws Exception {
        Boundary b = new Boundary().forbidden(PATTERN);
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            assertThrows(Errors.ForbiddenValue.class, () ->
                    rec.call("fetch", Map.of(), () ->
                            Recorder.effect("http.get", List.of("https://api/" + SECRET), () -> "body")));
        }
    }

    // --------------------------------------------- 3. the RE-saved, edited tape

    @Test
    void aMutationThatPutsASecretBackIsRefusedOnSave(@TempDir Path tmp) throws Exception {
        // Recorded clean, with no secret anywhere.
        Path tape;
        try (Recorder rec = Recorder.open(tmp.toString(), new Boundary())) {
            rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
            tape = Path.of(rec.path());
        }

        // Mutation exists precisely to EDIT recorded values — so a tape that passed the tripwire
        // when it was recorded can have a credential put into it by hand.
        Recording loaded = Recording.load(tape.toString());
        Mutate.on(loaded.call(0)).effect("store.get").setResult(Map.of("token", SECRET));

        loaded.forbidding(PATTERN);
        Path out = tmp.resolve("edited.jsonl");
        Errors.ForbiddenValue e = assertThrows(Errors.ForbiddenValue.class,
                () -> loaded.save(out.toString()));
        assertTrue(e.getMessage().contains(PATTERN));

        // The whole tape is vetted in memory before the file is opened, so a refusal never
        // truncates a good tape to punish a bad edit.
        assertFalse(Files.exists(out), "a refusal must leave no half-written file behind");
    }

    @Test
    void aTapeDoesNotCarryItsOwnForbidPatterns(@TempDir Path tmp) throws Exception {
        try (Recorder rec = Recorder.open(tmp.toString(), new Boundary().forbidden(PATTERN))) {
            rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
            String text = Files.readString(Path.of(rec.path()));
            // Deliberate: the rules are the boundary's, not the artifact's. A later reader must not
            // be able to find them and "helpfully" relax them.
            assertFalse(text.contains(PATTERN), "the tape must not carry the rules that guarded it");
        }
    }

    // ------------------------------------------------------ 4. the trace sidecar

    @Test
    void theTraceIsGuardedToo() {
        // The trace is the WORST artifact to leave unguarded, not the least: it records every local
        // on every executed line — values BEFORE they reach any redaction — and tracing is exactly
        // what you switch on when debugging the request that went wrong.
        Boundary b = new Boundary().forbidden(PATTERN);
        TraceHook.Sink sink = new TraceHook.Sink(null, b);
        TraceHook.Sink prior = TraceHook.sink();
        TraceHook.setSink(sink);
        try {
            long frame = TraceHook.enter("f", "a.java:1", new String[]{"token"}, new Object[]{SECRET});
            assertNotNull(sink.refused(), "a traced secret must trip the guard");
            assertEquals(PATTERN, sink.refused());
            // The buffer is cleared, not merely the file: an invariant reads these events while the
            // run is going, so "in memory" is a statement about latency, not confinement.
            assertEquals(0, sink.count());
            assertTrue(sink.snapshot().isEmpty());
            TraceHook.exit(frame);
        } finally {
            TraceHook.setSink(prior);
        }
    }

    @Test
    void aRefusalWritesASidecarBesideTheTraceAndDestroysIt(@TempDir Path tmp) throws Exception {
        Path tracePath = tmp.resolve("run.trace.jsonl");
        Boundary b = new Boundary().forbidden(PATTERN);
        TraceHook.Sink sink = new TraceHook.Sink(tracePath.toString(), b);
        TraceHook.Sink prior = TraceHook.sink();
        TraceHook.setSink(sink);
        try {
            TraceHook.enter("f", "a.java:1", new String[]{"token"}, new Object[]{SECRET});
        } finally {
            TraceHook.setSink(prior);
            sink.close();
        }

        assertFalse(Files.exists(tracePath), "the trace file must be destroyed");
        Path refusal = Path.of(TraceHook.refusalPath(tracePath.toString()));
        // The refusal goes to a sidecar because a traced run can trip this and still exit 0 — a
        // guard that only shouted into stderr would be a guard nobody enforces.
        assertTrue(Files.exists(refusal), "the refusal must be recorded beside the trace");
        assertEquals(PATTERN, Files.readString(refusal));
    }

    @Test
    void tracingStaysOffAfterAHit() {
        Boundary b = new Boundary().forbidden(PATTERN);
        TraceHook.Sink sink = new TraceHook.Sink(null, b);
        TraceHook.Sink prior = TraceHook.sink();
        TraceHook.setSink(sink);
        try {
            TraceHook.enter("f", "a.java:1", new String[]{"token"}, new Object[]{SECRET});
            TraceHook.enter("g", "a.java:9", new String[]{"harmless"}, new Object[]{"fine"});
            assertEquals(0, sink.count(), "tracing is disabled permanently after a hit");
        } finally {
            TraceHook.setSink(prior);
        }
    }

    // ------------------------------------------------------- declaration time

    @Test
    void aBadPatternFailsAtDeclarationTimeNotWhenItWouldHaveFired() {
        assertThrows(IllegalArgumentException.class, () -> new Boundary().forbidden("([unclosed"));
    }
}
