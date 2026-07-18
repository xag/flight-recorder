package io.github.xag.flightrecorder;

import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;
import java.util.regex.Pattern;

/**
 * Re-execute a recorded call with the recording as its world.
 *
 * <p>Recorded events are fed back in their original order; the replayed code must ask the boundary
 * the same questions in the same order — anything else is a divergence naming the first difference
 * — and gets handed the recorded answers. <b>Writes are compared, never executed</b>: replaying a
 * run must not charge the card twice.
 *
 * <p>The verdict carries three <em>independent</em> signals, and conflating them is how a replay
 * session goes wrong:
 * <ul>
 *   <li>a boundary <b>divergence</b> says the recording is stale;
 *   <li>a result/error <b>mismatch</b> says the code produces something else;
 *   <li>a <b>semantic divergence</b> says the code's own account of what it was doing changed —
 *       which may be a refactor as easily as a bug, so it is reported and does not gate.
 * </ul>
 */
public final class Replay {

    private Replay() {}

    /** Maps a recorded call (its name and revived kwargs) to the code to re-execute. */
    @FunctionalInterface
    public interface Resolver {
        Recorder.Body<Object> resolve(String fn, Map<String, Object> kwargs);
    }

    /** One semantic claim: a name and a phase. Payloads are a reader's business and are
     *  deliberately not compared. */
    public record SemPair(String name, String phase) {
        @Override public String toString() { return "\"" + name + "\" " + phase; }
    }

    /**
     * Under mutation a query's CONTENT changes — it flows from mutated data — but its SHAPE does
     * not. So probe matching compares shapes: {@code collection("u").where("x",">",0)} becomes
     * {@code collection.where}.
     */
    private static final Pattern CHAIN_ARGS = Pattern.compile("\\([^()]*\\)");

    static String skeleton(String sig) {
        return sig == null ? "" : CHAIN_ARGS.matcher(sig).replaceAll("");
    }

    // ------------------------------------------------------------------ feed

    /**
     * The recording as the world: events in order, popped by kind and shape.
     *
     * <p>In probe mode (a mutated recording) it is only an answering service — questions match by
     * kind and shape, order-monotonic, skipping recorded events the mutated execution no longer
     * asks. A mutation legitimately changes which questions get asked, but the tape holds only the
     * answers it holds.
     */
    static final class Feed {
        final List<Map<String, Object>> events;
        final boolean probe;
        final Boundary boundary;
        int pos;
        int consumed;
        int skipped;
        final List<String> writeDivs = new ArrayList<>();
        final List<Map<String, Object>> writes = new ArrayList<>();
        final List<SemPair> sems = new ArrayList<>();

        Feed(List<Map<String, Object>> events, boolean probe, Boundary boundary) {
            this.events = events;
            this.probe = probe;
            this.boundary = boundary;
        }

        /**
         * Steps past semantic events.
         *
         * <p>They are the app's testimony, never evidence, and are never fed back to anything —
         * so every pop skips them first, and the replay skips them once more after the body
         * returns (see {@link #run}).
         */
        void skipSems() {
            while (pos < events.size() && "sem".equals(events.get(pos).get("k"))) {
                pos++;
                consumed++;
            }
        }

        private boolean matches(Map<String, Object> ev, String kind, String sig, String op, String fn) {
            if (!kind.equals(ev.get("k"))) return false;
            if ("db".equals(kind) && sig != null) {
                if (!Objects.equals(Json.asString(ev.get("op")), op)) return false;
                if (probe) return skeleton(Json.asString(ev.get("sig"))).equals(skeleton(sig));
                return Objects.equals(Json.asString(ev.get("sig")), sig);
            }
            if ("fx".equals(kind) && fn != null) {
                return Objects.equals(Json.asString(ev.get("fn")), fn);
            }
            return true;
        }

        private static String want(String kind, String sig, String op, String fn) {
            if (sig != null) return kind + " " + op + " " + sig;
            if (fn != null) return kind + " " + fn;
            return kind;
        }

