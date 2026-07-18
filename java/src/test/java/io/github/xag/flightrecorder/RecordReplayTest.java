package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/** Record a run, then replay it against the real code. */
class RecordReplayTest {

    private static Recording record(Path dir) throws Exception {
        try (Recorder rec = Recorder.open(dir.toString(), Toy.plainBoundary())) {
            rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
            return Recording.load(rec.path());
        }
    }

    @Test
    void aRecordedRunReplaysExactly(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);

        assertTrue(r.ok(), () -> "expected a clean replay:\n" + Replay.format(0, r));
        assertTrue(r.resultMatch);
        assertTrue(r.errorMatch);
        assertEquals(r.eventsTotal, r.eventsConsumed, "every recorded event must be consumed");
        assertNull(r.divergence);
    }

    @Test
    void theWriteIsComparedNotExecuted(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);

        // The write is captured for invariants but never performed — replaying a run must not
        // charge the card twice.
        assertEquals(1, r.writes.size());
        assertEquals("set", r.writes.get(0).get("op"));
    }

    @Test
    void codeThatAsksADifferentQuestionDiverges(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        // The code changed: it now reads a different key first.
        Replay.Resolver changed = (fn, kwargs) -> () -> Toy.storeGet("bob");

        Replay.Report r = Replay.replayCall(tape.call(0), changed, Toy.plainBoundary(), false);
        assertFalse(r.ok());
        assertNotNull(r.divergence, "a different argument at the boundary must be reported");
        assertTrue(r.divergence.contains("different arguments"), r.divergence);
    }

    @Test
    void codeThatStopsAskingDiverges(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        // The code returns without ever reaching the boundary: a shorter path than recorded.
        Replay.Resolver lazy = (fn, kwargs) -> () -> Map.of("name", "Alice");

        Replay.Report r = Replay.replayCall(tape.call(0), lazy, Toy.plainBoundary(), false);
        assertFalse(r.ok(), "unconsumed events mean the code stopped asking");
        assertTrue(r.eventsConsumed < r.eventsTotal);
    }

    @Test
    void aRecordedErrorIsRevivedWithItsRealType(@TempDir Path tmp) throws Exception {
        try (Recorder rec = Recorder.open(tmp.toString(), Toy.plainBoundary())) {
            assertThrows(Toy.ToyError.class, () ->
                    rec.call("explode", Recorder.kwargs("user", "ghost"),
                            () -> Toy.explode(Map.of("user", "ghost"))));

            Recording tape = Recording.load(rec.path());
            Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.plainBoundary(), false);

            // The boundary declared a reviver, so the code's own `catch (ToyError)` still fires.
            // Without this the replay would take a path the original never took and then blame the
            // code for the difference.
            assertTrue(r.errorMatch, () -> Replay.format(0, r));
            assertEquals("no such key: ghost", r.replayedError);
        }
    }

    @Test
    void withoutAReviverTheStandInIsHonestAboutTheType(@TempDir Path tmp) throws Exception {
        try (Recorder rec = Recorder.open(tmp.toString(), Toy.plainBoundary())) {
            assertThrows(Toy.ToyError.class, () ->
                    rec.call("explode", Recorder.kwargs("user", "ghost"),
                            () -> Toy.explode(Map.of("user", "ghost"))));

            Recording tape = Recording.load(rec.path());
            Boundary noRevivers = new Boundary().maskFields("password");
            Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), noRevivers, false);

            assertNotNull(r.replayedError);
        }
    }

    @Test
    void aGateThatNeverAdmitsLeavesNoFile(@TempDir Path tmp) throws Exception {
        Boundary b = Toy.plainBoundary().enabledWhen((fn, kw) -> false);
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            Object out = rec.call("greet", Recorder.kwargs("user", "alice"),
                    () -> Toy.greet(Map.of("user", "alice")));
            assertNotNull(out, "the call still runs for real");
            assertNull(rec.path(), "a gate that never fires opens no file");
        }
        assertEquals(0, java.nio.file.Files.list(tmp).count(), "nothing should have been written");
    }

    @Test
    void aGateThatThrowsRefusesRatherThanBreakingTheCall(@TempDir Path tmp) throws Exception {
        Boundary b = Toy.plainBoundary().enabledWhen((fn, kw) -> { throw new RuntimeException("bad gate"); });
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            Object out = rec.call("greet", Recorder.kwargs("user", "alice"),
                    () -> Toy.greet(Map.of("user", "alice")));
            assertNotNull(out, "a gate that throws must never break the call it was asked about");
            assertNull(rec.path());
        }
    }

    @Test
    void seqIsOneBasedAndContiguous(@TempDir Path tmp) throws Exception {
        try (Recorder rec = Recorder.open(tmp.toString(), Toy.plainBoundary())) {
            for (int i = 0; i < 3; i++) {
                rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
            }
            Recording tape = Recording.load(rec.path());
            assertEquals(3, tape.numCalls());
            for (int i = 0; i < 3; i++) {
                assertEquals((long) (i + 1), tape.call(i).raw().get("seq"));
            }
        }
    }

    @Test
    void theSinkIsHandedTheWholeSessionEachTime(@TempDir Path tmp) throws Exception {
        StringBuilder last = new StringBuilder();
        int[] calls = {0};
        Boundary b = Toy.plainBoundary().publishingTo((name, text) -> {
            calls[0]++;
            last.setLength(0);
            last.append(text);
        });
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
        }
        assertTrue(calls[0] >= 2, "published after the header and after the call");
        // Being handed the whole session is what makes an overwriting sink sufficient.
        assertTrue(last.toString().contains("\"ev\":\"session\""));
        assertTrue(last.toString().contains("\"ev\":\"call\""));
    }

    @Test
    void aSinkThatThrowsNeverBreaksTheCall(@TempDir Path tmp) throws Exception {
        Boundary b = Toy.plainBoundary().publishingTo((name, text) -> {
            throw new RuntimeException("the bucket is on fire");
        });
        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            Object out = rec.call("greet", Recorder.kwargs("user", "alice"),
                    () -> Toy.greet(Map.of("user", "alice")));
            assertNotNull(out, "recording must never be the reason a call fails");
        }
    }

    @Test
    void aTornFinalLineIsToleratedAndTheRestIsStillEvidence(@TempDir Path tmp) throws Exception {
        Recording tape = record(tmp);
        String text = java.nio.file.Files.readString(java.nio.file.Paths.get(
                java.util.Objects.requireNonNull(
                        java.nio.file.Files.list(tmp).findFirst().orElseThrow()).toString()));
        Recording torn = Recording.parse(text + "{\"ev\":\"call\",\"seq\":2,\"fn\":\"gre");
        assertEquals(tape.numCalls(), torn.numCalls(), "the torn line is discarded, not raised");
    }

    /**
     * A secret the code produces from somewhere other than its inputs is masked on the tape and
     * raw in the live run. Comparing those two directly reports a divergence on every such value —
     * "the code changed" when nothing changed but the masking. So replay re-masks its own side
     * before comparing, which is what makes idempotent redaction a requirement rather than a
     * preference.
     */
    @Test
    void replayReMasksItsOwnSideBeforeComparing(@TempDir Path tmp) throws Exception {
        Boundary b = new Boundary().maskFields("password");
        // The secret comes from a constant, NOT from kwargs — so the replayed code regenerates it
        // raw rather than reading the already-masked value back off the tape.
        Replay.Resolver code = (fn, kwargs) -> () ->
                Recorder.effect("store.set", List.of(Map.of("password", "hunter2")), () -> "OK");

        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            rec.call("save", Map.of(), () ->
                    Recorder.effect("store.set", List.of(Map.of("password", "hunter2")), () -> "OK"));

            String text = java.nio.file.Files.readString(java.nio.file.Path.of(rec.path()));
            assertFalse(text.contains("hunter2"), "precondition: the tape is masked");

            Recording tape = Recording.load(rec.path());
            Replay.Report r = Replay.replayCall(tape.call(0), code, b, false);
            assertNull(r.divergence, () -> "a masked value must not read as a changed one:\n"
                    + Replay.format(0, r));
            assertTrue(r.ok(), () -> Replay.format(0, r));
        }
    }

    @Test
    void aTapeFromAnotherRuntimeReadsIdentically() throws Exception {
        // The whole point of freezing the format: this Java reader recovers a Go tape's account.
        Recording go = Recording.load("../spec/fixtures/go-sem-toy.jsonl");
        Recording java = Recording.load("../spec/fixtures/java-sem-toy.jsonl");
        assertEquals(go.call(0).renderSpans(), java.call(0).renderSpans(),
                "the same scenario must read the same whoever recorded it");
    }
}
