package io.github.xag.flightrecorder;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.RecordComponent;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.function.UnaryOperator;
import java.util.regex.Pattern;

/**
 * The Java half of the tape-v1 "Value encoding" contract (spec/tape-v1.md).
 *
 * <p>Everything crossing the recorded boundary becomes JSON with revivable markers for datetimes,
 * and anything the tape cannot represent degrades to an opaque marker rather than breaking the
 * recorded call. The failure direction is always "the recording is a bit poorer", never "the app
 * broke because it was being recorded".
 *
 * <p>This mirrors {@code flight_recorder/serial.py}, {@code js/src/serial.js},
 * {@code go/serial/serial.go} and {@code csharp/src/FlightRecorder/Serial.cs}. Where Python
 * branches on a dynamic type and Go on reflection, Java does the same with
 * {@code java.lang.reflect}.
 */
public final class Serial {

    private Serial() {}

    static final int MAX_DEPTH = 16;

    /** What a field's value becomes under a bare (null) rule, or when a rule or scrub throws. */
    public static final String REDACTED = "[REDACTED]";

    /**
     * A memory address in a rendered value is an IDENTITY — different on every run — so recording
     * it would make the effect it belongs to never match on replay. Java's default
     * {@code Object.toString} is {@code ClassName@1b6d3586}, so the shape to strip is the
     * {@code @hex} suffix; the Python and JS recorders strip their own runtime's equivalent.
     */
    private static final Pattern ADDR = Pattern.compile("@[0-9a-fA-F]+");

    private static final DateTimeFormatter ISO_AWARE = DateTimeFormatter.ISO_OFFSET_DATE_TIME;
    private static final DateTimeFormatter ISO_NAIVE = DateTimeFormatter.ISO_LOCAL_DATE_TIME;
    private static final DateTimeFormatter ISO_DATE = DateTimeFormatter.ISO_LOCAL_DATE;

    // ------------------------------------------------------------------ time

    /** An always-aware rendering, for the values that are recorder metadata:
     *  {@code session.started} and {@code call.ts}. */
    public static String iso(OffsetDateTime t) { return t.format(ISO_AWARE); }

    /** A naive rendering, for a value the application itself was handed. See {@link #encode} on
     *  why awareness is part of the value and must not be normalised. */
    public static String isoNaive(LocalDateTime t) { return t.format(ISO_NAIVE); }

    public static OffsetDateTime nowAware() { return OffsetDateTime.now(); }

    // -------------------------------------------------------------- encoding

    /** Renders any value for an opaque marker, with memory addresses scrubbed and length capped.
     *  Never throws. */
    public static String safeRepr(Object v, int limit) {
        String s;
        try {
            s = "<" + (v == null ? "null" : v.getClass().getSimpleName()) + " " + v + ">";
        } catch (Throwable t) {
            // A toString() that throws is exactly the kind of thing that must not take the
            // recorded call down with it.
            s = "<" + (v == null ? "null" : v.getClass().getSimpleName()) + " ?>";
        }
        s = ADDR.matcher(s).replaceAll("");
        if (s.length() <= limit) return s;
        return s.substring(0, limit - 1) + "…";
    }

    static Map<String, Object> opaque(Object v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("__opaque__", safeRepr(v, 200));
        return m;
    }

    private static Map<String, Object> marker(String key, Object value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(key, value);
        return m;
    }

    /**
     * Encodes one boundary value into a jsonable tree with markers. The result contains only
     * null, Boolean, Long, Double, String, List and Map — exactly the surface the tape-v1 checker
     * accepts.
     */
    public static Object toJsonable(Object v) { return encode(v, 0); }

