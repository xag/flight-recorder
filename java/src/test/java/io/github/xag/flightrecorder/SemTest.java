package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Semantic spans: the app's own account of what it was doing, next to the evidence.
 *
 * <p>A span claiming to have charged a card, with no call beneath it to the thing that charges
 * cards, is a claim a reader can refute. That juxtaposition is the whole feature.
 */
class SemTest {

    private static Recording recordEnrol(Path dir) throws Exception {
        try (Recorder rec = Recorder.open(dir.toString(), Toy.semBoundary())) {
            Map<String, Object> kw = Recorder.kwargs("user", "alice", "password", "hunter2");
            rec.call("enrol", kw, () -> Toy.enrol(kw));
            return Recording.load(rec.path());
        }
    }

    @Test
    void theSpanTreeIsRecoveredFromOrderAlone(@TempDir Path tmp) throws Exception {
        Recording.SpanNode root = recordEnrol(tmp).call(0).spans();

        assertEquals("enrol", root.name);
        assertEquals("call", root.phase);

        // enrol > [load_corpus, corpus_read, register, registration_failed]
        Recording.SpanNode enrol = root.children.get(0);
        assertEquals("enrol", enrol.name);
        assertEquals("span", enrol.phase);
        assertEquals("ok", enrol.outcome);

        List<String> names = enrol.children.stream().map(c -> c.name).toList();
        assertEquals(List.of("load_corpus", "corpus_read", "register", "registration_failed"), names);
    }

    @Test
    void aSpanWhoseBodyThrewEndsWithOutcomeError(@TempDir Path tmp) throws Exception {
        Recording.SpanNode enrol = recordEnrol(tmp).call(0).spans().children.get(0);
        Recording.SpanNode register = enrol.children.stream()
                .filter(c -> c.name.equals("register")).findFirst().orElseThrow();

        // The end still lands even though the body threw. A span that vanished on the error path
        // would make a failed run look like a run that told a shorter story.
        assertEquals("error", register.outcome);
    }

    @Test
    void rawEventsHangUnderTheSpanThatEnclosedThem(@TempDir Path tmp) throws Exception {
        Recording.SpanNode enrol = recordEnrol(tmp).call(0).spans().children.get(0);

        Recording.SpanNode loadCorpus = enrol.children.get(0);
        assertEquals(1, loadCorpus.events.size(), "load_corpus encloses exactly its one read");
        assertEquals("store.get", loadCorpus.events.get(0).get("fn"));

        Recording.SpanNode register = enrol.children.stream()
                .filter(c -> c.name.equals("register")).findFirst().orElseThrow();
        assertEquals(2, register.events.size(), "register encloses the set and the boom");
    }

    @Test
    void theClockReadOutsideTheSpanBelongsToTheCall(@TempDir Path tmp) throws Exception {
        Recording.SpanNode root = recordEnrol(tmp).call(0).spans();
        assertEquals(1, root.events.size());
        assertEquals("now", root.events.get(0).get("k"));
    }

    @Test
    void redactionReachesIntoSpanData(@TempDir Path tmp) throws Exception {
        Recording tape = recordEnrol(tmp);
        Map<String, Object> begin = tape.call(0).event("sem", 0);
        Map<String, Object> data = Json.asMap(begin.get("data"));
        assertEquals("[REDACTED]", data.get("password"));
    }

    @Test
    void emptyDataIsOmittedNotWrittenAsAnEmptyObject(@TempDir Path tmp) throws Exception {
        Recording tape = recordEnrol(tmp);
        // load_corpus was opened with no data — the absence of detail is not itself a detail.
        Map<String, Object> loadCorpus = tape.call(0).event("sem", 1);
        assertEquals("load_corpus", loadCorpus.get("name"));
        assertFalse(loadCorpus.containsKey("data"));
    }

    @Test
    void sidsAreUniqueWithinACallAndAnEndRepeatsItsBegin(@TempDir Path tmp) throws Exception {
        Recording tape = recordEnrol(tmp);
        Map<String, Object> begin = tape.call(0).event("sem", 0);
        assertEquals(1L, begin.get("sid"));

        // The outermost span's end repeats sid 1.
        List<Map<String, Object>> sems = tape.call(0).events().stream()
                .filter(e -> "sem".equals(e.get("k"))).toList();
        Map<String, Object> last = sems.get(sems.size() - 1);
        assertEquals("end", last.get("phase"));
        assertEquals(1L, last.get("sid"));
    }

    @Test
    void semsAreNeverFedBackToTheReplayedCode(@TempDir Path tmp) throws Exception {
        Recording tape = recordEnrol(tmp);
        Replay.Report r = Replay.replayCall(tape.call(0), Toy.resolver(), Toy.semBoundary(), false);

        assertTrue(r.ok(), () -> Replay.format(0, r));
        // Testimony is not evidence. But it IS consumed — sems trailing the last boundary answer
        // must still be counted, or the replay reports a shorter path than was recorded.
        assertEquals(r.eventsTotal, r.eventsConsumed);
        assertNull(r.semDivergence);
    }

    @Test
    void aChangedAccountIsReportedButDoesNotGate(@TempDir Path tmp) throws Exception {
        Recording tape = recordEnrol(tmp);
        // Same boundary questions, same answers, same result — but the code now tells a different
        // story about what it was doing.
        Replay.Resolver renamed = (fn, kwargs) -> () -> {
            String user = String.valueOf(kwargs.get("user"));
            String password = String.valueOf(kwargs.get("password"));
            Recorder.now();
            return Recorder.span("signup", () -> {           // was "enrol"
                Map<String, Object> row = Recorder.span("load_corpus", () -> Toy.storeGet(user));
                Recorder.note("corpus_read", Map.of("found", true));
                try {
                    Recorder.span("register", () -> {
                        Toy.storeSet("user:" + user, Map.of("password", password));
                        Toy.storeBoom(user);
                    });
                } catch (Toy.ToyError | Errors.ReplayedEffectError e) {
                    // Deliberately narrow. Catching RuntimeException here would swallow the
                    // replay engine's own divergence signal and make this test pass for the
                    // wrong reason.
                    Recorder.note("registration_failed", Map.of("why", "x"));
                }
                Map<String, Object> out = new java.util.LinkedHashMap<>();
                out.put("user", user);
                out.put("name", row.get("name"));
                return out;
            });
        };

        Replay.Report r = Replay.replayCall(tape.call(0), renamed, Toy.semBoundary(), false);
        assertNotNull(r.semDivergence, "a changed account must be reported");
        assertTrue(r.semDivergence.contains("account of what it was doing"));
        // ...and must NOT gate: renaming a span is a refactor as easily as it is a bug.
        assertTrue(r.ok(), () -> "a semantic divergence must not fail the replay:\n" + Replay.format(0, r));
    }

    @Test
    void theRenderedTreeReadsTopDown(@TempDir Path tmp) throws Exception {
        String rendered = recordEnrol(tmp).call(0).renderSpans();
        String[] lines = rendered.split("\n");
        assertTrue(lines[0].startsWith("enrol  ok"), lines[0]);
        assertTrue(rendered.contains("load_corpus  ok  (1 fx)"), rendered);
        assertTrue(rendered.contains("register  ERROR  (2 fx)"), rendered);
        assertTrue(rendered.contains("- corpus_read  found=true"), rendered);
    }
}
