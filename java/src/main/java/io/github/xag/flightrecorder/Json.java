package io.github.xag.flightrecorder;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * The tape's JSON, hand-rolled — and hand-rolled on purpose.
 *
 * <p>Java ships no JSON in the platform, and this library ships no dependencies: a recorder is a
 * thing you install into someone else's app, and every jar it drags along is a version conflict it
 * can cause in a codebase it was supposed to be observing silently. .NET made the same call in
 * Json.cs even though it *had* System.Text.Json, because the thing actually needed here is not a
 * general JSON library — it is a codec with two specific disciplines the general ones get wrong:
 *
 * <ul>
 *   <li><b>Integer-vs-float is preserved on the way in.</b> A number whose token carries no
 *       {@code . e E} parses to {@link Long}; anything else to {@link Double}. The tape-v1 checker
 *       must be able to say "{@code seq} must be an int" and reject {@code 1.0} — which a parser
 *       that funnels every number through a double cannot do. Go reaches this by
 *       {@code json.Number} + {@code UseNumber()}; this is the same rule, applied at the same
 *       place.
 *   <li><b>Comparison is by canonical form, not by token.</b> Replay compares what the code asked
 *       now against what it asked when recorded, and those two values travelled different roads —
 *       one through a live object, one through a file. {@code 30} and {@code 30.0} are the same
 *       answer and map key order is not information, so {@link #canonical} sorts keys and collapses
 *       an integral double onto its integer. Without this, replay reports divergence on a value
 *       that never changed.
 * </ul>
 *
 * <p>The value surface is exactly the tape's: {@code null}, {@link Boolean}, {@link Long},
 * {@link Double}, {@link String}, {@link List}, and {@link Map} with string keys.
 */
public final class Json {

    private Json() {}

    // ---------------------------------------------------------------- writing

    /** Renders a jsonable tree. Map iteration order is preserved, so a tape line reads in the
     *  order the recorder built it ({@code ev}, {@code seq}, {@code fn}, …) rather than
     *  alphabetically. Key order is not part of the contract — {@link #canonical} is what
     *  comparison uses. */
    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b ? "true" : "false");
        } else if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                // Not JSON. Callers encode through Serial, which turns these into an opaque
                // marker long before they arrive here; this is the backstop that keeps a
                // malformed line from ever reaching a file.
                sb.append("null");
            } else if (d == Math.rint(d) && Math.abs(d) < 1e15) {
                sb.append((long) d);
            } else {
                sb.append(shortestDouble(d));
            }
        } else if (v instanceof Number n) {
            sb.append(n.longValue());
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object x : it) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, x);
            }
            sb.append(']');
        } else if (v.getClass().isArray()) {
            sb.append('[');
            int n = java.lang.reflect.Array.getLength(v);
            for (int i = 0; i < n; i++) {
                if (i > 0) sb.append(',');
                writeTo(sb, java.lang.reflect.Array.get(v, i));
            }
            sb.append(']');
        } else {
            // Nothing else should reach the writer: Serial is what turns objects into this
            // surface. Rendering the text rather than throwing keeps the failure direction
            // "the recording is a bit poorer", never "the app broke because it was recorded".
            writeString(sb, String.valueOf(v));
        }
    }

    /** {@code Double.toString} but without the trailing {@code ".0"} problem for values that are
     *  genuinely fractional, and preferring the shortest round-tripping form. */
    private static String shortestDouble(double d) {
        String s = Double.toString(d);
        if (s.endsWith(".0")) return s.substring(0, s.length() - 2);
        return s;
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"'  -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                default -> {
                    // Control characters must be escaped; everything else (including non-ASCII)
                    // rides through as UTF-8, which is what the spec says the file is.
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    // ---------------------------------------------------------------- reading

    /** Parses one JSON document. Throws {@link JsonException} on anything malformed, including
     *  trailing content — a tape line is one complete object, and a line with a second value on
     *  it is corrupt, not generous. */
    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.value();
        p.skipWs();
        if (p.pos < p.src.length()) {
            throw new JsonException("trailing content at offset " + p.pos);
        }
        return v;
    }

    /** Thrown for malformed JSON. Readers catch this to recognise the one corruption the spec
     *  tolerates: a truncated final line, written by a process that died mid-write. */
    public static final class JsonException extends RuntimeException {
        public JsonException(String message) { super(message); }
    }

    private static final class Parser {
        final String src;
        int pos;

        Parser(String src) { this.src = src; }

        void skipWs() {
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        char peek() {
            if (pos >= src.length()) throw new JsonException("unexpected end of input");
            return src.charAt(pos);
        }

        Object value() {
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

        Object literal(String word, Object v) {
            if (!src.startsWith(word, pos)) {
                throw new JsonException("bad literal at offset " + pos);
            }
            pos += word.length();
            return v;
        }

        Map<String, Object> object() {
            Map<String, Object> out = new LinkedHashMap<>();
            pos++; // '{'
            skipWs();
            if (peek() == '}') { pos++; return out; }
            while (true) {
                skipWs();
                if (peek() != '"') throw new JsonException("object key must be a string at offset " + pos);
                String k = string();
                skipWs();
                if (peek() != ':') throw new JsonException("expected ':' at offset " + pos);
                pos++;
                skipWs();
                out.put(k, value());
                skipWs();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return out; }
                throw new JsonException("expected ',' or '}' at offset " + pos);
            }
        }

        List<Object> array() {
            List<Object> out = new ArrayList<>();
            pos++; // '['
            skipWs();
            if (peek() == ']') { pos++; return out; }
            while (true) {
                skipWs();
                out.add(value());
                skipWs();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return out; }
                throw new JsonException("expected ',' or ']' at offset " + pos);
            }
        }

        String string() {
            pos++; // opening quote
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= src.length()) throw new JsonException("unterminated string");
                char c = src.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c != '\\') { sb.append(c); continue; }
                if (pos >= src.length()) throw new JsonException("unterminated escape");
                char e = src.charAt(pos++);
                switch (e) {
                    case '"'  -> sb.append('"');
                    case '\\' -> sb.append('\\');
                    case '/'  -> sb.append('/');
                    case 'n'  -> sb.append('\n');
                    case 'r'  -> sb.append('\r');
                    case 't'  -> sb.append('\t');
                    case 'b'  -> sb.append('\b');
                    case 'f'  -> sb.append('\f');
                    case 'u'  -> {
                        if (pos + 4 > src.length()) throw new JsonException("truncated \\u escape");
                        sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                        pos += 4;
                    }
                    default -> throw new JsonException("bad escape \\" + e + " at offset " + (pos - 1));
                }
            }
        }

        /**
         * The integer/float split, made here and nowhere else.
         *
         * <p>A token with no {@code .}, {@code e} or {@code E} is an integer and parses to
         * {@link Long}. This is what lets the checker reject {@code "seq": 1.0} where an int is
         * required — Python's {@code isinstance(x, int)} and Go's {@code json.Number} draw the
         * line in exactly the same place.
         */
        Object number() {
            int start = pos;
            if (pos < src.length() && (src.charAt(pos) == '-' || src.charAt(pos) == '+')) pos++;
            boolean fractional = false;
            while (pos < src.length()) {
                char c = src.charAt(pos);
                if (c >= '0' && c <= '9') { pos++; continue; }
                if (c == '.' || c == 'e' || c == 'E') { fractional = true; pos++; continue; }
                if ((c == '-' || c == '+') && fractional) { pos++; continue; }
                break;
            }
            String tok = src.substring(start, pos);
            if (tok.isEmpty() || tok.equals("-") || tok.equals("+")) {
                throw new JsonException("bad number at offset " + start);
            }
            try {
                if (fractional) return Double.parseDouble(tok);
                return Long.parseLong(tok);
            } catch (NumberFormatException ex) {
                // A magnitude beyond long is still a number; keeping it as a double loses
                // precision but keeps the line readable, which is the right trade for a tape.
                try {
                    return Double.parseDouble(tok);
                } catch (NumberFormatException ex2) {
                    throw new JsonException("bad number '" + tok + "' at offset " + start);
                }
            }
        }
    }

    // ------------------------------------------------------------- comparison

    /**
     * A rendering two values can be compared by.
     *
     * <p>Keys are sorted, and a double that happens to be integral is written as an integer — so
     * {@code 30} equals {@code 30.0} and {@code {a:1,b:2}} equals {@code {b:2,a:1}}. Replay leans
     * on both: the recorded value came off a file and the replayed one out of a live object, and
     * neither difference is a behaviour change.
     */
    public static String canonical(Object v) {
        StringBuilder sb = new StringBuilder();
        canonicalTo(sb, v);
        return sb.toString();
    }

    /** Whether two jsonable trees say the same thing. */
    public static boolean equal(Object a, Object b) {
        return canonical(a).equals(canonical(b));
    }

    private static void canonicalTo(StringBuilder sb, Object v) {
        if (v instanceof Map<?, ?> m) {
            Map<String, Object> sorted = new TreeMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                sorted.put(String.valueOf(e.getKey()), e.getValue());
            }
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : sorted.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                canonicalTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Iterator<?> i = it.iterator(); i.hasNext(); ) {
                if (!first) sb.append(',');
                first = false;
                canonicalTo(sb, i.next());
            }
            sb.append(']');
        } else if (v != null && v.getClass().isArray()) {
            sb.append('[');
            int n = java.lang.reflect.Array.getLength(v);
            for (int i = 0; i < n; i++) {
                if (i > 0) sb.append(',');
                canonicalTo(sb, java.lang.reflect.Array.get(v, i));
            }
            sb.append(']');
        } else {
            writeTo(sb, v);
        }
    }

    // ----------------------------------------------------------- small helpers

    /** Reads a map entry as a map, or null when it is absent or another shape. Tape reading is
     *  full of this: the format is open (unknown keys are ignored), so a reader asks rather than
     *  asserts. */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> asMap(Object v) {
        return v instanceof Map ? (Map<String, Object>) v : null;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asList(Object v) {
        return v instanceof List ? (List<Object>) v : null;
    }

    public static String asString(Object v) {
        return v instanceof String s ? s : null;
    }

    /** True for a number the tape considers an integer. {@code 1.0} is not one — see
     *  {@link Parser#number}. */
    public static boolean isInt(Object v) {
        return v instanceof Long || v instanceof Integer || v instanceof Short || v instanceof Byte;
    }

    public static boolean isNumber(Object v) {
        return v instanceof Number;
    }
}
