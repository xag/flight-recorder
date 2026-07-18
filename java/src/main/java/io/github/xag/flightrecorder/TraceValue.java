package io.github.xag.flightrecorder;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The value codec for the TRACE, which is a different artifact from the tape and has a different
 * format.
 *
 * <p>The governing decision is <b>trace version 2: a traced value is DATA, not a rendering</b>.
 * Version 1 recorded {@code repr} strings, and an invariant asserting arithmetic over a repr fails
 * confusingly rather than loudly — {@code "42"} is not {@code 42}, and finding that out from a
 * failing comparison six frames away is miserable. So a traced int is an int, a traced document is
 * a map, and a claim about an internal variable can be written the way you would write it about a
 * live one.
 *
 * <p>The marker set is a superset of the tape's, because a trace sees things a boundary never does
 * — arbitrarily long strings and collections, live snapshot objects, and user maps that happen to
 * look like markers:
 *
 * <ul>
 *   <li>{@code __str__ {len, head}} — a long string, truncated but still reporting its TRUE length,
 *       so {@code len} stays assertable after truncation.
 *   <li>{@code __seq__ {len, head}} — the same for a long collection.
 *   <li>{@code __snap__} — a document snapshot (Python and .NET emit these; Java revives them).
 *   <li>{@code __esc__} — a user map that itself looks like a marker, escaped so the round trip is
 *       honest rather than silently reinterpreting the app's own data as recorder metadata.
 * </ul>
 */
public final class TraceValue {

    private TraceValue() {}

    /** How many members of a collection are kept. Beyond this the value becomes {@code __seq__}. */
    public static final int MAX_ITEMS = 100;

    /** How many characters of a string are kept. Beyond this it becomes {@code __str__}. */
    public static final int MAX_CHARS = 512;

    private static final int MAX_DEPTH = 8;

    private static final List<String> MARKERS = List.of(
            "__dt__", "__date__", "__opaque__", "__undef__", "__snap__", "__seq__", "__str__", "__esc__");

    private static Map<String, Object> marker(String k, Object v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(k, v);
        return m;
    }

    private static boolean looksLikeMarker(Map<?, ?> m) {
        if (m.size() != 1) return false;
        return MARKERS.contains(String.valueOf(m.keySet().iterator().next()));
    }

    /** Encodes one observed value. Never throws — a tracer that breaks the code it observes is
     *  worse than no tracer. */
    public static Object toTraceJsonable(Object v) {
        try {
            return encode(v, 0);
        } catch (Throwable t) {
            return marker("__opaque__", "<unencodable>");
        }
    }

