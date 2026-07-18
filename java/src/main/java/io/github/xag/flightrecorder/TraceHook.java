package io.github.xag.flightrecorder;

import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Pattern;

/**
 * What instrumented code calls. <b>Public because it is a compile target</b> — the rewriter splices
 * calls to these methods into a copy of your sources, and that copy has to be able to see them.
 *
 * <p>Two rules govern everything here, and they are absolute:
 *
 * <ol>
 *   <li><b>It must never throw into the observed frame.</b> Every entry point is wrapped. An
 *       exception raised here would propagate into the very execution the trace exists to explain,
 *       turning the instrument into the bug.
 *   <li><b>It must never change what it observes.</b> No {@code toString()} on a live value beyond
 *       the encoder's own guarded path, no locks held across a call back into user code, and
 *       nothing handed back to the program.
 * </ol>
 */
public final class TraceHook {

    private TraceHook() {}

    /** Where the child process is told to write its trace. */
    public static final String ENV_PATH = "FLIGHT_RECORDER_TRACE";

    /** The forbid patterns, as a JSON array of strings. See {@link Sink#vet} on why the guard has
     *  to cross the process boundary. */
    public static final String ENV_FORBID = "FLIGHT_RECORDER_TRACE_FORBID";

    /** The sidecar a refusal writes, beside the trace path. */
    public static final String REFUSAL_SUFFIX = ".forbidden";

    private static volatile Sink sink;
    private static final AtomicLong FRAMES = new AtomicLong();

    /** Installs the sink events go to. Null disables tracing. */
    public static void setSink(Sink s) { sink = s; }

    public static Sink sink() { return sink; }

    /** Whether this process is running with the tracer armed. A test that drives its own traced
     *  child branches on it: false is the orchestrating parent, true is the child. */
    public static boolean live() { return sink != null; }

    /** How many events the tracer has recorded so far. */
    public static int count() {
        Sink s = sink;
        return s == null ? 0 : s.count();
    }

    /** A mark to take before running code, so {@link #live(int)} returns the trace of THAT run
     *  rather than of everything the process has done since it started. */
    public static int mark() { return count(); }

    /** The trace this process has recorded since {@code from}. Empty in an ordinary build. */
    public static Trace live(int from) {
        Sink s = sink;
        return s == null ? Trace.empty() : s.snapshot(from);
    }

    public static String refusalPath(String tracePath) { return tracePath + REFUSAL_SUFFIX; }

    /** Builds an {@code at} location. The rewriter calls this with the ORIGINAL file's line
     *  number. */
    public static String at(String file, int line) { return file + ":" + line; }

    // ------------------------------------------------------------- the frames

    /**
     * One invocation's observation state.
     *
     * <p>Per invocation, not per method: recursive and concurrent calls to the same method each get
     * their own frame, so a delta is a change within one execution rather than an artifact of two
     * executions interleaving.
     */
    static final class Frame {
        final long id;
        final String fn;
        /** The canonical JSON of the last value seen for each name — comparison is on the ENCODED
         *  value, not the live one, because a live-object comparison would call user code (and
         *  would call a type-changing transition equal). */
        final Map<String, String> seen = new HashMap<>();
        String lastAt;

        Frame(long id, String fn, String at) { this.id = id; this.fn = fn; this.lastAt = at; }
    }

    private static final Map<Long, Frame> FRAME_TABLE = new java.util.concurrent.ConcurrentHashMap<>();

    /** Entry into an instrumented function. Returns the frame id the other hooks carry. */
    public static long enter(String fn, String at, String[] names, Object[] values) {
        try {
            Sink s = sink;
            long id = FRAMES.incrementAndGet();
            if (s == null) return id;
            Frame f = new Frame(id, fn, at);
            FRAME_TABLE.put(id, f);
            Map<String, Object> args = new LinkedHashMap<>();
            for (int i = 0; i < names.length && i < values.length; i++) {
                Object enc = TraceValue.toTraceJsonable(values[i]);
                f.seen.put(names[i], Json.canonical(enc));
                args.put(names[i], enc);
            }
            Map<String, Object> ev = new LinkedHashMap<>();
            ev.put("e", "C"); ev.put("fn", fn); ev.put("at", at); ev.put("args", args);
            s.emit(ev);
            return id;
        } catch (Throwable t) {
            return 0;
        }
    }

