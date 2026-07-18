package io.github.xag.flightrecorder;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * A traced execution's internal state, queryable: every local, on every executed line, of the code
 * you named.
 *
 * <p>This is the thing that turns "what was {@code level} when it went wrong?" from an inference
 * into a lookup — and, with it, an invariant can assert over an <b>internal</b> variable, which is
 * the form that catches a bug whose output is perfectly self-consistent and still wrong.
 *
 * <p>Every traced value is data (see {@link TraceValue}): numbers compare, documents are maps, and
 * anything long is a prefix that still reports its true length.
 */
public final class Trace {

    /** The trace format's version. A version-1 trace held {@code repr} strings and is refused
     *  outright rather than half-understood. */
    public static final int TRACE_VERSION = 2;

    public final List<Map<String, Object>> events;

    Trace(List<Map<String, Object>> events) { this.events = events; }

    /**
     * The empty trace.
     *
     * <p>Empty, <b>never null</b>. A query against it answers "never observed", so a claim about an
     * untraced variable fails honestly instead of passing vacuously — which is the difference
     * between an invariant that is satisfied and one that was never actually checked.
     */
    public static Trace empty() { return new Trace(new ArrayList<>()); }

    /** Reads a trace file written by any runtime's tracer. */
    public static Trace load(String path) throws IOException {
        return parse(Files.readString(Paths.get(path), StandardCharsets.UTF_8));
    }

    /**
     * Reads the JSONL trace format.
     *
     * @throws IllegalArgumentException on a version-1 trace — asserting arithmetic over reprs fails
     *         confusingly rather than loudly, and traces are cheap: regenerate.
     */
    public static Trace parse(String text) {
        List<Map<String, Object>> events = new ArrayList<>();
        for (String ln : text.split("\n", -1)) {
            if (ln.isBlank()) continue;
            try {
                Map<String, Object> ev = Json.asMap(Json.parse(ln));
                if (ev != null) events.add(ev);
            } catch (Json.JsonException e) {
                // Tolerate a torn final line: a truncated trace is still evidence.
            }
        }
        if (!events.isEmpty() && "H".equals(events.get(0).get("e"))) {
            Object v = events.get(0).get("trace_version");
            int version = v instanceof Number n ? n.intValue() : 0;
            if (version != TRACE_VERSION) {
                throw new IllegalArgumentException(
                        "this trace was written by an older tracer (version " + version + ", need "
                        + TRACE_VERSION + ") — re-run the traced replay to regenerate it");
            }
            events.remove(0);
        }
        return new Trace(events);
    }

    public int size() { return events.size(); }

    public boolean isEmpty() { return events.isEmpty(); }

    // ---------------------------------------------------------------- records

    /** One sighting of a named variable, at the line whose execution produced it. */
    public record Obs(String at, String fn, String name, Object value) {
        @Override public String toString() {
            return name + "=" + TraceValue.render(value, 90) + " at " + at + " in " + fn;
        }
    }

    /** One entry into an instrumented function, with the arguments it arrived with. */
    public record Call(String at, String fn, Map<String, Object> args) {}

    /** One return, with the value that came back. */
    public record Return(String at, String fn, Object value) {}

    /** One exception on the way out of an instrumented function. */
    public record Raise(String at, String fn, String type, String detail) {}

    // ---------------------------------------------------------------- queries

    /**
     * The timeline of one variable: every value it held, in order, and where — the arguments it
     * arrived with and each line that changed it.
     *
     * <p>An output can be entirely self-consistent and still be produced by a wrong internal value.
     * That value is only visible here.
     */
    public List<Obs> values(String name) {
        List<Obs> out = new ArrayList<>();
        for (Map<String, Object> e : events) {
            Map<String, Object> bag = switch (String.valueOf(e.get("e"))) {
                case "L" -> Json.asMap(e.get("d"));
                case "C" -> Json.asMap(e.get("args"));
                default -> null;
            };
            if (bag == null || !bag.containsKey(name)) continue;
            // The tracer already emits only changes, so every entry here is a transition. There is
            // no second filter to apply and no unchanged value to hide.
            out.add(new Obs(Json.asString(e.get("at")), Json.asString(e.get("fn")), name,
                    TraceValue.fromTraceJsonable(bag.get(name))));
        }
        return out;
    }

    /** The value a variable arrived with, or null if it was never observed. */
    public Obs first(String name) {
        List<Obs> vs = values(name);
        return vs.isEmpty() ? null : vs.get(0);
    }

    /** The last value a variable held, or null if it was never observed. */
    public Obs last(String name) {
        List<Obs> vs = values(name);
        return vs.isEmpty() ? null : vs.get(vs.size() - 1);
    }

    /** Every distinct variable the trace ever saw, sorted. */
    public List<String> names() {
        TreeSet<String> seen = new TreeSet<>();
        for (Map<String, Object> e : events) {
            for (String key : new String[]{"d", "args"}) {
                Map<String, Object> bag = Json.asMap(e.get(key));
                if (bag != null) seen.addAll(bag.keySet());
            }
        }
        return new ArrayList<>(seen);
    }

