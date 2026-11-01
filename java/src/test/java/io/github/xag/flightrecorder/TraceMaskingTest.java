package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.*;

/**
 * The trace is masked as the tape is: the boundary's field rules and scrub rewrite every trace
 * event's PAYLOAD ({@code args}, {@code d}, {@code v}) before the forbid tripwire reads the line,
 * and never its envelope ({@code e}, {@code fn}, {@code at}).
 *
 * <p>And a traced local holding the runtime's own machinery — a class, a reflected member, a
 * thread, a class loader — is written as its name, never walked: such a value is code and process
 * state, not something the execution computed.
 */
class TraceMaskingTest {

    private static final String KEY = "sk-live-9f8e7d6c5b4a39281706";
    private static final String KEY_SHAPE = "sk-live-[0-9a-f]+";
    private static final String TOY = "src/test/resources/traced/TracedToy.java";

    /** Drives the hook the way instrumented code does: a call, a line, a return. */
    private static List<Map<String, Object>> traceOneFrame(TraceHook.Sink sink, String[] names, Object[] values) {
        TraceHook.Sink prior = TraceHook.sink();
        TraceHook.setSink(sink);
        try {
            long f = TraceHook.enter("derive", "a.java:1", names, values);
            Object[] changed = new Object[values.length];
            for (int i = 0; i < values.length; i++) changed[i] = values[i] instanceof String s ? s + "!" : values[i];
            TraceHook.line(f, "derive", "a.java:2", names, changed);
            TraceHook.line(f, "derive", "a.java:3", names, changed);
            // The LAST value is returned: a returned value has no name for a field rule to reach.
            TraceHook.returned(f, "derive", "a.java:3", values.length == 0 ? null : values[values.length - 1]);
            TraceHook.exit(f);
        } finally {
            TraceHook.setSink(prior);
            sink.close();
        }
        return sink.snapshot().events;
    }

    private static List<Map<String, Object>> lines(Path trace) throws Exception {
        List<Map<String, Object>> out = new ArrayList<>();
        for (String l : Files.readAllLines(trace, StandardCharsets.UTF_8)) {
            if (!l.isBlank()) out.add(Json.asMap(Json.parse(l)));
        }
        return out;
    }

    // ----------------------------------------------------------- the payload is masked

    @Test
    void theScrubMasksTheTraceAsItMasksTheTape(@TempDir Path tmp) throws Exception {
        Path trace = tmp.resolve("trace.jsonl");
        Boundary b = new Boundary().scrubbingWith(s -> s.replace(KEY, "[KEY]"));
        List<Map<String, Object>> events = traceOneFrame(new TraceHook.Sink(trace.toString(), b),
                new String[]{"key", "note"}, new Object[]{KEY, "use " + KEY + " here"});

        String text = Files.readString(trace, StandardCharsets.UTF_8);
        assertTrue(text.contains("[KEY]"), "the scrub's mask must be in the trace file");
        assertFalse(text.contains(KEY), "the key must not reach the trace file");
        // The in-memory buffer is read by invariants while the run goes: it is masked too.
        assertFalse(Json.write(events).contains(KEY), "the key must not reach the in-memory trace");
        assertTrue(Json.write(events).contains("[KEY]"));
    }

    @Test
    void aRedactedFieldNameMasksTheLocalOfThatName(@TempDir Path tmp) throws Exception {
        // A trace's payload is keyed by local name as a tape's kwargs are keyed by parameter name,
        // so a field rule reaches a local exactly as it reaches an argument.
        Path trace = tmp.resolve("trace.jsonl");
        Boundary b = new Boundary().maskFields("secret");
        traceOneFrame(new TraceHook.Sink(trace.toString(), b),
                new String[]{"secret", "user"}, new Object[]{KEY, "alice"});

        List<Map<String, Object>> events = lines(trace);
        Map<String, Object> call = events.stream().filter(e -> "C".equals(e.get("e"))).findFirst().orElseThrow();
        assertEquals(Serial.REDACTED, Json.asMap(call.get("args")).get("secret"));
        assertEquals("alice", Json.asMap(call.get("args")).get("user"), "only the named local is masked");
        boolean sawDelta = false;
        for (Map<String, Object> e : events) {
            Map<String, Object> d = Json.asMap(e.get("d"));
            if (d != null && d.containsKey("secret")) {
                assertEquals(Serial.REDACTED, d.get("secret"));
                sawDelta = true;
            }
        }
        assertTrue(sawDelta, "the local's change must still be reported, masked");
        assertFalse(Files.readString(trace, StandardCharsets.UTF_8).contains(KEY));
    }