    private static Object encode(Object v, int depth) {
        if (v == null) return null;
        if (depth > MAX_DEPTH) return marker("__opaque__", Serial.safeRepr(v, 200));

        if (v instanceof Boolean b) return b;
        if (v instanceof Character c) return String.valueOf(c);

        if (v instanceof String s) {
            if (s.length() <= MAX_CHARS) return s;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("len", (long) s.length());
            m.put("head", s.substring(0, MAX_CHARS));
            return marker("__str__", m);
        }

        if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) return marker("__opaque__", String.valueOf(d));
            return d;
        }
        if (v instanceof Number n) {
            if (v instanceof java.math.BigDecimal bd) return bd.doubleValue();
            return n.longValue();
        }

        if (v instanceof OffsetDateTime t) return marker("__dt__", Serial.iso(t));
        if (v instanceof LocalDateTime t) return marker("__dt__", Serial.isoNaive(t));
        if (v instanceof LocalDate t) return marker("__date__", t.toString());
        if (v instanceof java.time.Instant t) return marker("__dt__", t.toString());

        if (v instanceof Snapshot s) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", s.id);
            m.put("exists", s.exists);
            m.put("data", encode(s.data, depth + 1));
            return marker("__snap__", m);
        }

        if (v instanceof byte[] b) {
            int head = Math.min(b.length, 32);
            StringBuilder hex = new StringBuilder();
            for (int i = 0; i < head; i++) hex.append(String.format("%02x", b[i]));
            return marker("__opaque__", "<bytes " + b.length + ": " + hex + ">");
        }

        if (v.getClass().isArray()) {
            int n = java.lang.reflect.Array.getLength(v);
            List<Object> items = new ArrayList<>();
            for (int i = 0; i < Math.min(n, MAX_ITEMS); i++) {
                items.add(encode(java.lang.reflect.Array.get(v, i), depth + 1));
            }
            return n > MAX_ITEMS ? truncated(n, items) : items;
        }

        if (v instanceof Collection<?> c) {
            List<Object> items = new ArrayList<>();
            int n = c.size();
            for (Object x : c) {
                if (items.size() >= MAX_ITEMS) break;
                items.add(encode(x, depth + 1));
            }
            return n > MAX_ITEMS ? truncated(n, items) : items;
        }

        if (v instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            int i = 0;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (i++ >= MAX_ITEMS) break;
                out.put(String.valueOf(e.getKey()), encode(e.getValue(), depth + 1));
            }
            // A user map shaped exactly like a marker is escaped rather than emitted as-is, so a
            // reader never mistakes the app's own data for recorder metadata.
            if (looksLikeMarker(out)) return marker("__esc__", out);
            return out;
        }

        if (v instanceof Enum<?> e) return e.name();

        // Anything else: the tape codec already knows how to reach an object's public surface, and
        // a trace wants the same surface.
        Object viaTape = Serial.toJsonable(v);
        if (viaTape instanceof Map<?, ?> m && looksLikeMarker(m)) return viaTape;
        return viaTape;
    }

    private static Map<String, Object> truncated(int len, List<Object> head) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("len", (long) len);
        m.put("head", head);
        return marker("__seq__", m);
    }

    /** Revives a traced value. Truncation markers revive as their head — the length stays readable
     *  through {@link #lengthOf}. */
    public static Object fromTraceJsonable(Object v) {
        if (v instanceof Map<?, ?> m && m.size() == 1) {
            Map.Entry<?, ?> e = m.entrySet().iterator().next();
            String k = String.valueOf(e.getKey());
            Object x = e.getValue();
            switch (k) {
                case "__dt__", "__date__" -> {
                    if (x instanceof String s) {
                        Object parsed = Serial.parseIso(s);
                        if (parsed != null) return parsed;
                    }
                    return x;
                }
                case "__undef__" -> { return null; }
                case "__opaque__" -> { return x; }
                case "__esc__" -> { return fromTraceJsonable(x); }
                case "__snap__" -> {
                    Map<String, Object> sm = Json.asMap(x);
                    if (sm == null) return x;
                    Object id = sm.get("id");
                    return new Snapshot(id == null ? null : String.valueOf(id),
                            Boolean.TRUE.equals(sm.get("exists")),
                            fromTraceJsonable(sm.get("data")));
                }
                case "__str__" -> {
                    Map<String, Object> sm = Json.asMap(x);
                    return sm == null ? x : sm.get("head");
                }
                case "__seq__" -> {
                    Map<String, Object> sm = Json.asMap(x);
                    return sm == null ? x : fromTraceJsonable(sm.get("head"));
                }
                default -> { }
            }
        }
        if (v instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                out.put(String.valueOf(e.getKey()), fromTraceJsonable(e.getValue()));
            }
            return out;
        }
        if (v instanceof List<?> l) {
            List<Object> out = new ArrayList<>();
            for (Object x : l) out.add(fromTraceJsonable(x));
            return out;
        }
        return v;
    }

    /** The TRUE length a truncation marker reports, or the natural length of a live value, or -1
     *  when length is not a thing the value has. */
    public static long lengthOf(Object encoded) {
        if (encoded instanceof Map<?, ?> m && m.size() == 1) {
            Object x = m.values().iterator().next();
            Map<String, Object> sm = Json.asMap(x);
            if (sm != null && sm.get("len") instanceof Number n) return n.longValue();
        }
        if (encoded instanceof String s) return s.length();
        if (encoded instanceof Collection<?> c) return c.size();
        if (encoded instanceof Map<?, ?> m) return m.size();
        return -1;
    }

    /** A compact rendering of a traced value, for a human or a failure message. */
    public static String render(Object v, int limit) {
        String s;
        if (v == null) s = "null";
        else if (v instanceof String str) s = "\"" + str + "\"";
        else if (v instanceof Snapshot snap) s = snap.toString();
        else if (v instanceof Map || v instanceof List) s = Json.write(Serial.toJsonable(v));
        else s = String.valueOf(v);
        if (s.length() <= limit) return s;
        return s.substring(0, limit - 1) + "…";
    }
}
