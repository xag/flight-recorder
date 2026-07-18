package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.*;

/** Wrapping what the app holds: a transparent proxy, not a mock. */
class WrapTest {

    record Row(String name, int x) {}

    interface Store {
        Row read(String key);
        void write(String key, String value);
        String untouched(String key);
    }

    static final class RealStore implements Store {
        final AtomicInteger reads = new AtomicInteger();
        final AtomicInteger writes = new AtomicInteger();

        @Override public Row read(String key) {
            reads.incrementAndGet();
            return new Row("Alice", 3);
        }

        @Override public void write(String key, String value) { writes.incrementAndGet(); }

        @Override public String untouched(String key) { return "passthrough"; }
    }

    @Test
    void aWrappedCallIsForwardedToTheRealThingAndWrittenDown(@TempDir Path tmp) throws Exception {
        RealStore real = new RealStore();
        Store store = Recorder.wrapAs(Store.class, real, "kv", "read", "write");

        try (Recorder rec = Recorder.open(tmp.toString(), new Boundary())) {
            Row row = rec.call("load", Map.of(), () -> store.read("alice"));
            assertEquals(new Row("Alice", 3), row);
            assertEquals(1, real.reads.get(), "the real object was actually called");

            Recording tape = Recording.load(rec.path());
            assertEquals("kv.read", tape.call(0).event("fx").get("fn"),
                    "the prefix qualifies the name so two clients never collide on the tape");
        }
    }

    @Test
    void aMethodNotNamedIsInvisibleToTheRecorder(@TempDir Path tmp) throws Exception {
        RealStore real = new RealStore();
        Store store = Recorder.wrapAs(Store.class, real, "kv", "read");

        try (Recorder rec = Recorder.open(tmp.toString(), new Boundary())) {
            String out = rec.call("x", Map.of(), () -> store.untouched("k"));
            assertEquals("passthrough", out);

            Recording tape = Recording.load(rec.path());
            assertNull(tape.call(0).event("fx"), "an unnamed method is forwarded, not recorded");
        }
    }

    /**
     * The reason coercion exists. A tape stores structure, not types — so the recorded {@code Row}
     * comes back as a map. Without fitting it to the declared return type, the app fails on a cast
     * the recorder caused.
     */
    @Test
    void replayHandsBackTheDeclaredTypeNotARawMap(@TempDir Path tmp) throws Exception {
        RealStore real = new RealStore();
        Store store = Recorder.wrapAs(Store.class, real, "kv", "read", "write");

        try (Recorder rec = Recorder.open(tmp.toString(), new Boundary())) {
            rec.call("load", Map.of(), () -> store.read("alice"));

            Recording tape = Recording.load(rec.path());
            Replay.Resolver code = (fn, kwargs) -> () -> store.read("alice");
            Replay.Report r = Replay.replayCall(tape.call(0), code, new Boundary(), false);

            assertTrue(r.ok(), () -> Replay.format(0, r));
            // The real store was called once (during record) and NOT during replay.
            assertEquals(1, real.reads.get(), "replay must not reach the real world");
        }
    }

    @Test
    void aWriteIsRecordedAndNotReExecutedOnReplay(@TempDir Path tmp) throws Exception {
        RealStore real = new RealStore();
        Store store = Recorder.wrapAs(Store.class, real, "kv", "read", "write");

        try (Recorder rec = Recorder.open(tmp.toString(), new Boundary())) {
            rec.call("save", Map.of(), () -> store.write("alice", "v"));
            assertEquals(1, real.writes.get());

            Recording tape = Recording.load(rec.path());
            Replay.Resolver code = (fn, kwargs) -> () -> { store.write("alice", "v"); return null; };
            Replay.replayCall(tape.call(0), code, new Boundary(), false);

            // Replaying a run must not charge the card twice.
            assertEquals(1, real.writes.get(), "the write must not happen again on replay");
        }
    }

    @Test
    void coercionFitsRecordsNumbersAndEnums() {
        assertEquals(new Row("Alice", 3),
                Serial.coerce(Map.of("name", "Alice", "x", 3L), Row.class));
        assertEquals(3, Serial.coerce(3L, int.class));
        assertEquals(3.0, Serial.coerce(3L, double.class));
        // Best-effort: an unfittable shape comes back as it is rather than throwing.
        assertEquals("nope", Serial.coerce("nope", Row.class));
    }
}
