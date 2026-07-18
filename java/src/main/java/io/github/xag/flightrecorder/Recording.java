package io.github.xag.flightrecorder;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;

/**
 * A loaded tape: the analysis layer.
 *
 * <p>A tape is data, so this needs no runtime — a {@code Recording} reads any conformant tape,
 * recorded by any implementation, and recovers its structure: the calls, and each call's
 * semantic-span tree with the raw events each span encloses, in order.
 *
 * <p>It also edits. A recording is data, so a hostile world is one mutation away: load a call,
 * empty a result or run the clock backwards, mark it a probe, and replay the real code against the
 * world that never happened.
 */
public final class Recording {

    public final Map<String, Object> header;
    private final List<Map<String, Object>> calls;
    private final List<Pattern> forbid = new ArrayList<>();

    private Recording(Map<String, Object> header, List<Map<String, Object>> calls) {
        this.header = header;
        this.calls = calls;
    }

    /** Reads a tape from a file. */
    public static Recording load(String path) throws IOException {
        return parse(Files.readString(Paths.get(path), StandardCharsets.UTF_8));
    }

    /**
     * Parses a tape from its text.
     *
     * <p>A truncated final line — the process died mid-write — is the only corruption the format
     * admits, and it is discarded rather than raised: every line is complete when written, so the
     * rest of the tape is still evidence.
     */
    public static Recording parse(String text) {
        Map<String, Object> header = null;
        List<Map<String, Object>> calls = new ArrayList<>();
        String[] lines = text.split("\n", -1);
        for (String ln : lines) {
            if (ln.isBlank()) continue;
            Map<String, Object> obj;
            try {
                obj = Json.asMap(Json.parse(ln));
            } catch (Json.JsonException e) {
                continue; // tolerate a torn final line
            }
            if (obj == null) continue;
            Object ev = obj.get("ev");
            if ("session".equals(ev)) header = obj;
            else if ("call".equals(ev)) calls.add(obj);
            // A reader MUST ignore an `ev` it does not know. That is the whole
            // forward-compatibility story, and why new event kinds need no version bump.
        }
        if (header == null) {
            throw new IllegalArgumentException("no session header — not a flight recording?");
        }
        Object version = header.get("version");
        if (!(version instanceof Long v) || v != Recorder.FORMAT_VERSION) {
            throw new IllegalArgumentException(
                    "tape format version " + version + " is not one this reader implements (v"
                            + Recorder.FORMAT_VERSION + ")");
        }
        return new Recording(header, calls);
    }

    public int numCalls() { return calls.size(); }

    /** A view onto call {@code i}, through which its events can be inspected and mutated. */
    public CallView call(int i) {
        if (i < 0 || i >= calls.size()) return null;
        return new CallView(this, i, calls.get(i));
    }

    /** The first call named {@code fn}, or null. */
    public CallView call(String fn) {
        for (int i = 0; i < calls.size(); i++) {
            if (fn.equals(calls.get(i).get("fn"))) return call(i);
        }
        return null;
    }

    /**
     * Arms this recording's tripwire with the same patterns the {@link Boundary} declared, so
     * {@link #save} refuses to write a forbidden value.
     *
     * <p>The write path was guarded and the RE-write path was not, which is the wrong way round:
     * mutation exists precisely to EDIT recorded values, so a tape that passed the tripwire when it
     * was recorded can have a credential put into it by hand and then be saved with nothing
     * looking.
     *
     * <p>The patterns have to be handed over here because <b>a tape does not carry them</b> — the
     * rules are the boundary's, not the artifact's, and they are deliberately not written onto the
     * tape for a later reader to find and "helpfully" relax.
     */
    public Recording forbidding(String... patterns) {
        forbid.clear();
        for (String p : patterns) {
            try {
                forbid.add(Pattern.compile(p));
            } catch (java.util.regex.PatternSyntaxException e) {
                throw new IllegalArgumentException("bad forbid pattern \"" + p + "\": " + e.getMessage(), e);
            }
        }
        return this;
    }

    private String forbiddenHit(String line) {
        for (Pattern p : forbid) {
            if (p.matcher(line).find()) return p.pattern();
        }
        return null;
    }