        Map<String, Object> popExpect(String kind, String sig, String op, String fn) {
            skipSems();
            if (probe) {
                for (int j = pos; j < events.size(); j++) {
                    Map<String, Object> ev = events.get(j);
                    if ("sem".equals(ev.get("k"))) continue; // not an answer, nor evidence of a changed path
                    if (matches(ev, kind, sig, op, fn)) {
                        for (int k = pos; k < j; k++) {
                            if (!"sem".equals(events.get(k).get("k"))) skipped++;
                        }
                        consumed += (j - pos) + 1;
                        pos = j + 1;
                        return ev;
                    }
                }
                throw new Errors.ProbeUnanswerable(
                        "the replayed code asked for \"" + want(kind, sig, op, fn) + "\" but the recording "
                        + "holds no further such event — the mutation sent execution down a path this "
                        + "recording cannot answer");
            }
            if (pos >= events.size()) {
                throw new Errors.ReplayDivergence(
                        "replay asked for a \"" + kind + "\" event at position " + pos + " but the recording "
                        + "is exhausted — the replayed code takes a longer path than the recorded one");
            }
            Map<String, Object> ev = events.get(pos);
            if (!matches(ev, kind, sig, op, fn)) {
                String got = Json.asString(ev.get("k"));
                if ("db".equals(got)) got = "db " + ev.get("op") + " " + ev.get("sig");
                else if ("fx".equals(got)) got = "fx " + ev.get("fn");
                throw new Errors.ReplayDivergence(
                        "boundary divergence at event " + pos + ":\n  recorded: " + got
                        + "\n  replayed: " + want(kind, sig, op, fn));
            }
            pos++;
            consumed++;
            return ev;
        }

        // ------------------------------------------------- serving the answers

        LocalDateTime now() {
            Object t = Serial.parseIso(Json.asString(popExpect("now", null, null, null).get("v")));
            if (t instanceof LocalDateTime l) return l;
            if (t instanceof OffsetDateTime o) return o.toLocalDateTime();
            return LocalDateTime.now();
        }

        OffsetDateTime nowOffset() {
            Object t = Serial.parseIso(Json.asString(popExpect("now", null, null, null).get("v")));
            if (t instanceof OffsetDateTime o) return o;
            if (t instanceof LocalDateTime l) return l.atZone(ZoneId.systemDefault()).toOffsetDateTime();
            return OffsetDateTime.now();
        }

        double perf() { return toDouble(popExpect("perf", null, null, null).get("v")); }

        private Map<String, Object> expectRand(String method) {
            Map<String, Object> ev = popExpect("rand", null, null, null);
            String m = Json.asString(ev.get("m"));
            if (!method.equals(m)) {
                throw new Errors.ReplayDivergence(
                        "random divergence: replayed code drew \"" + method + "\" but the recording holds a \""
                        + m + "\" draw here");
            }
            return ev;
        }

        List<Integer> sample(int n, int k) {
            Map<String, Object> ev = expectRand("sample");
            List<Object> idx = Json.asList(ev.get("idx"));
            List<Integer> out = new ArrayList<>();
            if (idx != null) for (Object x : idx) {
                int i = (int) toDouble(x);
                // A mutation may have shrunk the population under a recorded index. That is the
                // tape being incompletely edited, not the code misbehaving.
                if (i >= n) {
                    throw new Errors.ProbeUnanswerable(
                            "the recording drew index " + i + " from a population of " + n + " — the mutation "
                            + "shrank the population below an index this recording depends on");
                }
                out.add(i);
            }
            return out;
        }

        byte[] bytes(int n) {
            String hex = Json.asString(expectRand("bytes").get("hex"));
            if (hex == null) return new byte[0];
            byte[] out = new byte[hex.length() / 2];
            for (int i = 0; i < out.length; i++) {
                out[i] = (byte) Integer.parseInt(hex.substring(i * 2, i * 2 + 2), 16);
            }
            return out;
        }

        double randFloat() { return toDouble(expectRand("float").get("v")); }