    /**
     * The locals readable at a statement.
     *
     * <p><b>The delta is reported at the PREVIOUS statement's location, not this one.</b> A hook
     * fires <em>before</em> a statement runs, so what it sees is the work the last statement did;
     * blaming the upcoming line would put the wrong line number on every value in the trace. This
     * is the single easiest thing to get wrong in a tracer, and it is wrong in a way that looks
     * plausible.
     */
    public static void line(long frame, String fn, String at, String[] names, Object[] values) {
        try {
            Sink s = sink;
            if (s == null) return;
            Frame f = FRAME_TABLE.get(frame);
            if (f == null) return;
            Map<String, Object> delta = new LinkedHashMap<>();
            for (int i = 0; i < names.length && i < values.length; i++) {
                Object enc = TraceValue.toTraceJsonable(values[i]);
                String canon = Json.canonical(enc);
                if (!canon.equals(f.seen.get(names[i]))) {
                    f.seen.put(names[i], canon);
                    delta.put(names[i], enc);
                }
            }
            if (!delta.isEmpty()) {
                Map<String, Object> ev = new LinkedHashMap<>();
                ev.put("e", "L"); ev.put("fn", fn); ev.put("at", f.lastAt); ev.put("d", delta);
                s.emit(ev);
            }
            f.lastAt = at;
        } catch (Throwable ignored) {
        }
    }

    /** A return. */
    public static void returning(long frame, String fn, String at, Object value) {
        try {
            Sink s = sink;
            if (s == null) return;
            Map<String, Object> ev = new LinkedHashMap<>();
            ev.put("e", "R"); ev.put("fn", fn); ev.put("at", at);
            ev.put("v", TraceValue.toTraceJsonable(value));
            s.emit(ev);
        } catch (Throwable ignored) {
        }
    }

    /**
     * A return, as an identity passthrough.
     *
     * <p>The rewriter wraps {@code return expr} as {@code return returned(f, fn, at, expr)} rather
     * than declaring a temporary, because it cannot always <em>spell</em> a method's return type in
     * a local declaration (a captured generic, an intersection type, an anonymous class). Being
     * type-preserving also means the rewrite can never change an overload resolution or a
     * conversion the original relied on.
     */
    public static <T> T returned(long frame, String fn, String at, T value) {
        returning(frame, fn, at, value);
        return value;
    }

    /** An exception on the way out of an instrumented frame. */
    public static void raise(long frame, String fn, String at, Throwable t) {
        try {
            Sink s = sink;
            if (s == null) return;
            Map<String, Object> ev = new LinkedHashMap<>();
            ev.put("e", "X"); ev.put("fn", fn); ev.put("at", at);
            ev.put("type", t == null ? "null" : t.getClass().getSimpleName());
            ev.put("v", t == null ? "" : String.valueOf(t.getMessage()));
            s.emit(ev);
        } catch (Throwable ignored) {
        }
    }

    /** Leaving a frame. Drops its observation state so a long run does not accumulate one entry
     *  per invocation forever. */
    public static void exit(long frame) {
        try {
            FRAME_TABLE.remove(frame);
        } catch (Throwable ignored) {
        }
    }

    // -------------------------------------------------------------- the sink

    /**
     * Where trace events go: an in-memory buffer, optionally mirrored to a file.
     *
     * <p>The delta detection lives here rather than in the injected code, deliberately: the hook
     * hands over the whole readable scope on every line and the sink emits only what moved. That
     * keeps the injected call sites trivial, which is what makes them safe to inject.
     */
    public static final class Sink {

        private final List<Map<String, Object>> events = new ArrayList<>();
        private final List<Pattern> forbid = new ArrayList<>();
        private final Path path;
        private Writer out;
        private String refused;

        public Sink() { this(null, null); }

