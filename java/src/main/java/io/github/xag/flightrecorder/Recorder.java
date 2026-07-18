package io.github.xag.flightrecorder;

import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.security.SecureRandom;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.Callable;
import java.util.function.Function;
import java.util.function.UnaryOperator;
import java.util.regex.Pattern;

/**
 * Records what the outside world told your code — every database answer, HTTP response, clock read
 * and random draw — as one JSONL tape per session, conformant to {@code spec/tape-v1.md}, and
 * replays that tape against the real code so a past run reproduces exactly.
 *
 * <p>The cardinal rule is <b>INSTRUMENT, NEVER DUPLICATE</b>: nothing here evaluates a query,
 * computes a date, or knows what any value means. It records the questions your code asked the
 * world and the answers it got; on replay it feeds those answers back and checks the questions
 * still match.
 *
 * <p>Java can neither monkeypatch a module's functions the way the Python recorder shims
 * {@code datetime} and {@code random}, nor rely on an ambient that follows a value across an
 * arbitrary executor the way .NET's {@code AsyncLocal} follows an {@code await}. So instrumentation
 * is explicit: every boundary read goes through one of this class's static primitives, and the
 * active call rides on a {@link ThreadLocal}.
 *
 * <p><b>Crossing a thread boundary.</b> The ambient does not follow work you hand to an executor.
 * If a recorded call fans out, wrap the work with {@link #propagate(Runnable)} or
 * {@link #propagate(Callable)} — otherwise the events raised on that thread are silently dropped,
 * which is the failure mode this note exists to prevent. Child threads created inline inherit it
 * without help.
 */
public final class Recorder implements AutoCloseable {

    static final int FORMAT_VERSION = 1;

    /** A monotonic origin for {@link #perf}. Arbitrary, per-process — exactly what a monotonic
     *  clock is. */
    private static final long PROCESS_START = System.nanoTime();

    private static final DateTimeFormatter STAMP = DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss");
    private static final Random RNG = new Random();
    private static final SecureRandom ENTROPY = new SecureRandom();

    // ------------------------------------------------------------- the ambient

    /**
     * What a boundary primitive consults. Exactly one field is set: {@code call} while recording,
     * {@code feed} while replaying.
     *
     * <p>Inheritable so a thread started inside a recorded call still records. It does NOT reach a
     * pooled executor thread — see {@link #propagate}.
     */
    static final class Ambient {
        CallBuffer call;
        Replay.Feed feed;
        Ambient(CallBuffer c, Replay.Feed f) { call = c; feed = f; }
    }

    static final InheritableThreadLocal<Ambient> AMBIENT = new InheritableThreadLocal<>();

    static Ambient ambient() { return AMBIENT.get(); }

    /** Wraps a task so it carries the current recording/replay ambient onto whatever thread runs
     *  it. Without this, an executor thread has no ambient and its boundary reads go unrecorded. */
    public static Runnable propagate(Runnable task) {
        Ambient a = AMBIENT.get();
        return () -> {
            Ambient prior = AMBIENT.get();
            AMBIENT.set(a);
            try { task.run(); } finally { AMBIENT.set(prior); }
        };
    }

    /** @see #propagate(Runnable) */
    public static <T> Callable<T> propagate(Callable<T> task) {
        Ambient a = AMBIENT.get();
        return () -> {
            Ambient prior = AMBIENT.get();
            AMBIENT.set(a);
            try { return task.call(); } finally { AMBIENT.set(prior); }
        };
    }

    /** A recorded body. Allowed to throw anything — the recorder records the failure and lets it
     *  through untouched. */
    @FunctionalInterface
    public interface Body<T> { T run() throws Exception; }

    /** A recorded body with no result. */
    @FunctionalInterface
    public interface Act { void run() throws Exception; }

    /** The event buffer for one in-flight call. */
    static final class CallBuffer {
        final Recorder rec;
        final List<Map<String, Object>> events = new ArrayList<>();
        int sid;