        int randInt() { return (int) toDouble(expectRand("int").get("v")); }

        Snapshot answerQueryOne(String op, String sig) {
            return Snapshot.fromJsonable(popExpect("db", sig, op, null).get("res"));
        }

        List<Snapshot> answerQuery(String op, String sig) {
            Object res = popExpect("db", sig, op, null).get("res");
            List<Object> arr = Json.asList(res);
            List<Snapshot> out = new ArrayList<>();
            if (arr != null) for (Object s : arr) out.add(Snapshot.fromJsonable(s));
            else if (res != null) out.add(Snapshot.fromJsonable(res));
            return out;
        }

        void expectWrite(String op, String sig, List<Object> argsJsonable) {
            // Every write the replayed code performs is captured for invariants ("never writes when
            // the corpus is empty"); the write itself is compared, never executed.
            Map<String, Object> w = new LinkedHashMap<>();
            w.put("op", op); w.put("sig", sig); w.put("args", argsJsonable);
            writes.add(w);
            Map<String, Object> ev = popExpect("db", sig, op, null);
            if (!probe && !Json.equal(ev.get("args"), redacted(argsJsonable))) {
                writeDivs.add(op + " on " + sig + ":\n    recorded: " + brief(ev.get("args"))
                        + "\n    replayed: " + brief(redacted(argsJsonable)));
            }
        }

        /**
         * Re-applies the boundary's redaction to a value the replayed code just produced, before
         * comparing it against the tape.
         *
         * <p>The tape holds MASKED values; the live code produces raw ones. Comparing the two
         * directly reports a divergence on every secret the code legitimately still handles — a
         * phantom finding that says "the code changed" when nothing changed but the masking.
         *
         * <p>This is precisely why both redaction layers must be idempotent, and why
         * {@link Boundary#scrubbing(String, String)} refuses a mask that matches its own pattern:
         * the value being compared here has been through the masker once on the record side and
         * once more on this one.
         */
        private Object redacted(Object jsonable) {
            if (boundary == null) return jsonable;
            return Serial.redact(jsonable, boundary.redact, boundary.scrub);
        }

        Object answerEffect(String name, List<Object> argsJsonable) {
            Map<String, Object> ev = popExpect("fx", null, null, name);
            // Probe replay never compares args: a mutated upstream answer legitimately changes every
            // downstream question. The effect name and event order still gate.
            if (!probe && !Json.equal(ev.get("args"), redacted(argsJsonable))) {
                throw new Errors.ReplayDivergence(
                        "effect " + name + " called with different arguments than recorded:\n  recorded: "
                        + brief(ev.get("args")) + "\n  replayed: " + brief(redacted(argsJsonable)));
            }
            Map<String, Object> err = Json.asMap(ev.get("err"));
            if (err != null) throw revive(err);
            return Serial.fromJsonable(ev.get("res"));
        }

        /**
         * Rebuilds a recorded error with its real type when the boundary declared a reviver.
         *
         * <p>Java code branches on exception type constantly — {@code catch (RateLimited e)} takes a
         * different path from {@code catch (NotFound e)} — so a replay that threw one generic
         * stand-in for every recorded error would send execution down a path the original never
         * took, and then report the difference as a divergence in the code.
         */
        private RuntimeException revive(Map<String, Object> err) {
            String type = Json.asString(err.get("type"));
            String repr = Json.asString(err.get("repr"));
            List<Object> args = Json.asList(err.get("args"));
            List<Object> revived = new ArrayList<>();
            if (args != null) for (Object a : args) revived.add(Serial.fromJsonable(a));
            if (boundary != null && type != null) {
                Function<List<Object>, RuntimeException> build = boundary.revivers.get(type);
                if (build != null) {
                    try {
                        RuntimeException e = build.apply(revived);
                        if (e != null) return e;
                    } catch (Throwable ignored) {
                        // A reviver that throws must not become the replay's verdict; fall back to
                        // the faithful stand-in.
                    }
                }
            }
            return new Errors.ReplayedEffectError(type, repr, revived);
        }