        public Sink(String path, Boundary boundary) {
            this.path = path == null ? null : Paths.get(path);
            if (boundary != null) for (String p : boundary.forbid) forbid.add(Pattern.compile(p));
            if (this.path != null) {
                try {
                    if (this.path.getParent() != null) Files.createDirectories(this.path.getParent());
                    out = Files.newBufferedWriter(this.path, StandardCharsets.UTF_8,
                            StandardOpenOption.CREATE, StandardOpenOption.WRITE,
                            StandardOpenOption.TRUNCATE_EXISTING);
                    Map<String, Object> h = new LinkedHashMap<>();
                    h.put("e", "H");
                    h.put("trace_version", Trace.TRACE_VERSION);
                    out.write(Json.write(h));
                    out.write("\n");
                    out.flush();
                } catch (IOException e) {
                    out = null;
                }
            }
        }

        /** The pattern that refused this trace, or null. */
        public String refused() { return refused; }

        public int count() { synchronized (events) { return events.size(); } }

        /**
         * The tripwire, run <b>before the in-memory buffer</b> and not merely before the file.
         *
         * <p>An invariant reads these events while the run is still going, and a pathless sink is
         * not private — "in memory" is a statement about latency, not about confinement.
         *
         * <p>The trace is the <em>worst</em> artifact to leave unguarded, not the least: it records
         * every local on every executed line — values <b>before</b> they reach any redaction — and
         * tracing is exactly what you switch on when debugging the request that went wrong, which
         * is the one carrying the real credential.
         */
        private String vet(String line) {
            for (Pattern p : forbid) {
                if (p.matcher(line).find()) return p.pattern();
            }
            return null;
        }

        void emit(Map<String, Object> ev) {
            if (refused != null) return; // tracing is disabled permanently after a hit
            String line;
            try {
                line = Json.write(ev);
            } catch (Throwable t) {
                // Something that cannot be inspected cannot be cleared. Refuse rather than wave
                // it through unread.
                line = null;
            }
            if (line == null) return;
            if (!forbid.isEmpty()) {
                String hit = vet(line);
                if (hit != null) { refuse(hit); return; }
            }
            synchronized (events) { events.add(ev); }
            if (out != null) {
                try {
                    out.write(line);
                    out.write("\n");
                    out.flush();
                } catch (IOException ignored) {
                }
            }
        }

        /**
         * A hit: destroy what was written, record the refusal beside it, and stop tracing for good.
         *
         * <p>The refusal goes to a SIDECAR FILE because the guard may be running in a child process
         * whose exit code nobody checks — a traced test run can trip this and still exit 0, so a
         * guard that only shouted into the child's stderr would be a guard nobody enforces. The
         * parent reads the sidecar before the trace, and it wins.
         */
        private void refuse(String pattern) {
            refused = pattern;
            synchronized (events) { events.clear(); }
            if (out != null) {
                try { out.close(); } catch (IOException ignored) { }
                out = null;
            }
            if (path != null) {
                try { Files.deleteIfExists(path); } catch (IOException ignored) { }
                try {
                    Files.writeString(Paths.get(refusalPath(path.toString())), pattern, StandardCharsets.UTF_8);
                } catch (IOException ignored) { }
            }
            System.err.println("flight-recorder: tracing refused — a traced value matched the forbidden "
                    + "pattern (\"" + pattern + "\"); the trace was destroyed and tracing is now off");
        }

        /** The events recorded since {@code from}, as a queryable trace. */
        public Trace snapshot(int from) {
            synchronized (events) {
                int start = Math.min(Math.max(from, 0), events.size());
                List<Map<String, Object>> out2 = new ArrayList<>();
                for (int i = start; i < events.size(); i++) {
                    Map<String, Object> e = events.get(i);
                    if (!"H".equals(e.get("e"))) out2.add(e);
                }
                return new Trace(out2);
            }
        }

        public Trace snapshot() { return snapshot(0); }

        public void close() {
            if (out != null) {
                try { out.close(); } catch (IOException ignored) { }
                out = null;
            }
        }
    }
}