    @Test
    void theTraceEnvelopeIsNotRedacted(@TempDir Path tmp) throws Exception {
        // As on the tape, rules rewrite payloads only: a rule named like an envelope key, or a
        // scrub that matches a function name, cannot take the structure of the trace apart.
        Path trace = tmp.resolve("trace.jsonl");
        Boundary b = new Boundary().maskFields("e", "fn", "at").scrubbingWith(s -> s.replace("derive", "XXX"));
        traceOneFrame(new TraceHook.Sink(trace.toString(), b), new String[]{"x"}, new Object[]{"derive"});

        List<Map<String, Object>> events = lines(trace);
        Set<Object> kinds = new HashSet<>();
        for (Map<String, Object> e : events) kinds.add(e.get("e"));
        assertTrue(kinds.containsAll(Set.of("H", "C", "L", "R")), "every event kind survives: " + kinds);
        for (Map<String, Object> e : events.subList(1, events.size())) {
            assertEquals("derive", e.get("fn"));
            assertTrue(String.valueOf(e.get("at")).startsWith("a.java:"));
        }
        // ...while the payload, which is the app's data, is scrubbed.
        Map<String, Object> call = events.get(1);
        assertEquals("XXX", Json.asMap(call.get("args")).get("x"));
    }

    @Test
    void forbidRunsOnTheMaskedLine() {
        // Declaring both the scrub and a forbid on the same shape is the recommended pairing: the
        // tripwire fires only on what the scrub left, so a cleaned trace is not refused.
        Boundary b = new Boundary().scrubbing(KEY_SHAPE).forbidden(KEY_SHAPE);
        TraceHook.Sink sink = new TraceHook.Sink(null, b);
        List<Map<String, Object>> events = traceOneFrame(sink, new String[]{"key"}, new Object[]{KEY});
        assertNull(sink.refused(), "a scrubbed trace must not trip the forbid that guards it");
        assertFalse(events.isEmpty());
        assertFalse(Json.write(events).contains(KEY));
    }

    @Test
    void aTracedRunWritesTheMaskNotTheKey() throws Exception {
        Boundary b = new Boundary().scrubbing(KEY_SHAPE).forbidden(KEY_SHAPE);
        Tracer.Run run = Tracer.run(List.of(TOY), "TracedToy", "holdsASecret", b, KEY);
        assertEquals(("Bearer " + KEY).length() + KEY.length(), run.result(), "masking must not change the run");
        String text = run.trace().toJsonl();
        assertFalse(text.contains(KEY));
        assertEquals(Serial.REDACTED, run.trace().last("secret").value());
        assertEquals("Bearer " + Serial.REDACTED, run.trace().last("header").value());
    }

    // ---------------------------------------- machinery is a name, never walked

    @Test
    void aClassIsTracedAsItsName() {
        Object enc = assertTimeoutPreemptively(Duration.ofSeconds(10),
                () -> TraceValue.toTraceJsonable(String.class));
        assertEquals(Map.of("__opaque__", "<class java.lang.String>"), enc);
    }

    record Holder(Class<?> type, Thread thread, int n) {}