        void note(String name) { sems.add(new SemPair(name, "point")); }

        <T> T span(String name, Recorder.Body<T> body) {
            sems.add(new SemPair(name, "begin"));
            try {
                return body.run();
            } catch (Throwable t) {
                throw Recorder.sneak(t);
            } finally {
                // The end still lands whether the body returned or threw — the recorded span did,
                // and a shorter sem sequence would look like a changed account.
                sems.add(new SemPair(name, "end"));
            }
        }
    }

    // ---------------------------------------------------------------- report

    /** The verdict. */
    public static final class Report {
        public String fn;
        public boolean resultMatch;
        public boolean errorMatch;
        public String divergence;
        public int eventsConsumed;
        public int eventsTotal;
        /** Probe only: recorded events the mutated path no longer asked for. */
        public int skipped;
        public List<String> writeDivs = new ArrayList<>();
        public List<SemPair> semsRecorded = new ArrayList<>();
        public List<SemPair> semsReplayed = new ArrayList<>();
        public String semDivergence;
        public Object replayedResult;
        public String replayedError;
        /** Every write the replayed code performed (op/sig/args). */
        public List<Map<String, Object>> writes = new ArrayList<>();
        public Map<String, Object> kwargs = new LinkedHashMap<>();
        public boolean probe;
        /** Probe only: the mutation redirected onto a path the tape cannot serve. */
        public String unanswerable;
        /**
         * What the replayed code BELIEVED while it ran — every local, on every executed line. The
         * one thing a tape alone can never give you.
         *
         * <p><b>Empty, never null</b>, when this process is not running instrumented — so a claim
         * about an untraced variable fails honestly instead of passing vacuously.
         */
        public Trace trace = Trace.empty();

        /**
         * A strict match: same result, same error, no boundary divergence, no write divergence, and
         * every recorded event consumed (the replayed code took neither a shorter nor a longer
         * path).
         *
         * <p>A probe replay is <b>not gated by match</b> — a mutated recording is judged by
         * invariants — so its {@code ok} asks only that the tape could answer the path the mutation
         * produced.
         */
        public boolean ok() {
            if (divergence != null || unanswerable != null) return false;
            if (probe) return true;
            return resultMatch && errorMatch && writeDivs.isEmpty() && eventsConsumed == eventsTotal;
        }

        @Override public String toString() { return format(0, this); }
    }

    /** A human-readable rendering of a verdict. */
    public static String format(int index, Report r) {
        StringBuilder b = new StringBuilder();
        b.append("call ").append(index).append(" ").append(r.fn).append(": ")
                .append(r.ok() ? "OK" : "FAILED").append('\n');
        if (r.divergence != null) b.append("  ").append(r.divergence).append('\n');
        if (r.unanswerable != null) b.append("  ").append(r.unanswerable).append('\n');
        if (!r.probe && !r.resultMatch && r.divergence == null) {
            b.append("  result differs from the recording\n");
        }
        if (!r.probe && !r.errorMatch) {
            b.append("  error differs: recorded ").append(r.replayedError).append('\n');
        }
        for (String w : r.writeDivs) b.append("  write divergence: ").append(w).append('\n');
        if (r.semDivergence != null) b.append("  ").append(r.semDivergence).append('\n');
        b.append("  events ").append(r.eventsConsumed).append('/').append(r.eventsTotal);
        if (r.probe && r.skipped > 0) b.append(", ").append(r.skipped).append(" skipped");
        return b.toString();
    }

    // ------------------------------------------------------------------- run

    /** Replays call {@code index} of the session at {@code path}. */
    public static Report replay(String path, int index, Resolver resolve) throws java.io.IOException {
        return replay(Recording.load(path), index, resolve, null, false);
    }

    /** Replays call {@code index} of a loaded (possibly mutated) recording. A call the mutation API
     *  marked a probe replays in probe mode by itself. */
    public static Report replay(Recording rec, int index, Resolver resolve, Boundary boundary, boolean probe) {
        Recording.CallView cv = rec.call(index);
        if (cv == null) {
            throw new IllegalArgumentException(
                    "call " + index + " out of range: " + rec.numCalls() + " call(s) in the session");
        }
        return replayCall(cv, resolve, boundary, probe);
    }