    /**
     * Accepts the empty filter, an exact name, or a bare name against a qualified one — so
     * {@code calls("studyStatus")} finds {@code Toy.studyStatus} without the caller having to know
     * how it was qualified.
     */
    private static boolean matchFn(String want, String got) {
        if (want == null || want.isEmpty()) return true;
        if (got == null) return false;
        return got.equals(want) || got.endsWith("." + want);
    }

    /** Every entry into a function, with its arguments. */
    public List<Call> calls(String fn) {
        List<Call> out = new ArrayList<>();
        for (Map<String, Object> e : events) {
            if (!"C".equals(e.get("e")) || !matchFn(fn, Json.asString(e.get("fn")))) continue;
            Map<String, Object> args = new LinkedHashMap<>();
            Map<String, Object> bag = Json.asMap(e.get("args"));
            if (bag != null) for (Map.Entry<String, Object> a : bag.entrySet()) {
                args.put(a.getKey(), TraceValue.fromTraceJsonable(a.getValue()));
            }
            out.add(new Call(Json.asString(e.get("at")), Json.asString(e.get("fn")), args));
        }
        return out;
    }

    public List<Call> calls() { return calls(null); }

    /** Every return out of a function, with the value it produced. */
    public List<Return> returns(String fn) {
        List<Return> out = new ArrayList<>();
        for (Map<String, Object> e : events) {
            if (!"R".equals(e.get("e")) || !matchFn(fn, Json.asString(e.get("fn")))) continue;
            out.add(new Return(Json.asString(e.get("at")), Json.asString(e.get("fn")),
                    TraceValue.fromTraceJsonable(e.get("v"))));
        }
        return out;
    }

    public List<Return> returns() { return returns(null); }

    /** Every exception the trace saw leave an instrumented function. */
    public List<Raise> raised() {
        List<Raise> out = new ArrayList<>();
        for (Map<String, Object> e : events) {
            if (!"X".equals(e.get("e"))) continue;
            out.add(new Raise(Json.asString(e.get("at")), Json.asString(e.get("fn")),
                    Json.asString(e.get("type")), Json.asString(e.get("v"))));
        }
        return out;
    }

    // ---------------------------------------------------------------- render

    /** One variable's timeline, for a human or a failure message. A trace nobody can read is a
     *  trace nobody consults. */
    public String render(String name) {
        List<Obs> vs = values(name);
        if (vs.isEmpty()) return name + ": never observed";
        StringBuilder b = new StringBuilder();
        for (Obs o : vs) {
            b.append(String.format("  %-28s %s = %s%n", o.at(), name, TraceValue.render(o.value(), 90)));
        }
        return trimEnd(b.toString());
    }

    /** The whole trace: calls, changed locals, returns and exceptions, in order. */
    public String timeline() {
        StringBuilder b = new StringBuilder();
        for (Map<String, Object> e : events) {
            String at = Json.asString(e.get("at"));
            String fn = Json.asString(e.get("fn"));
            switch (String.valueOf(e.get("e"))) {
                case "C" -> b.append(String.format("  %-28s call %s(%s)%n", at, fn, renderBag(e.get("args"))));
                case "L" -> b.append(String.format("  %-28s %s%n", at, renderBag(e.get("d"))));
                case "R" -> b.append(String.format("  %-28s return %s%n", at,
                        TraceValue.render(TraceValue.fromTraceJsonable(e.get("v")), 90)));
                case "X" -> b.append(String.format("  %-28s THREW %s: %s%n", at, e.get("type"), e.get("v")));
                default -> { }
            }
        }
        return trimEnd(b.toString());
    }

    /** The trace as JSONL, header included — the artifact form. */
    public String toJsonl() {
        StringBuilder b = new StringBuilder();
        Map<String, Object> h = new LinkedHashMap<>();
        h.put("e", "H");
        h.put("trace_version", TRACE_VERSION);
        b.append(Json.write(h)).append('\n');
        for (Map<String, Object> e : events) b.append(Json.write(e)).append('\n');
        return b.toString();
    }

    private static String renderBag(Object v) {
        Map<String, Object> bag = Json.asMap(v);
        if (bag == null) return "";
        TreeSet<String> keys = new TreeSet<>(bag.keySet());
        List<String> parts = new ArrayList<>();
        for (String k : keys) {
            parts.add(k + "=" + TraceValue.render(TraceValue.fromTraceJsonable(bag.get(k)), 60));
        }
        return String.join(", ", parts);
    }

    private static String trimEnd(String s) {
        int end = s.length();
        while (end > 0 && (s.charAt(end - 1) == '\n' || s.charAt(end - 1) == '\r')) end--;
        return s.substring(0, end);
    }

    @Override public String toString() { return timeline(); }
}