    /**
     * Writes the (possibly mutated) recording back to a tape file.
     *
     * <p>If {@link #forbidding} armed the tripwire, every line is vetted before <b>any</b> of them
     * reaches the disk — the whole tape is built in memory first, so a refusal leaves no
     * half-written file behind and never truncates a good tape to punish a bad edit.
     */
    public String save(String path) throws IOException {
        StringBuilder b = new StringBuilder();
        appendVetted(b, header, "the session record");
        for (int i = 0; i < calls.size(); i++) {
            Map<String, Object> c = calls.get(i);
            appendVetted(b, c, "the edited call record for \"" + c.get("fn") + "\" (call " + i + ")");
        }
        Path p = Paths.get(path);
        if (p.getParent() != null) Files.createDirectories(p.getParent());
        Files.writeString(p, b.toString(), StandardCharsets.UTF_8);
        return path;
    }

    private void appendVetted(StringBuilder b, Map<String, Object> obj, String what) {
        String line = Json.write(obj);
        String hit = forbiddenHit(line);
        if (hit != null) throw new Errors.ForbiddenValue(hit, what);
        b.append(line).append('\n');
    }

    // ------------------------------------------------------------- a call view

    /** Inspects and edits one recorded call. */
    public static final class CallView {

        final Recording rec;
        final int index;
        final Map<String, Object> raw;

        CallView(Recording rec, int index, Map<String, Object> raw) {
            this.rec = rec;
            this.index = index;
            this.raw = raw;
        }

        public String fn() { return Json.asString(raw.get("fn")); }

        public int index() { return index; }

        /** The call's kwargs, revived. */
        public Map<String, Object> kwargs() {
            Object k = Serial.fromJsonable(raw.get("kwargs"));
            return k instanceof Map ? Json.asMap(k) : new LinkedHashMap<>();
        }

        /** The recorded result, revived. */
        public Object result() { return Serial.fromJsonable(raw.get("result")); }

        /** The recorded error's rendering, or null if the call did not raise. */
        public String error() { return Json.asString(raw.get("error")); }

        /** The call's raw boundary events, in order — mutate them in place to visit a world that
         *  never happened. */
        public List<Map<String, Object>> events() {
            List<Object> arr = Json.asList(raw.get("events"));
            List<Map<String, Object>> out = new ArrayList<>();
            if (arr != null) for (Object e : arr) {
                Map<String, Object> m = Json.asMap(e);
                if (m != null) out.add(m);
            }
            return out;
        }

        /** The nth event of a given kind ({@code fx}/{@code db}/{@code now}/{@code perf}/
         *  {@code rand}/{@code sem}), or null. */
        public Map<String, Object> event(String kind, int n) {
            int seen = 0;
            for (Map<String, Object> e : events()) {
                if (kind.equals(e.get("k"))) {
                    if (seen == n) return e;
                    seen++;
                }
            }
            return null;
        }

        public Map<String, Object> event(String kind) { return event(kind, 0); }

        /**
         * Flags this call a probe.
         *
         * <p>A mutated upstream answer changes every downstream question, so replay stops comparing
         * arguments — name and order still gate. The flag is persisted to the tape, so a saved
         * mutated call can never later be mistaken for a strict regression pin.
         */
        public CallView markProbe() {
            raw.put("probe", true);
            return this;
        }

        public boolean isProbe() { return Boolean.TRUE.equals(raw.get("probe")); }

        Map<String, Object> raw() { return raw; }

        // --------------------------------------------------- the span tree