    /** Replays one call view. */
    public static Report replayCall(Recording.CallView cv, Resolver resolve, Boundary boundary, boolean probe) {
        Objects.requireNonNull(cv, "no such call");
        return run(cv, resolve, boundary, probe || cv.isProbe());
    }

    private static Report run(Recording.CallView cv, Resolver resolve, Boundary boundary, boolean probe) {
        List<Map<String, Object>> events = cv.events();
        Feed feed = new Feed(events, probe, boundary);

        Report report = new Report();
        report.fn = cv.fn();
        report.eventsTotal = events.size();
        report.semsRecorded = semPairs(events);
        report.probe = probe;
        report.kwargs = cv.kwargs();

        Recorder.Body<Object> body = resolve.resolve(report.fn, report.kwargs);
        if (body == null) {
            throw new IllegalArgumentException("no code resolved for \"" + report.fn + "\"");
        }

        Recorder.Ambient prior = Recorder.AMBIENT.get();
        Recorder.AMBIENT.set(new Recorder.Ambient(null, feed));
        // Mark the tracer's tape before the code runs, so the report carries the trace of THIS
        // replay and not of everything the process has done since it started.
        int mark = TraceHook.mark();
        Object result = null;
        Throwable failure = null;
        try {
            result = body.run();
        } catch (Errors.ReplayDivergence d) {
            report.divergence = d.getMessage();
        } catch (Errors.ProbeUnanswerable u) {
            report.unanswerable = u.getMessage();
        } catch (Throwable t) {
            failure = t;
        } finally {
            Recorder.AMBIENT.set(prior);
        }

        report.trace = TraceHook.live(mark);

        // Sems trailing the last boundary answer (an outermost span's end, most often) were never
        // reached by a pop; leaving them unread would report a shorter path than recorded.
        feed.skipSems();

        report.eventsConsumed = feed.consumed;
        report.skipped = feed.skipped;
        report.writeDivs = feed.writeDivs;
        report.writes = feed.writes;
        report.semsReplayed = feed.sems;
        report.semDivergence = semDivergence(report.semsRecorded, report.semsReplayed);
        report.replayedError = failure == null ? null : Recorder.render(failure);
        report.errorMatch = Objects.equals(report.replayedError, cv.error());

        if (report.divergence == null && report.unanswerable == null) {
            Object rj = Serial.toJsonable(result);
            report.replayedResult = rj;
            report.resultMatch = Json.equal(rj, cv.raw().get("result"));
        }
        return report;
    }

    // --------------------------------------------------------------- helpers

    static List<SemPair> semPairs(List<Map<String, Object>> events) {
        List<SemPair> out = new ArrayList<>();
        for (Map<String, Object> e : events) {
            if ("sem".equals(e.get("k"))) {
                out.add(new SemPair(Json.asString(e.get("name")), Json.asString(e.get("phase"))));
            }
        }
        return out;
    }

    static String semDivergence(List<SemPair> recorded, List<SemPair> replayed) {
        int n = Math.max(recorded.size(), replayed.size());
        for (int i = 0; i < n; i++) {
            SemPair a = i < recorded.size() ? recorded.get(i) : null;
            SemPair b = i < replayed.size() ? replayed.get(i) : null;
            if (a == null || b == null || !a.equals(b)) {
                return "semantic divergence at " + i + ": recorded " + (a == null ? "nothing" : a)
                        + ", replayed " + (b == null ? "nothing" : b)
                        + " — the code's account of what it was doing has changed";
            }
        }
        return null;
    }

    static double toDouble(Object v) {
        return v instanceof Number n ? n.doubleValue() : 0.0;
    }

    static String brief(Object v) {
        String s = Json.write(v);
        return s.length() > 400 ? s.substring(0, 400) : s;
    }
}