    private static Object encode(Object v, int depth) {
        if (depth > MAX_DEPTH) return opaque(v);
        if (v == null) return null;

        // Time first: it is data with a marker, not an object to walk.
        //
        // The naive/aware split is preserved rather than normalised, and that is load-bearing.
        // `now.v` is a value the APPLICATION was handed, and replay must hand back something
        // indistinguishable from it. A LocalDateTime and an OffsetDateTime are not
        // interchangeable in Java any more than a naive and an aware datetime are in Python —
        // code branches and comparisons behave differently — so a codec that "helpfully"
        // promoted one to the other would change behaviour on replay, which is the one thing
        // replay may never do.
        if (v instanceof OffsetDateTime t) return marker("__dt__", t.format(ISO_AWARE));
        if (v instanceof ZonedDateTime t) return marker("__dt__", t.toOffsetDateTime().format(ISO_AWARE));
        if (v instanceof Instant t) return marker("__dt__", t.atOffset(java.time.ZoneOffset.UTC).format(ISO_AWARE));
        if (v instanceof java.util.Date t) {
            return marker("__dt__", t.toInstant().atZone(ZoneId.systemDefault()).toOffsetDateTime().format(ISO_AWARE));
        }
        if (v instanceof LocalDateTime t) return marker("__dt__", t.format(ISO_NAIVE));
        if (v instanceof LocalDate t) return marker("__date__", t.format(ISO_DATE));

        if (v instanceof Boolean b) return b;
        if (v instanceof String s) return s;
        if (v instanceof Character c) return String.valueOf(c);

        if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) return opaque(v); // not JSON
            return d;
        }
        if (v instanceof java.math.BigDecimal bd) return bd.doubleValue();
        if (v instanceof java.math.BigInteger bi) return bi.longValue();
        if (v instanceof Number n) return n.longValue();

        // Raw bytes are entropy or a payload, not structure: hex, tagged opaque, like JS and Go.
        if (v instanceof byte[] b) {
            int head = Math.min(b.length, 32);
            StringBuilder hex = new StringBuilder();
            for (int i = 0; i < head; i++) hex.append(String.format("%02x", b[i]));
            return marker("__opaque__", "<bytes " + b.length + ": " + hex + ">");
        }

        if (v.getClass().isArray()) {
            int n = java.lang.reflect.Array.getLength(v);
            List<Object> out = new ArrayList<>(n);
            for (int i = 0; i < n; i++) out.add(encode(java.lang.reflect.Array.get(v, i), depth + 1));
            return out;
        }
        if (v instanceof Collection<?> c) {
            List<Object> out = new ArrayList<>(c.size());
            for (Object x : c) out.add(encode(x, depth + 1));
            return out;
        }
        if (v instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                out.put(String.valueOf(e.getKey()), encode(e.getValue(), depth + 1));
            }
            return out;
        }
        if (v instanceof java.util.Optional<?> o) {
            return o.isPresent() ? encode(o.get(), depth) : null; // unwrapping is not nesting
        }
        if (v instanceof Enum<?> e) return e.name();
        if (v instanceof java.util.UUID u) return u.toString();

        return encodeObject(v, depth);
    }

    /**
     * Records an object's public data surface — the thing an app reads and writes at a boundary.
     *
     * <p>Record components first (they are the declared surface), then public getters, then
     * public fields. Names are camelCased, deliberately: field-name redaction is declared in the
     * app's own lowercase vocabulary ({@code "password"}), and a codec that emitted
     * {@code "Password"} would route straight past the rule that exists to mask it. .NET
     * camelCases its properties for exactly this reason.
     *
     * <p>Reviving an object as itself is impossible (as with a JS class instance), so revival
     * yields a generic map; the tape keeps the surface, not the type.
     */
    private static Object encodeObject(Object v, int depth) {
        Class<?> cls = v.getClass();
        Map<String, Object> out = new LinkedHashMap<>();
        try {
            if (cls.isRecord()) {
                for (RecordComponent rc : cls.getRecordComponents()) {
                    Method m = rc.getAccessor();
                    m.setAccessible(true);
                    out.put(rc.getName(), encode(m.invoke(v), depth + 1));
                }
                return out;
            }
            for (Method m : cls.getMethods()) {
                if (m.getParameterCount() != 0) continue;
                if (m.getDeclaringClass() == Object.class) continue;
                if (Modifier.isStatic(m.getModifiers())) continue;
                String name = m.getName();
                String prop;
                if (name.startsWith("get") && name.length() > 3) prop = name.substring(3);
                else if (name.startsWith("is") && name.length() > 2
                        && (m.getReturnType() == boolean.class || m.getReturnType() == Boolean.class)) {
                    prop = name.substring(2);
                } else continue;
                out.put(camel(prop), encode(m.invoke(v), depth + 1));
            }
            for (Field f : cls.getFields()) {
                if (Modifier.isStatic(f.getModifiers())) continue;
                out.putIfAbsent(camel(f.getName()), encode(f.get(v), depth + 1));
            }
        } catch (Throwable t) {
            // A getter that throws is the app's business, not the recorder's. Degrade.
            return opaque(v);
        }
        if (out.isEmpty()) return opaque(v);
        return out;
    }

    private static String camel(String s) {
        if (s.isEmpty()) return s;
        if (s.length() > 1 && Character.isUpperCase(s.charAt(0)) && Character.isUpperCase(s.charAt(1))) {
            return s; // an acronym (URL, ID) stays as it is
        }
        return Character.toLowerCase(s.charAt(0)) + s.substring(1);
    }

    // -------------------------------------------------------------- decoding

    /** Parses one of the three layouts a datetime marker may carry, in order. */
    static Object parseIso(String s) {
        try { return OffsetDateTime.parse(s, ISO_AWARE); } catch (Exception ignored) { }
        try { return LocalDateTime.parse(s, ISO_NAIVE); } catch (Exception ignored) { }
        try { return LocalDate.parse(s, ISO_DATE); } catch (Exception ignored) { }
        return null;
    }

    /**
     * Revives a boundary value.
     *
     * <p>{@code __opaque__} is a one-way door by design — it revives as its text.
     * {@code __undef__} (which only the JS runtime emits; Java never writes one) revives to null,
     * the same as JSON null: a Java program has no way to hold "undefined" distinctly, and
     * inventing one would be a worse lie than collapsing it.
     */
    public static Object fromJsonable(Object v) {
        if (v instanceof Map<?, ?> m) {
            if (m.size() == 1) {
                Map.Entry<?, ?> e = m.entrySet().iterator().next();
                String k = String.valueOf(e.getKey());
                Object x = e.getValue();
                switch (k) {
                    case "__dt__", "__date__" -> {
                        if (x instanceof String s) {
                            Object parsed = parseIso(s);
                            if (parsed != null) return parsed;
                        }
                        return x;
                    }
                    case "__undef__" -> { return null; }
                    case "__opaque__" -> { return x; }
                    default -> { }
                }
            }
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                out.put(String.valueOf(e.getKey()), fromJsonable(e.getValue()));
            }
            return out;
        }
        if (v instanceof List<?> l) {
            List<Object> out = new ArrayList<>(l.size());
            for (Object x : l) out.add(fromJsonable(x));
            return out;
        }
        return v;
    }

    // -------------------------------------------------------------- coercion

    /**
     * Fits a revived value into the type the code was promised.
     *
     * <p>A tape stores structure, not types: a record comes back as a {@link Map}. Under record
     * that never shows, because the app's own object is what flows. Under replay the map IS what
     * flows — so without this step, code that declared a return type fails on a cast the recorder
     * caused, and the recorder has broken the very thing it exists not to disturb.
     *
     * <p>Deliberately best-effort: when the shape cannot be fitted, the value is returned as it is
     * rather than throwing. A poorer replay is a finding; a replay that dies inside the codec is a
     * distraction.
     */
    @SuppressWarnings("unchecked")
    public static Object coerce(Object v, Class<?> target) {
        if (target == null || target == Object.class || target == void.class || target == Void.TYPE) {
            return v;
        }
        if (v == null) return defaultOf(target);
        if (target.isInstance(v) && !(v instanceof Map && target.isRecord())) return v;

        try {
            if (target == String.class) return String.valueOf(v);
            if (v instanceof Number n) {
                if (target == int.class || target == Integer.class) return n.intValue();
                if (target == long.class || target == Long.class) return n.longValue();
                if (target == double.class || target == Double.class) return n.doubleValue();
                if (target == float.class || target == Float.class) return n.floatValue();
                if (target == short.class || target == Short.class) return n.shortValue();
                if (target == byte.class || target == Byte.class) return n.byteValue();
            }
            if (v instanceof Boolean b && (target == boolean.class || target == Boolean.class)) return b;
            if (target.isEnum() && v instanceof String s) {
                return Enum.valueOf((Class<? extends Enum>) target.asSubclass(Enum.class), s);
            }
            if (target.isRecord() && v instanceof Map<?, ?> m) {
                RecordComponent[] rcs = target.getRecordComponents();
                Class<?>[] types = new Class<?>[rcs.length];
                Object[] argv = new Object[rcs.length];
                for (int i = 0; i < rcs.length; i++) {
                    types[i] = rcs[i].getType();
                    argv[i] = coerce(m.get(rcs[i].getName()), rcs[i].getType());
                }
                var ctor = target.getDeclaredConstructor(types);
                ctor.setAccessible(true);
                return ctor.newInstance(argv);
            }
            if (v instanceof List<?> l && target.isArray()) {
                Object arr = java.lang.reflect.Array.newInstance(target.getComponentType(), l.size());
                for (int i = 0; i < l.size(); i++) {
                    java.lang.reflect.Array.set(arr, i, coerce(l.get(i), target.getComponentType()));
                }
                return arr;
            }
        } catch (Throwable ignored) {
            // Fall through: hand back what we have.
        }
        return v;
    }

    private static Object defaultOf(Class<?> target) {
        if (!target.isPrimitive()) return null;
        if (target == boolean.class) return false;
        if (target == char.class) return '\0';
        if (target == long.class) return 0L;
        if (target == double.class) return 0.0d;
        if (target == float.class) return 0.0f;
        return 0;
    }

    // ------------------------------------------------------------- redaction

    /**
     * Redacts by FIELD NAME: a jsonable map entry whose key is named here has its value replaced —
     * by {@link #REDACTED} when the rule is null, else by the rule's output. A rule that throws
     * degrades to {@link #REDACTED}.
     */
    public static final class Rules extends LinkedHashMap<String, Function<Object, Object>> {}

    private static Object safeApply(Function<Object, Object> fn, Object x) {
        try {
            return fn.apply(x);
        } catch (Throwable t) {
            return REDACTED;
        }
    }

    private static String safeScrub(UnaryOperator<String> fn, String s) {
        try {
            String out = fn.apply(s);
            return out == null ? REDACTED : out;
        } catch (Throwable t) {
            return REDACTED;
        }
    }

    /**
     * Applies field-name rules and a value scrub to a jsonable tree.
     *
     * <p>The scrub sweeps every leaf string wherever it sits, catching secrets no field name can
     * see — a positional argument, a key built by interpolation, prose mid-sentence in a body.
     * Object KEYS are deliberately not swept, so tapes stay comparable across implementations.
     *
     * <p>A field rule's own output meets the sweep too, so a transform that shortens rather than
     * masks cannot smuggle the secret past.
     *
     * <p>Both layers MUST be idempotent — replay re-derives the question, scrubs it the same way,
     * and compares against the tape, so a value that is already a mask must scrub to itself. See
     * {@link Boundary#scrubbing} , which refuses a mask that would match its own pattern.
     *
     * <p>The failure direction is always "masked", never "leaked" and never "broke the recorded
     * call".
     */
    public static Object redact(Object v, Map<String, Function<Object, Object>> rules,
                                UnaryOperator<String> scrub) {
        boolean hasRules = rules != null && !rules.isEmpty();
        if (!hasRules && scrub == null) return v;
        return redact0(v, hasRules ? rules : null, scrub);
    }

    private static Object redact0(Object v, Map<String, Function<Object, Object>> rules,
                                  UnaryOperator<String> scrub) {
        if (v instanceof List<?> l) {
            List<Object> out = new ArrayList<>(l.size());
            for (Object x : l) out.add(redact0(x, rules, scrub));
            return out;
        }
        if (v instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                String k = String.valueOf(e.getKey());
                if (rules != null && rules.containsKey(k)) {
                    Function<Object, Object> rule = rules.get(k);
                    if (rule == null) out.put(k, REDACTED);
                    else out.put(k, leaf(safeApply(rule, e.getValue()), scrub));
                    continue;
                }
                out.put(k, redact0(e.getValue(), rules, scrub));
            }
            return out;
        }
        return leaf(v, scrub);
    }

    private static Object leaf(Object x, UnaryOperator<String> scrub) {
        if (scrub == null || !(x instanceof String s)) return x;
        return safeScrub(scrub, s);
    }

    // ---------------------------------------------------------------- render

    /** A compact, stable rendering of a chained-call argument, for db signatures. */
    public static String shortRender(Object v, int limit) {
        String s;
        try {
            s = Json.write(toJsonable(v));
        } catch (Throwable t) {
            s = safeRepr(v, limit);
        }
        if (s.length() <= limit) return s;
        return s.substring(0, limit - 1) + "…";
    }

    public static String shortRender(Object v) { return shortRender(v, 60); }
}
