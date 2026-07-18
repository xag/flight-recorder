package io.github.xag.flightrecorder;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The toy application the cross-runtime fixtures are recorded from.
 *
 * <p>Every runtime ships this same shape, so {@code java-toy.jsonl} and {@code go-toy.jsonl} differ
 * only in the runtime key and the timestamps. That is what makes the fixture sweep meaningful: a
 * reader that can recover one runtime's account must recover every runtime's.
 */
final class Toy {

    private Toy() {}

    /** An application error carrying the values it was built from, so replay can rebuild it with
     *  its real type rather than a stand-in. */
    static final class ToyError extends RuntimeException implements Recorder.FlightError {
        private final List<Object> args;

        ToyError(String message, int code) {
            super(message);
            this.args = List.of(message, (long) code);
        }

        ToyError(List<Object> args) {
            super(args.isEmpty() ? "" : String.valueOf(args.get(0)));
            this.args = args;
        }

        @Override public List<Object> errorArgs() { return args; }
    }

    // ------------------------------------------------------- the outside world

    static Map<String, Object> storeGet(String key) {
        return Recorder.effect("store.get", List.of(key), () -> {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("name", "Alice");
            row.put("x", 3L);
            return row;
        });
    }

    static String storeSet(String key, Object value) {
        return Recorder.effect("store.set", List.of(key, value), () -> "OK");
    }

    static Object storeBoom(String key) {
        return Recorder.effect("store.boom", List.of(key), () -> {
            throw new ToyError("no such key: " + key, 42);
        });
    }

    // ------------------------------------------------------------- the toy call

    /** The rich basic scenario: an effect, a chained read, all four random shapes, both clocks,
     *  and a write. */
    static Map<String, Object> greet(Map<String, Object> kwargs) {
        String user = String.valueOf(kwargs.get("user"));

        Map<String, Object> row = storeGet(user);

        Recorder.query("stream", "collection(\"users\").where(\"x\", \">\", 0)", () -> {
            List<Snapshot> out = new ArrayList<>();
            out.add(Snapshot.of("0", Map.of("name", "alpha", "x", 1L)));
            out.add(Snapshot.of("1", Map.of("name", "beta", "x", 2L)));
            return out;
        });

        Recorder.sampleIndices(3, 2);
        Recorder.randBytes(4);
        Recorder.randFloat();
        Recorder.randInt(100);

        LocalDateTime at = Recorder.now();
        Recorder.perf();

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("at", at);
        Recorder.exec("set", "store.set(greeted:" + user + ")", List.of(payload), () -> { });

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("name", row.get("name"));
        return result;
    }

    /** The failing call: an effect that raises, so the tape carries both an {@code fx.err} and a
     *  non-null {@code call.error}. */
    static Object explode(Map<String, Object> kwargs) {
        return storeBoom(String.valueOf(kwargs.get("user")));
    }

    /**
     * The universal {@code enrol} scenario, identical across all runtimes so a reader recovers the
     * same account whoever wrote the tape.
     *
     * <p>A clock read OUTSIDE the span (it belongs to the call, not the act), then a span enclosing
     * a nested span, a point note, and a second span that fails — so the tape carries an
     * {@code outcome: "error"} end whose exception the caller catches and turns into a note.
     */
    static Map<String, Object> enrol(Map<String, Object> kwargs) {
        String user = String.valueOf(kwargs.get("user"));
        String password = String.valueOf(kwargs.get("password"));
        LocalDateTime started = Recorder.now();

        Map<String, Object> spanData = new LinkedHashMap<>();
        spanData.put("user", user);
        spanData.put("started", started);
        spanData.put("password", password); // redaction must reach INTO span data too

        return Recorder.span("enrol", spanData, () -> {
            Map<String, Object> row = Recorder.span("load_corpus", () -> storeGet(user));
            Recorder.note("corpus_read", Map.of("found", true));

            Map<String, Object> regData = new LinkedHashMap<>();
            regData.put("password", password);
            try {
                Recorder.span("register", regData, () -> {
                    Map<String, Object> body = new LinkedHashMap<>();
                    body.put("password", password);
                    storeSet("user:" + user, body);
                    storeBoom(user);
                });
            } catch (ToyError e) {
                Recorder.note("registration_failed", Map.of("why", Recorder.render(e)));
            } catch (Errors.ReplayedEffectError e) {
                // On replay the recorded error arrives as its revived type when the boundary
                // declared a reviver, and as this stand-in when it did not. Both take this path.
                Recorder.note("registration_failed", Map.of("why", e.repr));
            }

            Map<String, Object> out = new LinkedHashMap<>();
            out.put("user", user);
            out.put("name", row.get("name"));
            return out;
        });
    }

    // ----------------------------------------------------------- the boundaries

    static Boundary plainBoundary() {
        return new Boundary()
                .constant("toy.LIMIT", 3)
                .maskFields("password")
                .reviving("ToyError", ToyError::new);
    }

    static Boundary semBoundary() {
        return new Boundary()
                .maskFields("password")
                .reviving("ToyError", ToyError::new);
    }

    /** Maps a recorded call back to the code that produced it. */
    static Replay.Resolver resolver() {
        return (fn, kwargs) -> switch (fn) {
            case "greet" -> () -> greet(kwargs);
            case "explode" -> () -> explode(kwargs);
            case "enrol" -> () -> enrol(kwargs);
            default -> null;
        };
    }
}