        /**
         * Recovers the call's span tree — the property the whole {@code sem} event kind exists for,
         * recovered from a tape any runtime could have written.
         *
         * <p>The tree comes from ORDER alone: no event carries a parent pointer. {@code begin}
         * pushes, {@code end} pops and sets the outcome, {@code point} attaches to the current top,
         * and every non-{@code sem} event attaches to the enclosing node. That works because a span
         * is well-nested by construction — it wraps the body it encloses — which is exactly why a
         * recorder that cannot guarantee nesting must not emit {@code sem} at all.
         */
        public SpanNode spans() {
            SpanNode root = new SpanNode(fn(), "call", raw.get("error") != null ? "error" : "ok", null);
            List<SpanNode> stack = new ArrayList<>();
            stack.add(root);
            for (Map<String, Object> e : events()) {
                SpanNode top = stack.get(stack.size() - 1);
                if (!"sem".equals(e.get("k"))) {
                    top.events.add(e);
                    continue;
                }
                String phase = Json.asString(e.get("phase"));
                if ("begin".equals(phase)) {
                    SpanNode node = new SpanNode(Json.asString(e.get("name")), "span", "", dataOf(e));
                    top.children.add(node);
                    stack.add(node);
                } else if ("end".equals(phase)) {
                    if (stack.size() > 1) {
                        top.outcome = Json.asString(e.get("outcome"));
                        stack.remove(stack.size() - 1);
                    }
                } else if ("point".equals(phase)) {
                    top.children.add(new SpanNode(Json.asString(e.get("name")), "point", "", dataOf(e)));
                }
            }
            return root;
        }

        /** A top-down, human-readable rendering of the span tree — the same shape the other
         *  runtimes' readers produce, so a tape reads identically whoever wrote it. */
        public String renderSpans() {
            StringBuilder b = new StringBuilder();
            render(spans(), 0, b);
            String s = b.toString();
            int end = s.length();
            while (end > 0 && s.charAt(end - 1) == '\n') end--;
            return s.substring(0, end);
        }

        private static void render(SpanNode n, int depth, StringBuilder b) {
            String indent = "  ".repeat(depth);
            if ("point".equals(n.phase)) {
                b.append(indent).append("- ").append(n.name).append(renderData(n.data)).append('\n');
                return;
            }
            String outcome = "error".equals(n.outcome) ? "ERROR" : "ok";
            b.append(indent).append(n.name).append("  ").append(outcome)
                    .append(renderCount(n.events)).append('\n');
            for (SpanNode ch : n.children) render(ch, depth + 1, b);
        }

        private static Map<String, Object> dataOf(Map<String, Object> e) {
            return Json.asMap(e.get("data"));
        }

        private static String renderCount(List<Map<String, Object>> events) {
            if (events.isEmpty()) return "";
            Map<String, Integer> kinds = new LinkedHashMap<>();
            for (Map<String, Object> e : events) {
                kinds.merge(String.valueOf(e.get("k")), 1, Integer::sum);
            }
            if (kinds.size() == 1) {
                return "  (" + events.size() + " " + kinds.keySet().iterator().next() + ")";
            }
            return "  (" + events.size() + " events)";
        }

        private static String renderData(Map<String, Object> data) {
            if (data == null || data.isEmpty()) return "";
            Map<String, Object> sorted = new TreeMap<>(data);
            StringBuilder b = new StringBuilder("  ");
            boolean first = true;
            for (Map.Entry<String, Object> e : sorted.entrySet()) {
                if (!first) b.append(' ');
                first = false;
                b.append(e.getKey()).append('=');
                if (e.getValue() instanceof String s) b.append('"').append(s).append('"');
                else b.append(Json.write(e.getValue()));
            }
            return b.toString();
        }
    }

    /**
     * A node of a call's structure: the call itself, a span, or a point note.
     *
     * <p>A span (and the call) carries the raw boundary events directly beneath it — those enclosed
     * by no deeper span — plus its child spans and notes, in order. That juxtaposition is the
     * point: a span claiming to have charged a card, with no call beneath it to the thing that
     * charges cards, is a claim a reader can refute.
     */
    public static final class SpanNode {
        public final String name;
        public final String phase;   // "call" | "span" | "point"
        public String outcome;       // "ok" | "error"; "" for a point
        public final Map<String, Object> data;
        public final List<Map<String, Object>> events = new ArrayList<>();
        public final List<SpanNode> children = new ArrayList<>();

        SpanNode(String name, String phase, String outcome, Map<String, Object> data) {
            this.name = name;
            this.phase = phase;
            this.outcome = outcome;
            this.data = data;
        }
    }
}