    @Test
    void whatIsNotDataIsNeverWalked() throws Exception {
        // A class, a reflected member, a thread, a class loader, a module: each names what it is.
        // Walked through its getters, a class reaches every method of every class it mentions —
        // tens of thousands of objects, re-encoded on every executed line of the frame holding it.
        Map<String, Object> mods = new LinkedHashMap<>();
        mods.put("types", List.of(String.class, Integer.class));
        Object[] values = {
                String.class,
                String.class.getMethod("length"),
                String.class.getDeclaredField("value"),
                Thread.currentThread(),
                ClassLoader.getSystemClassLoader(),
                String.class.getModule(),
                ProcessHandle.current().info(),
                new Holder(String.class, Thread.currentThread(), 3),
                mods,
        };
        for (Object v : values) {
            String out = assertTimeoutPreemptively(Duration.ofSeconds(10),
                    () -> Json.write(TraceValue.toTraceJsonable(v)), String.valueOf(v));
            assertTrue(out.length() < 400, "a reference, not a walk: " + v.getClass().getName() + " → " + out.length() + " chars");
        }
        assertEquals(Map.of("__opaque__", "<method java.lang.String.length>"),
                TraceValue.toTraceJsonable(String.class.getMethod("length")));
        Map<String, Object> held = Json.asMap(TraceValue.toTraceJsonable(new Holder(String.class, null, 3)));
        assertEquals(Map.of("__opaque__", "<class java.lang.String>"), held.get("type"));
        assertEquals(3L, held.get("n"), "the data beside the reference is still data");
        // The process's command line is process state too, and may carry a -D credential.
        assertEquals(Map.of("__opaque__", "<process info>"), TraceValue.toTraceJsonable(ProcessHandle.current().info()));
    }

    /**
     * The environment can only reach a trace through a value that holds it. Java cannot set an
     * environment variable in its own process, so a child JVM is started with a sentinel in its
     * environment; it traces a frame holding a class, a thread and a class loader, and encodes
     * each of them directly, into a file this test then reads.
     */
    @Test
    void aTracedFrameHoldingMachineryWritesNoEnvironment(@TempDir Path tmp) throws Exception {
        String sentinel = "fr-env-sentinel-" + UUID.randomUUID();
        Path out = tmp.resolve("trace.jsonl");
        String java = Paths.get(System.getProperty("java.home"), "bin", "java").toString();
        ProcessBuilder pb = new ProcessBuilder(java, "-cp", System.getProperty("java.class.path"),
                EnvChild.class.getName(), out.toString(), TOY);
        pb.environment().put("FR_TRACE_SENTINEL", sentinel);
        pb.redirectErrorStream(true);
        pb.redirectOutput(tmp.resolve("child.log").toFile());
        Process p = pb.start();
        boolean done = p.waitFor(90, TimeUnit.SECONDS);
        if (!done) p.destroyForcibly();
        assertTrue(done, "the traced child must finish: a reference costs nothing to encode");
        assertEquals(0, p.exitValue(), () -> readOrEmpty(tmp.resolve("child.log")));

        String text = Files.readString(out, StandardCharsets.UTF_8);
        assertFalse(text.contains(sentinel), "the environment must not reach the trace");
        assertTrue(text.contains("\"type\":{\"__opaque__\":\"<class java.lang.String>\"}"), text);
        assertTrue(text.contains("<thread main>"), text);
    }

    private static String readOrEmpty(Path p) {
        try { return Files.readString(p, StandardCharsets.UTF_8); } catch (Exception e) { return ""; }
    }

    /** The child half of {@link #aTracedFrameHoldingMachineryWritesNoEnvironment}. */
    public static final class EnvChild {
        public static void main(String[] args) throws Exception {
            Tracer.Run run = Tracer.run(List.of(args[1]), "TracedToy", "holdsMachinery");
            StringBuilder b = new StringBuilder(run.trace().toJsonl());
            for (Object v : new Object[]{String.class, String.class.getMethod("length"), Thread.currentThread(),
                    ClassLoader.getSystemClassLoader(), ClassLoader.getPlatformClassLoader(),
                    String.class.getModule(), ProcessHandle.current().info()}) {
                b.append(Json.write(TraceValue.toTraceJsonable(v))).append('\n');
                b.append(Json.write(Serial.toJsonable(v))).append('\n');
            }
            Files.writeString(Paths.get(args[0]), b.toString(), StandardCharsets.UTF_8);
            System.exit(0);
        }
    }
}