        CallBuffer(Recorder rec) { this.rec = rec; }

        int nextSid() { return ++sid; }

        /**
         * Scrubs, runs the forbid tripwire, and appends.
         *
         * <p>The guard runs BEFORE the append, not before the file write, because the buffer is
         * what becomes the call record — and because an invariant can read these events while the
         * run is still going. "In memory" is a statement about latency, not about confinement.
         */
        void emit(Map<String, Object> ev) {
            Map<String, Object> scrubbed = rec.scrubEvent(ev);
            String line = Json.write(scrubbed);
            String hit = rec.forbiddenHit(line);
            if (hit != null) {
                throw new Errors.ForbiddenValue(hit, "a recorded " + scrubbed.get("k") + " event");
            }
            events.add(scrubbed);
        }
    }

    // --------------------------------------------------------------- the state

    private final Path dir;
    private final Map<String, Object> header;
    private final Map<String, Function<Object, Object>> redact;
    private final UnaryOperator<String> scrub;
    private final List<Pattern> forbid = new ArrayList<>();
    private final java.util.function.BiPredicate<String, Map<String, Object>> gate;
    private final Sink sink;
    final Boundary boundary;

    private final Object lock = new Object();
    private Writer file;
    private Path path;
    private int seq;
    private StringBuilder mirror; // an in-memory copy of the file, kept only to feed a sink

    /**
     * Prepares a recorder writing into {@code dir}.
     *
     * <p>It does <b>not</b> open the session file. The first call the gate admits does — so a gate
     * that never fires leaves nothing behind, and a process that records nothing is
     * indistinguishable from one with the recorder uninstalled.
     *
     * @throws IllegalArgumentException on a bad forbid pattern (caught here, at declaration time,
     *         rather than at the moment it would have fired)
     * @throws IOException if the directory cannot be created
     */
    public static Recorder open(String dir, Boundary b) throws IOException {
        return new Recorder(Paths.get(dir), b);
    }

