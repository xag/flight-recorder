package io.github.xag.flightrecorder.spec;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Tape v1 conformance checker for Java — the mirror of the normative Python arbiter
 * ({@code spec/validate.py}), and of the Node, .NET and Go ports.
 *
 * <p>The five files are the same claim written five times, on purpose. The tape is the contract
 * between the runtimes, and the only way to know a contract holds is to have independent parties
 * agree about the same artifact: every checker runs over the fixtures in {@code spec/fixtures/},
 * and a disagreement means the tape has forked — the single failure this whole arrangement exists
 * to prevent.
 *
 * <p>So this file imports nothing from {@code io.github.xag.flightrecorder}, not even its JSON
 * codec. It carries its own parser instead. That duplication is the point: a checker that shared
 * the recorder's reader would bless whatever that reader happens to do, and the two would agree
 * about a malformed tape forever without either one being right. Python, Go and Node reach the
 * same independence for free by using a JSON parser from their standard library; Java has none, so
 * the independence has to be bought with the ~100 lines below.
 *
 * <p>{@link #validateTape} returns human-readable violations; empty means conformant.
 */
public final class Validate {

    private Validate() {}

    public static final int VERSION = 1;
    public static final int MAX_DEPTH = 16;

    // __undef__ exists for JavaScript, which has two nothings. Java has one, so a Java recorder
    // never emits it and a Java reader revives it as null — the marker costs this runtime nothing
    // and buys the other one exact fidelity.
    private static final Set<String> MARKERS =
            new HashSet<>(Arrays.asList("__dt__", "__date__", "__undef__", "__opaque__"));
    // Reserved by the trace encoding — a *reader* must tolerate them, so they are legal in a tape
    // even though a v1 recorder never emits them.
    private static final Set<String> RESERVED_MARKERS =
            new HashSet<>(Arrays.asList("__snap__", "__seq__", "__str__", "__esc__"));
    private static final Set<String> EVENT_KINDS =
            new HashSet<>(Arrays.asList("fx", "db", "now", "perf", "rand", "sem"));
    private static final Set<String> SEM_PHASES =
            new HashSet<>(Arrays.asList("begin", "end", "point"));
    // Adding a runtime is an additive change (the spec's own "add a key, no version bump" rule):
    // existing tapes still validate, and a further recorder's tapes now validate too.
    private static final String[] RUNTIMES = {"python", "node", "dotnet", "go", "java"};

    // Deliberately a regex rather than java.time: the reference checker accepts whatever Python's
    // datetime.fromisoformat accepts, which is a shape, not one of java.time's named formats.
    private static final Pattern ISO = Pattern.compile(
            "^\\d{4}-\\d{2}-\\d{2}([T ]\\d{2}:\\d{2}:\\d{2}(\\.\\d+)?(Z|[+-]\\d{2}:?\\d{2})?)?$");
    private static final Pattern HAS_OFFSET = Pattern.compile("(Z|[+-]\\d{2}:?\\d{2})$");
    private static final Pattern HEX = Pattern.compile("^[0-9a-f]+$");

    // ------------------------------------------------------------------ predicates

    private static boolean isIso(Object v) {
        return v instanceof String s && ISO.matcher(s).matches();
    }

    private static boolean isTzAware(Object v) {
        return isIso(v) && HAS_OFFSET.matcher((String) v).find();
    }

    /**
     * Mirrors Python's {@code isinstance(x, int)}: a JSON integer literal, never a float, never a
     * bool. The parser below decides this from the token — {@code 1} is an int, {@code 1.0} is not
     * — exactly where Go's {@code json.Number} + {@code UseNumber()} draws the same line.
     */
    private static boolean isInt(Object v) { return v instanceof Long; }

    private static long asInt(Object v) { return (Long) v; }

    private static boolean isNumber(Object v) { return v instanceof Long || v instanceof Double; }

    private static double asNum(Object v) { return ((Number) v).doubleValue(); }

    private static boolean isMap(Object v) { return v instanceof Map; }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Object v) { return (Map<String, Object>) v; }

    @SuppressWarnings("unchecked")
    private static List<Object> list(Object v) { return (List<Object>) v; }

    private static Object get(Object o, String k) {
        return isMap(o) ? map(o).get(k) : null;
    }

    private static boolean has(Object o, String k) {
        return isMap(o) && map(o).containsKey(k);
    }

    /** A value as it should read inside a violation message. */
    private static String show(Object v) { return Jsonish.render(v); }

    // ------------------------------------------------------------------ the rules

    /** A boundary value: JSON, with at most a marker at any node. */
    private static void checkValue(Object v, String path, List<String> out, int depth) {
        if (depth > MAX_DEPTH) {
            out.add(path + ": nested deeper than " + MAX_DEPTH + "; must degrade to __opaque__");
            return;
        }
        if (v == null || v instanceof String || v instanceof Boolean || isNumber(v)) return;

        if (v instanceof List<?> arr) {
            for (int i = 0; i < arr.size(); i++) checkValue(arr.get(i), path + "[" + i + "]", out, depth + 1);
            return;
        }

        if (isMap(v)) {
            Map<String, Object> m = map(v);
            if (m.size() == 1) {
                String k = m.keySet().iterator().next();
                Object payload = m.get(k);
                if (MARKERS.contains(k)) {
                    if ((k.equals("__dt__") || k.equals("__date__")) && !isIso(payload)) {
                        out.add(path + ": " + k + " payload is not ISO-8601: " + show(payload));
                    }
                    if (k.equals("__undef__") && !Boolean.TRUE.equals(payload)) {
                        out.add(path + ": __undef__ payload must be true");
                    }
                    if (k.equals("__opaque__")) {
                        if (!(payload instanceof String s)) {
                            out.add(path + ": __opaque__ payload must be a string");
                        } else if (s.length() > 200) {
                            out.add(path + ": __opaque__ payload exceeds 200 chars");
                        }
                    }
                    return;
                }
                if (RESERVED_MARKERS.contains(k)) return; // reserved: legal, not interpreted here
            }
            for (Map.Entry<String, Object> e : m.entrySet()) {
                checkValue(e.getValue(), path + "." + e.getKey(), out, depth + 1);
            }
            return;
        }

        out.add(path + ": " + v.getClass().getSimpleName() + " is not JSON");
    }

    private static void checkSnapshot(Object s, String path, List<String> out) {
        if (!isMap(s)) {
            out.add(path + ": snapshot must be an object");
            return;
        }
        for (String key : new String[] {"id", "exists", "data"}) {
            if (!has(s, key)) out.add(path + ": snapshot missing '" + key + "'");
        }
        if (has(s, "exists") && !(get(s, "exists") instanceof Boolean)) {
            out.add(path + ".exists: must be a bool");
        }
        if (has(s, "data")) checkValue(get(s, "data"), path + ".data", out, 0);
    }

    private static void checkEvent(Object e, String path, List<String> out) {
        if (!isMap(e)) {
            out.add(path + ": event must be an object");
            return;
        }
        Object kv = get(e, "k");
        if (!(kv instanceof String k) || !EVENT_KINDS.contains(k)) {
            return; // unknown kind: a reader must ignore it (forward compatibility)
        }

        switch (k) {
            case "fx" -> {
                if (!(get(e, "fn") instanceof String)) out.add(path + ": fx needs a string 'fn'");
                if (!(get(e, "args") instanceof List)) {
                    out.add(path + ": fx needs an array 'args'");
                } else {
                    checkValue(get(e, "args"), path + ".args", out, 0);
                }
                if (!isMap(get(e, "kwargs"))) {
                    out.add(path + ": fx needs an object 'kwargs' ({} in JS)");
                } else {
                    checkValue(get(e, "kwargs"), path + ".kwargs", out, 0);
                }
                boolean hasRes = has(e, "res");
                boolean hasErr = has(e, "err");
                if (hasRes == hasErr) out.add(path + ": fx must carry exactly one of 'res' / 'err'");
                if (hasRes) checkValue(get(e, "res"), path + ".res", out, 0);
                if (hasErr) {
                    Object err = get(e, "err");
                    if (!isMap(err) || !(get(err, "type") instanceof String)) {
                        out.add(path + ".err: must be an object with a string 'type'");
                    }
                }
            }

            case "db" -> {
                if (!(get(e, "op") instanceof String)) out.add(path + ": db needs a string 'op'");
                if (!(get(e, "sig") instanceof String)) out.add(path + ": db needs a string 'sig'");
                boolean hasRes = has(e, "res");
                boolean hasArgs = has(e, "args");
                if (hasRes && hasArgs) {
                    out.add(path + ": db carries 'res' (a read) or 'args' (a write), never both");
                }
                if (!hasRes && !hasArgs) out.add(path + ": db must carry 'res' or 'args'");
                if (hasRes) {
                    Object r = get(e, "res");
                    if (r instanceof List<?> arr) {
                        for (int i = 0; i < arr.size(); i++) {
                            checkSnapshot(arr.get(i), path + ".res[" + i + "]", out);
                        }
                    } else {
                        checkSnapshot(r, path + ".res", out);
                    }
                }
                if (hasArgs) checkValue(get(e, "args"), path + ".args", out, 0);
            }

            // ISO-8601, and deliberately NOT required to be timezone-aware. This is an app-visible
            // value, not recorder metadata: the app called now() and got back whatever it got back.
            // Python's datetime.now() is naive, and comparing a naive datetime with an aware one
            // raises — so a replay that "helpfully" handed back an aware value where the recording
            // saw a naive one would change behaviour, which is the one thing replay may never do.
            case "now" -> {
                if (!isIso(get(e, "v"))) {
                    out.add(path + ": now.v must be an ISO-8601 string, got " + show(get(e, "v")));
                }
            }

            // A separate kind from `now` because it is a separate clock: monotonic, arbitrary
            // origin, not a wall time. Feeding a wall time back into it would be a category error.
            case "perf" -> {
                Object v = get(e, "v");
                if (!isNumber(v)) {
                    out.add(path + ": perf.v must be a number (milliseconds), got " + show(v));
                }
            }

            // Testimony, not evidence. The checker validates its SHAPE and says nothing about its
            // content: `name` is the app's own vocabulary and no implementation may interpret it. A
            // checker that knew what a span name meant would have given the library semantics,
            // which is the one thing the library is not allowed to have.
            case "sem" -> {
                Object name = get(e, "name");
                if (!(name instanceof String s) || s.isEmpty()) {
                    out.add(path + ": sem needs a non-empty string 'name'");
                }
                Object phase = get(e, "phase");
                if (!(phase instanceof String p) || !SEM_PHASES.contains(p)) {
                    out.add(path + ": sem.phase must be one of begin|end|point, got " + show(phase));
                }
                if (!isInt(get(e, "sid"))) {
                    out.add(path + ": sem needs an int 'sid', unique within the call");
                }
                if (has(e, "data")) {
                    if (!isMap(get(e, "data"))) {
                        out.add(path + ": sem.data must be an object");
                    } else {
                        checkValue(get(e, "data"), path + ".data", out, 0);
                    }
                }
                if (has(e, "outcome")) {
                    Object outcome = get(e, "outcome");
                    if (!"end".equals(phase)) {
                        out.add(path + ": sem.outcome belongs to an 'end', not a " + show(phase));
                    }
                    if (!"ok".equals(outcome) && !"error".equals(outcome)) {
                        out.add(path + ": sem.outcome must be 'ok' or 'error', got " + show(outcome));
                    }
                }
            }

            case "rand" -> checkRand(e, path, out);

            default -> { }
        }
    }

    private static void checkRand(Object e, String path, List<String> out) {
        Object m = get(e, "m");
        if ("sample".equals(m)) {
            for (String key : new String[] {"n", "kk"}) {
                if (!isInt(get(e, key))) out.add(path + ": rand." + key + " must be an int");
            }
            Object idxv = get(e, "idx");
            List<Long> idx = null;
            if (idxv instanceof List<?> arr) {
                idx = new ArrayList<>();
                for (Object x : arr) {
                    if (!isInt(x)) { idx = null; break; }
                    idx.add(asInt(x));
                }
            }
            if (idx == null) {
                out.add(path + ": rand.idx must be an array of ints");
            } else if (isInt(get(e, "n"))) {
                long n = asInt(get(e, "n"));
                List<Long> bad = new ArrayList<>();
                for (long i : idx) if (i < 0 || i >= n) bad.add(i);
                if (!bad.isEmpty()) {
                    out.add(path + ": rand.idx " + bad + " out of range for population " + n);
                }
                if (isInt(get(e, "kk")) && idx.size() != asInt(get(e, "kk"))) {
                    out.add(path + ": rand.idx has " + idx.size() + " positions but kk="
                            + asInt(get(e, "kk")));
                }
            }
        } else if ("bytes".equals(m)) {
            Object n = get(e, "n");
            boolean nOk = isInt(n) && asInt(n) >= 0;
            if (!nOk) out.add(path + ": rand.n must be a non-negative int");
            Object hxv = get(e, "hex");
            if (!(hxv instanceof String hx) || (!hx.isEmpty() && !HEX.matcher(hx).matches())) {
                out.add(path + ": rand.hex must be a lowercase hex string");
            } else if (isInt(n) && hx.length() != 2 * asInt(n)) {
                out.add(path + ": rand.hex is " + hx.length() + " chars but n=" + asInt(n)
                        + " implies " + (2 * asInt(n)));
            }
        } else if ("float".equals(m)) {
            Object v = get(e, "v");
            if (!isNumber(v) || !(asNum(v) >= 0.0 && asNum(v) < 1.0)) {
                out.add(path + ": rand.v must be a number in [0, 1), got " + show(v));
            }
        } else if ("int".equals(m)) {
            if (!isInt(get(e, "v"))) {
                out.add(path + ": rand.v must be an int, got " + show(get(e, "v")));
            }
        } else {
            out.add(path + ": rand.m must be one of sample|bytes|float|int, got " + show(m));
        }
    }

    private record SemFrame(long sid, Object name) {}

    /**
     * The one structural promise {@code sem} makes: begin/end pairs are well-nested within a call.
     *
     * <p>Enclosure is derived from ORDER — a span contains every event between its begin and its
     * end — so nesting is not decoration, it is the only thing that makes the derivation sound. Two
     * spans that straddle (A begins, B begins, A ends, B ends) would put an event inside both and
     * inside neither, and every reader that walks the stream would build a different tree.
     *
     * <p>A span left open by a process that died mid-call is a separate matter and not a violation
     * here: that call never reached the tape at all. It lives in the {@code inflight} sidecar, an
     * unknown {@code ev} to this checker, and there an unclosed span is exactly the information the
     * reader wants.
     */
    private static void checkSemNesting(List<Object> evs, String path, List<String> out) {
        List<SemFrame> stack = new ArrayList<>();
        Set<Long> seen = new HashSet<>();
        for (int j = 0; j < evs.size(); j++) {
            Object e = evs.get(j);
            if (!isMap(e) || !"sem".equals(get(e, "k"))) continue;
            Object sidv = get(e, "sid");
            Object phase = get(e, "phase");
            Object name = get(e, "name");
            if (!isInt(sidv) || !(phase instanceof String p) || !SEM_PHASES.contains(p)) {
                continue; // already reported by checkEvent; do not compound it
            }
            long sid = asInt(sidv);

            if (p.equals("begin") || p.equals("point")) {
                if (!seen.add(sid)) {
                    out.add(path + ".events[" + j + "]: sem sid " + sid + " is reused — a sid must "
                            + "be unique within the call, or an 'end' cannot name its 'begin'");
                }
                if (p.equals("begin")) stack.add(new SemFrame(sid, name));
            } else { // end
                if (stack.isEmpty()) {
                    out.add(path + ".events[" + j + "]: sem 'end' (sid " + sid + ") with no open span");
                } else if (stack.get(stack.size() - 1).sid() != sid) {
                    SemFrame open = stack.get(stack.size() - 1);
                    out.add(path + ".events[" + j + "]: sem spans are not well-nested — 'end' closes "
                            + "sid " + sid + " while sid " + open.sid() + " (" + show(open.name())
                            + ") is still open. Spans nest; they never straddle.");
                    // Unwind to it if it is open at all, so one crossing is not reported N times.
                    boolean present = false;
                    for (SemFrame f : stack) if (f.sid() == sid) { present = true; break; }
                    if (present) {
                        while (!stack.isEmpty() && stack.get(stack.size() - 1).sid() != sid) {
                            stack.remove(stack.size() - 1);
                        }
                        if (!stack.isEmpty()) stack.remove(stack.size() - 1);
                    }
                } else {
                    stack.remove(stack.size() - 1);
                }
            }
        }
        for (SemFrame f : stack) {
            out.add(path + ": sem span " + show(f.name()) + " (sid " + f.sid() + ") is never closed "
                    + "— a completed call holds no open spans");
        }
    }

    private static void validateLine(Object obj, int i, List<String> out, boolean first) {
        if (!isMap(obj)) {
            out.add("line " + i + ": not an object");
            return;
        }
        Object ev = get(obj, "ev");

        if (first) {
            if (!"session".equals(ev)) {
                out.add("line " + i + ": the first line must be the session header, got ev=" + show(ev));
                return;
            }
        } else if ("session".equals(ev)) {
            out.add("line " + i + ": a second session header");
            return;
        }

        if ("session".equals(ev)) {
            Object version = get(obj, "version");
            if (!isInt(version) || asInt(version) != VERSION) {
                out.add("line " + i + ": version must be " + VERSION + ", got " + show(version));
            }
            if (!isTzAware(get(obj, "started"))) {
                out.add("line " + i + ": session.started must be timezone-aware ISO-8601");
            }
            if (!isMap(get(obj, "constants"))) {
                out.add("line " + i + ": session.constants must be an object");
            } else {
                checkValue(get(obj, "constants"), "line " + i + ".constants", out, 0);
            }
            List<String> runtimes = new ArrayList<>();
            for (String rk : RUNTIMES) if (has(obj, rk)) runtimes.add(rk);
            if (runtimes.size() != 1) {
                out.add("line " + i + ": session must name exactly one runtime "
                        + "(python|node|dotnet|go|java), got " + runtimes);
            }
            return;
        }

        if ("call".equals(ev)) {
            Object seq = get(obj, "seq");
            if (!isInt(seq) || asInt(seq) < 1) out.add("line " + i + ": call.seq must be an int >= 1");
            if (!(get(obj, "fn") instanceof String)) out.add("line " + i + ": call.fn must be a string");
            if (!isMap(get(obj, "kwargs"))) {
                out.add("line " + i + ": call.kwargs must be an object");
            } else {
                checkValue(get(obj, "kwargs"), "line " + i + ".kwargs", out, 0);
            }
            if (has(obj, "result")) checkValue(get(obj, "result"), "line " + i + ".result", out, 0);
            if (!has(obj, "error")) {
                out.add("line " + i + ": call must carry 'error' (null when it did not raise)");
            } else {
                Object err = get(obj, "error");
                if (err != null && !(err instanceof String)) {
                    out.add("line " + i + ": call.error must be a string or null");
                }
            }
            if (!isTzAware(get(obj, "ts"))) {
                out.add("line " + i + ": call.ts must be timezone-aware ISO-8601");
            }
            if (!isNumber(get(obj, "ms"))) out.add("line " + i + ": call.ms must be a number");
            Object evs = get(obj, "events");
            if (!(evs instanceof List)) {
                out.add("line " + i + ": call.events must be an array");
            } else {
                List<Object> arr = list(evs);
                for (int j = 0; j < arr.size(); j++) {
                    checkEvent(arr.get(j), "line " + i + ".events[" + j + "]", out);
                }
                checkSemNesting(arr, "line " + i, out);
            }
            return;
        }

        // unknown ev (e.g. the reserved "inflight"): a reader must tolerate it.
    }

    /** Validates a whole tape. Returns violations; empty means conformant. */
    public static List<String> validateTape(String text) {
        List<String> out = new ArrayList<>();
        List<String> lines = new ArrayList<>();
        for (String ln : text.split("\n", -1)) {
            if (!ln.isBlank()) lines.add(ln);
        }
        if (lines.isEmpty()) {
            out.add("empty tape: the session header is mandatory");
            return out;
        }

        List<Long> seqs = new ArrayList<>();
        for (int i = 0; i < lines.size(); i++) {
            Object obj;
            try {
                obj = Jsonish.parse(lines.get(i));
            } catch (Jsonish.Bad ex) {
                // Only the final line may be torn (the process died mid-write).
                if (i == lines.size() - 1) continue;
                out.add("line " + i + ": not JSON (" + ex.getMessage() + ")");
                continue;
            }
            validateLine(obj, i, out, i == 0);
            if (isMap(obj) && "call".equals(get(obj, "ev")) && isInt(get(obj, "seq"))) {
                seqs.add(asInt(get(obj, "seq")));
            }
        }

        boolean ok = true;
        for (int i = 0; i < seqs.size(); i++) {
            if (seqs.get(i) != (long) (i + 1)) { ok = false; break; }
        }
        if (!ok) out.add("call.seq must be 1-based and monotonic; got " + seqs);

        return out;
    }

    // ------------------------------------------------------------------ its own JSON

    /**
     * A JSON reader owned by the checker, so the checker owes the implementation nothing.
     *
     * <p>It produces exactly the tape's value surface — {@code null}, {@link Boolean},
     * {@link Long}, {@link Double}, {@link String}, {@link List}, {@link Map} — and draws the
     * integer/float line at the token: no {@code . e E} means {@link Long}. That is what lets the
     * rules above reject {@code "seq": 1.0} where an int is required, which a parser funnelling
     * every number through a double could not do. Python reaches it with
     * {@code isinstance(x, int)}, Go with {@code json.Number}.
     */
    static final class Jsonish {

        static final class Bad extends RuntimeException {
            Bad(String message) { super(message); }
        }

        private final String src;
        private int pos;

        private Jsonish(String src) { this.src = src; }

        static Object parse(String text) {
            Jsonish p = new Jsonish(text);
            p.ws();
            Object v = p.value();
            p.ws();
            if (p.pos < text.length()) throw new Bad("trailing content at offset " + p.pos);
            return v;
        }

        private void ws() {
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        private char peek() {
            if (pos >= src.length()) throw new Bad("unexpected end of input");
            return src.charAt(pos);
        }

        private Object value() {
            char c = peek();
            return switch (c) {
                case '{' -> object();
                case '[' -> array();
                case '"' -> string();
                case 't' -> literal("true", Boolean.TRUE);
                case 'f' -> literal("false", Boolean.FALSE);
                case 'n' -> literal("null", null);
                default -> number();
            };
        }

        private Object literal(String word, Object v) {
            if (!src.startsWith(word, pos)) throw new Bad("bad literal at offset " + pos);
            pos += word.length();
            return v;
        }

        private Map<String, Object> object() {
            Map<String, Object> out = new LinkedHashMap<>();
            pos++;
            ws();
            if (peek() == '}') { pos++; return out; }
            while (true) {
                ws();
                if (peek() != '"') throw new Bad("object key must be a string at offset " + pos);
                String k = string();
                ws();
                if (peek() != ':') throw new Bad("expected ':' at offset " + pos);
                pos++;
                ws();
                out.put(k, value());
                ws();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return out; }
                throw new Bad("expected ',' or '}' at offset " + pos);
            }
        }

        private List<Object> array() {
            List<Object> out = new ArrayList<>();
            pos++;
            ws();
            if (peek() == ']') { pos++; return out; }
            while (true) {
                ws();
                out.add(value());
                ws();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return out; }
                throw new Bad("expected ',' or ']' at offset " + pos);
            }
        }

        private String string() {
            pos++;
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= src.length()) throw new Bad("unterminated string");
                char c = src.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c != '\\') { sb.append(c); continue; }
                if (pos >= src.length()) throw new Bad("unterminated escape");
                char e = src.charAt(pos++);
                switch (e) {
                    case '"' -> sb.append('"');
                    case '\\' -> sb.append('\\');
                    case '/' -> sb.append('/');
                    case 'n' -> sb.append('\n');
                    case 'r' -> sb.append('\r');
                    case 't' -> sb.append('\t');
                    case 'b' -> sb.append('\b');
                    case 'f' -> sb.append('\f');
                    case 'u' -> {
                        if (pos + 4 > src.length()) throw new Bad("truncated \\u escape");
                        sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                        pos += 4;
                    }
                    default -> throw new Bad("bad escape \\" + e + " at offset " + (pos - 1));
                }
            }
        }

        private Object number() {
            int start = pos;
            if (pos < src.length() && src.charAt(pos) == '-') pos++;
            boolean digits = false;
            boolean fractional = false;
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if (c >= '0' && c <= '9') { digits = true; pos++; continue; }
                if (c == '.' || c == 'e' || c == 'E') { fractional = true; pos++; continue; }
                if ((c == '-' || c == '+') && fractional) { pos++; continue; }
                break;
            }
            String tok = src.substring(start, pos);
            if (!digits) throw new Bad("bad number at offset " + start);
            try {
                return fractional ? (Object) Double.parseDouble(tok) : (Object) Long.parseLong(tok);
            } catch (NumberFormatException ex) {
                // A magnitude beyond long is still a number, and a checker that rejected the line
                // outright would be reporting corruption where there is none.
                try {
                    return Double.parseDouble(tok);
                } catch (NumberFormatException ex2) {
                    throw new Bad("bad number '" + tok + "' at offset " + start);
                }
            }
        }

        /** Compact JSON, for quoting a value back inside a violation message. */
        static String render(Object v) {
            StringBuilder sb = new StringBuilder();
            renderTo(sb, v);
            return sb.toString();
        }

        private static void renderTo(StringBuilder sb, Object v) {
            if (v == null) {
                sb.append("null");
            } else if (v instanceof String s) {
                sb.append('"');
                for (int i = 0; i < s.length(); i++) {
                    char c = s.charAt(i);
                    if (c == '"' || c == '\\') sb.append('\\').append(c);
                    else if (c == '\n') sb.append("\\n");
                    else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
                sb.append('"');
            } else if (v instanceof Map<?, ?> m) {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : m.entrySet()) {
                    if (!first) sb.append(',');
                    first = false;
                    renderTo(sb, String.valueOf(e.getKey()));
                    sb.append(':');
                    renderTo(sb, e.getValue());
                }
                sb.append('}');
            } else if (v instanceof List<?> l) {
                sb.append('[');
                for (int i = 0; i < l.size(); i++) {
                    if (i > 0) sb.append(',');
                    renderTo(sb, l.get(i));
                }
                sb.append(']');
            } else {
                sb.append(v);
            }
        }
    }
}