    private Recorder(Path dir, Boundary b) throws IOException {
        Files.createDirectories(dir);
        this.dir = dir;
        this.boundary = b;
        this.redact = b.redact;
        this.scrub = b.scrub;
        this.gate = b.enabled;
        this.sink = b.sink;
        for (String p : b.forbid) forbid.add(Pattern.compile(p));

        Map<String, Object> constants = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : b.constants.entrySet()) {
            constants.put(e.getKey(), Serial.toJsonable(e.getValue()));
        }
        Map<String, Object> h = new LinkedHashMap<>();
        h.put("ev", "session");
        h.put("version", FORMAT_VERSION);
        h.put("java", System.getProperty("java.version"));
        h.put("constants", constants);
        for (Map.Entry<String, Object> e : b.headerExtras.entrySet()) {
            h.put(e.getKey(), Serial.toJsonable(e.getValue()));
        }
        this.header = h;
    }

    /** The session file's path, or null if no call has been recorded yet. */
    public String path() {
        synchronized (lock) { return path == null ? null : path.toString(); }
    }

    @Override
    public void close() throws IOException {
        synchronized (lock) {
            if (file != null) { file.close(); file = null; }
        }
    }

    /** Opens the session file and writes the header, once. Caller holds {@link #lock}. */
    private void ensureOpen() throws IOException {
        if (file != null) return;
        header.put("started", Serial.iso(OffsetDateTime.now()));

        // The header is vetted BEFORE the file exists. A hit here must leave no session file at
        // all: creating the file and then refusing to write into it would leave an empty tape on
        // disk, which reads as "a recording that captured nothing" rather than as "a recording
        // that was refused" — and the difference matters to whoever finds it later.
        String line = Json.write(header);
        String hit = forbiddenHit(line);
        if (hit != null) throw new Errors.ForbiddenValue(hit, "the session record");

        String stamp = LocalDateTime.now().format(STAMP);
        long pid = ProcessHandle.current().pid();
        // The nonce is not decoration: two processes starting in the same second would otherwise
        // produce the same file name, and a name-keyed sink would have one silently overwrite the
        // other's tape. .NET adds one for the same reason.
        String nonce = Long.toHexString(ENTROPY.nextLong() & 0xFFFFFFFFL);
        path = dir.resolve("flight-" + stamp + "-" + pid + "-" + nonce + ".jsonl");
        file = Files.newBufferedWriter(path, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE, StandardOpenOption.WRITE, StandardOpenOption.APPEND);
        file.write(line);
        file.write("\n");
        file.flush();
        if (sink != null) {
            if (mirror == null) mirror = new StringBuilder();
            mirror.append(line).append('\n');
            publish();
        }
    }

    private String forbiddenHit(String line) {
        for (Pattern p : forbid) {
            if (p.matcher(line).find()) return p.pattern();
        }
        return null;
    }

    /**
     * Renders, guards, writes, mirrors, publishes — in that order, so nothing reaches a file or a
     * sink unvetted. Caller holds {@link #lock}. Nothing is written on a hit.
     */
    private void writeLocked(Map<String, Object> obj, String what) throws IOException {
        String line = Json.write(obj);
        String hit = forbiddenHit(line);
        if (hit != null) throw new Errors.ForbiddenValue(hit, what);
        file.write(line);
        file.write("\n");
        file.flush();
        if (sink != null) {
            if (mirror == null) mirror = new StringBuilder();
            mirror.append(line).append('\n');
            publish();
        }
    }

    /** Hands the whole session to the sink, best-effort: a throw is swallowed, because recording
     *  must never be the reason a call fails. Caller holds {@link #lock}. */
    private void publish() {
        try {
            sink.publish(path.getFileName().toString(), mirror.toString());
        } catch (Throwable ignored) {
            // Deliberately swallowed. See Sink.
        }
    }

    private boolean gateAdmits(String fn, Map<String, Object> kwargs) {
        if (gate == null) return true;
        try {
            return gate.test(fn, kwargs);
        } catch (Throwable t) {
            return false; // a gate that throws can never break the call it was asked about
        }
    }

    private Map<String, Object> scrubEvent(Map<String, Object> ev) {
        if ((redact == null || redact.isEmpty()) && scrub == null) return ev;
        Map<String, Object> out = new LinkedHashMap<>(ev);
        // The union of what Go and .NET sweep. `err` carries a message the app built, which is a
        // classic place for a secret to be interpolated; `result`/`res` and `data` are the obvious
        // ones. Missing one of these is how a redacted tape leaks anyway.
        for (String key : new String[]{"args", "kwargs", "res", "err", "result", "data"}) {
            if (out.containsKey(key)) out.put(key, Serial.redact(out.get(key), redact, scrub));
        }
        return out;
    }

    // ---------------------------------------------------------------- the call

    /**
     * Records one top-level tool call: runs {@code body} with an active call on the ambient,
     * buffers every event it produces, and writes the call line when it returns.
     *
     * <p>An exception from {@code body} is recorded and then rethrown <b>exactly as it was</b> —
     * not wrapped. Wrapping would change what a caller's {@code catch} sees, and the recorder is
     * not allowed to change behaviour.
     */
    public <T> T call(String fn, Map<String, Object> kwargs, Body<T> body) {
        Map<String, Object> kw = kwargs == null ? Map.of() : kwargs;
        if (!gateAdmits(fn, kw)) {
            // The gate declined: run for real, record nothing, open no file.
            try { return body.run(); } catch (Exception e) { throw sneak(e); }
        }

        CallBuffer c = new CallBuffer(this);
        Ambient prior = AMBIENT.get();
        AMBIENT.set(new Ambient(c, null));
        long t0 = System.nanoTime();

        T result = null;
        Throwable failure = null;
        try {
            result = body.run();
        } catch (Throwable t) {
            failure = t;
        } finally {
            AMBIENT.set(prior);
        }

        double ms = (System.nanoTime() - t0) / 1_000_000.0;
        try {
            writeCall(fn, kw, c.events, result, failure, ms);
        } catch (Errors.ForbiddenValue fv) {
            // The one recorder failure that is never swallowed.
            throw fv;
        } catch (IOException io) {
            if (failure == null) throw new RuntimeException("flight-recorder: could not write the tape", io);
        }

        if (failure != null) throw sneak(failure);
        return result;
    }

    /** @see #call(String, Map, Body) */
    public void call(String fn, Map<String, Object> kwargs, Act body) {
        call(fn, kwargs, () -> { body.run(); return null; });
    }

    private void writeCall(String fn, Map<String, Object> kwargs, List<Map<String, Object>> events,
                           Object result, Throwable callErr, double ms) throws IOException {
        synchronized (lock) {
            ensureOpen(); // opens the file and writes the header on the first admitted call
            int next = seq + 1;
            Map<String, Object> obj = new LinkedHashMap<>();
            obj.put("ev", "call");
            obj.put("seq", next);
            obj.put("fn", fn);
            obj.put("kwargs", Serial.redact(Serial.toJsonable(kwargs), redact, scrub));
            obj.put("events", new ArrayList<Object>(events));
            obj.put("result", Serial.redact(Serial.toJsonable(result), redact, scrub));
            obj.put("error", callErr == null ? null : render(callErr));
            obj.put("ts", Serial.iso(OffsetDateTime.now()));
            obj.put("ms", round2(ms));
            writeLocked(obj, "the call record for \"" + fn + "\"");
            // Only a written call consumes a seq, so the tape stays 1-based and contiguous even
            // when a line was refused.
            seq = next;
        }
    }

    static String render(Throwable t) {
        String m = t.getMessage();
        return m == null || m.isEmpty() ? t.getClass().getSimpleName() : m;
    }

    static double round2(double d) { return Math.round(d * 100.0) / 100.0; }

    /**
     * Rethrows a checked exception without declaring it.
     *
     * <p>This looks like a trick, and it is, but the alternative is worse: wrapping the app's
     * exception in a {@code RuntimeException} would change what its own {@code catch} clauses see,
     * and the recorder's whole promise is that a recorded run behaves like an unrecorded one.
     */
    @SuppressWarnings("unchecked")
    static <E extends Throwable> RuntimeException sneak(Throwable t) throws E { throw (E) t; }

    // ------------------------------------------------- the boundary primitives

    /**
     * Records and returns the wall clock, naive — the shape most Java code reads.
     *
     * <p>The naive/aware distinction is preserved rather than normalised. It is part of the value:
     * a {@link LocalDateTime} and an {@link OffsetDateTime} compare and format differently, so a
     * replay that handed back the other kind would change behaviour.
     */
    public static LocalDateTime now() {
        Ambient a = ambient();
        if (a == null) return LocalDateTime.now();
        if (a.feed != null) return a.feed.now();
        LocalDateTime v = LocalDateTime.now();
        a.call.emit(ev("now", "v", Serial.isoNaive(v)));
        return v;
    }

    /** Records and returns the wall clock, timezone-aware. */
    public static OffsetDateTime nowOffset() {
        Ambient a = ambient();
        if (a == null) return OffsetDateTime.now();
        if (a.feed != null) return a.feed.nowOffset();
        OffsetDateTime v = OffsetDateTime.now();
        a.call.emit(ev("now", "v", Serial.iso(v)));
        return v;
    }

    /** Records and returns a monotonic clock reading in milliseconds (arbitrary origin). */
    public static double perf() {
        Ambient a = ambient();
        double live = (System.nanoTime() - PROCESS_START) / 1_000_000.0;
        if (a == null) return live;
        if (a.feed != null) return a.feed.perf();
        a.call.emit(ev("perf", "v", live));
        return live;
    }

    /**
     * Records a module-level effect: the {@code (args → result | exception)} that IS the external
     * world. While replaying it serves the recorded result (or rethrows the recorded error) and
     * asserts the args match — a different question here is a path divergence.
     */
    public static <T> T effect(String name, List<Object> args, Body<T> real) {
        Ambient a = ambient();
        if (a != null && a.feed != null) {
            @SuppressWarnings("unchecked")
            T served = (T) a.feed.answerEffect(name, jsonableList(args));
            return served;
        }
        T res;
        try {
            res = real.run();
        } catch (Throwable t) {
            if (a != null) {
                Map<String, Object> e = new LinkedHashMap<>();
                e.put("k", "fx");
                e.put("fn", name);
                e.put("args", jsonableList(args));
                e.put("kwargs", new LinkedHashMap<String, Object>()); // JS has no kwargs; the spec fixes this at {}
                Map<String, Object> err = new LinkedHashMap<>();
                err.put("type", t.getClass().getSimpleName());
                err.put("repr", truncate(render(t), 300));
                // A structured error can carry its own constructive values, so an error reviver can
                // rebuild it faithfully on replay rather than guessing from a rendering.
                err.put("args", t instanceof FlightError fe ? jsonableList(fe.errorArgs())
                                                            : jsonableList(List.of(render(t))));
                e.put("err", err);
                a.call.emit(e);
            }
            throw sneak(t);
        }
        if (a != null) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("k", "fx");
            e.put("fn", name);
            e.put("args", jsonableList(args));
            e.put("kwargs", new LinkedHashMap<String, Object>());
            e.put("res", Serial.toJsonable(res));
            a.call.emit(e);
        }
        return res;
    }

    /**
     * An exception that carries the values it was built from, so replay can rebuild it with its
     * real type. Mirrors Python's {@code err.args} and Go's {@code interface{ Args() []any }}.
     */
    public interface FlightError {
        List<Object> errorArgs();
    }

    /**
     * Wraps what the app HOLDS: a transparent proxy over an interface that records the named
     * methods and passes everything else straight through.
     *
     * <p><b>This is not a mock.</b> Under record it calls the real object and writes down what came
     * back; under replay it serves the recorded answer without calling anything. A method not named
     * here is invisible to the recorder — it is forwarded and nothing is written.
     *
     * <p>Java could in principle patch a class the way Python patches a module, but only through an
     * instrumentation agent, which is a launch-flag dependency no library should impose. So the
     * boundary is the <em>object</em>, as it is in Node and .NET.
     */
    public static <T> T wrap(Class<T> iface, T target, String... methods) {
        return wrapAs(iface, target, null, methods);
    }

    /** @param prefix qualifies the recorded effect names, so {@code read} is written down as
     *                {@code kv.read} and two wrapped clients never collide on the tape. */
    @SuppressWarnings("unchecked")
    public static <T> T wrapAs(Class<T> iface, T target, String prefix, String... methods) {
        java.util.Set<String> recorded = new java.util.HashSet<>(Arrays.asList(methods));
        return (T) java.lang.reflect.Proxy.newProxyInstance(
                iface.getClassLoader(), new Class<?>[]{iface}, (proxy, method, args) -> {
            Object[] a = args == null ? new Object[0] : args;
            if (!recorded.contains(method.getName())) {
                return method.invoke(target, a); // untouched and unrecorded
            }
            String name = (prefix == null || prefix.isEmpty() ? "" : prefix + ".") + method.getName();
            Object answer = effect(name, Arrays.asList(a), () -> {
                try {
                    return method.invoke(target, a);
                } catch (java.lang.reflect.InvocationTargetException e) {
                    throw sneak(e.getCause() == null ? e : e.getCause());
                }
            });
            // Coerce into what the method PROMISED. Without this, replay hands a Map back to code
            // that declared a return type, and the app fails on a cast the recorder caused.
            return Serial.coerce(answer, method.getReturnType());
        });
    }

    /**
     * Draws {@code k} distinct positions from {@code [0, n)} and records the POSITIONS, not the
     * members — which is what lets replay pick the same members from a population a mutation has
     * since changed.
     */
    public static List<Integer> sampleIndices(int n, int k) {
        if (k > n) k = n;
        Ambient a = ambient();
        if (a != null && a.feed != null) return a.feed.sample(n, k);
        List<Integer> all = new ArrayList<>(n);
        for (int i = 0; i < n; i++) all.add(i);
        Collections.shuffle(all, RNG);
        List<Integer> idx = new ArrayList<>(all.subList(0, k));
        if (a != null) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("k", "rand"); e.put("m", "sample"); e.put("n", n); e.put("kk", k); e.put("idx", idx);
            a.call.emit(e);
        }
        return idx;
    }

    /** Draws {@code n} bytes of real entropy and records them as hex. */
    public static byte[] randBytes(int n) {
        Ambient a = ambient();
        if (a != null && a.feed != null) return a.feed.bytes(n);
        byte[] b = new byte[n];
        ENTROPY.nextBytes(b);
        if (a != null) {
            StringBuilder hex = new StringBuilder(n * 2);
            for (byte x : b) hex.append(String.format("%02x", x));
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("k", "rand"); e.put("m", "bytes"); e.put("n", n); e.put("hex", hex.toString());
            a.call.emit(e);
        }
        return b;
    }

    /** Draws and records a uniform double in {@code [0, 1)}. */
    public static double randFloat() {
        Ambient a = ambient();
        if (a != null && a.feed != null) return a.feed.randFloat();
        double v = RNG.nextDouble();
        if (a != null) a.call.emit(ev2("rand", "m", "float", "v", v));
        return v;
    }

    /** Draws and records a uniform int in {@code [0, n)}. */
    public static int randInt(int n) {
        Ambient a = ambient();
        if (a != null && a.feed != null) return a.feed.randInt();
        int v = RNG.nextInt(n);
        if (a != null) a.call.emit(ev2("rand", "m", "int", "v", v));
        return v;
    }

    /** Records and returns a terminal read that yielded several snapshots. While replaying it
     *  serves the recorded snapshots and does not run. */
    public static List<Snapshot> query(String op, String sig, Body<List<Snapshot>> real) {
        Ambient a = ambient();
        if (a != null && a.feed != null) return a.feed.answerQuery(op, sig);
        List<Snapshot> res;
        try { res = real.run(); } catch (Exception e) { throw sneak(e); }
        if (a != null) {
            List<Object> arr = new ArrayList<>();
            for (Snapshot s : res) arr.add(s.jsonable());
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("k", "db"); e.put("op", op); e.put("sig", sig); e.put("res", arr);
            a.call.emit(e);
        }
        return res;
    }

    /** Records and returns a terminal read that yielded a single snapshot. */
    public static Snapshot queryOne(String op, String sig, Body<Snapshot> real) {
        Ambient a = ambient();
        if (a != null && a.feed != null) return a.feed.answerQueryOne(op, sig);
        Snapshot res;
        try { res = real.run(); } catch (Exception e) { throw sneak(e); }
        if (a != null) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("k", "db"); e.put("op", op); e.put("sig", sig); e.put("res", res.jsonable());
            a.call.emit(e);
        }
        return res;
    }

    /**
     * Records a terminal write — the questions (args), not answers.
     *
     * <p>While replaying the write is <b>not executed</b>: it is compared against the recording,
     * and a mismatch is a write divergence. Replaying a run must not charge the card twice.
     */
    public static void exec(String op, String sig, List<Object> args, Act real) {
        Ambient a = ambient();
        if (a != null && a.feed != null) {
            a.feed.expectWrite(op, sig, jsonableList(args));
            return;
        }
        try { real.run(); } catch (Exception e) { throw sneak(e); }
        if (a != null) {
            Map<String, Object> e = new LinkedHashMap<>();
            e.put("k", "db"); e.put("op", op); e.put("sig", sig); e.put("args", jsonableList(args));
            a.call.emit(e);
        }
    }

    // ------------------------------------------------------- semantic spans

    /** Records that something meaningful happened at a point, in the app's own vocabulary. */
    public static void note(String name, Map<String, Object> data) {
        Ambient a = ambient();
        if (a == null) return;
        if (a.feed != null) { a.feed.note(name); return; }
        Map<String, Object> e = new LinkedHashMap<>();
        e.put("k", "sem"); e.put("name", name); e.put("phase", "point"); e.put("sid", a.call.nextSid());
        // An empty data map is omitted rather than written as {} — the absence of detail is not
        // itself a detail.
        if (data != null && !data.isEmpty()) e.put("data", jsonableMap(data));
        a.call.emit(e);
    }

    public static void note(String name) { note(name, null); }

    /**
     * Records that a stretch of execution constituted a domain act, and encloses the raw events it
     * produced.
     *
     * <p>Well-nested by construction, because a span wraps the body it encloses. If the body
     * throws, the {@code end} <b>still lands</b> with {@code outcome: "error"} and the exception
     * propagates untouched — a span that vanished on the error path would make a failed run look
     * like a run that told a shorter story, which is a different and much more confusing finding.
     */
    public static <T> T span(String name, Map<String, Object> data, Body<T> body) {
        Ambient a = ambient();
        if (a == null) {
            try { return body.run(); } catch (Exception e) { throw sneak(e); }
        }
        if (a.feed != null) return a.feed.span(name, body);

        CallBuffer c = a.call;
        int sid = c.nextSid();
        Map<String, Object> begin = new LinkedHashMap<>();
        begin.put("k", "sem"); begin.put("name", name); begin.put("phase", "begin"); begin.put("sid", sid);
        if (data != null && !data.isEmpty()) begin.put("data", jsonableMap(data));
        c.emit(begin);

        boolean ok = false;
        try {
            T out = body.run();
            ok = true;
            return out;
        } catch (Throwable t) {
            throw sneak(t);
        } finally {
            Map<String, Object> end = new LinkedHashMap<>();
            end.put("k", "sem"); end.put("name", name); end.put("phase", "end"); end.put("sid", sid);
            end.put("outcome", ok ? "ok" : "error");
            c.emit(end);
        }
    }

    public static <T> T span(String name, Body<T> body) { return span(name, null, body); }

    public static void span(String name, Map<String, Object> data, Act body) {
        span(name, data, () -> { body.run(); return null; });
    }

    public static void span(String name, Act body) { span(name, null, body); }

    // --------------------------------------------------------------- helpers

    private static Map<String, Object> ev(String kind, String k1, Object v1) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("k", kind); m.put(k1, v1);
        return m;
    }

    private static Map<String, Object> ev2(String kind, String k1, Object v1, String k2, Object v2) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("k", kind); m.put(k1, v1); m.put(k2, v2);
        return m;
    }

    static List<Object> jsonableList(List<Object> args) {
        List<Object> out = new ArrayList<>();
        if (args != null) for (Object a : args) out.add(Serial.toJsonable(a));
        return out;
    }

    static Map<String, Object> jsonableMap(Map<String, Object> m) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (m != null) for (Map.Entry<String, Object> e : m.entrySet()) {
            out.put(e.getKey(), Serial.toJsonable(e.getValue()));
        }
        return out;
    }

    static String truncate(String s, int limit) {
        if (s == null) return null;
        return s.length() <= limit ? s : s.substring(0, limit - 1) + "…";
    }

    /** Convenience for building a kwargs map inline. */
    public static Map<String, Object> kwargs(Object... pairs) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < pairs.length; i += 2) m.put(String.valueOf(pairs[i]), pairs[i + 1]);
        return m;
    }
}
