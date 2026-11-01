package io.github.xag.flightrecorder;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Stock declarations an app may hand its {@link Boundary}: credentials, masked by shape and by
 * field.
 *
 * <p>The recorder knows nothing about what any value means, and nothing here changes that: no
 * recording consults this class unless the app declares it, exactly as it declares any other scrub
 * or forbid. It ships beside the recorder because every app that records a configuration file, an
 * environment or a conversation transcript meets the same credentials.
 *
 * <pre>{@code
 * Boundary b = new Boundary()
 *     .scrubbingWith(Scrubs::credentials)          // mask every credential, wherever it sits
 *     .forbidden(Scrubs.CREDENTIAL_SHAPES);        // and refuse a line where one survived
 * }</pre>
 *
 * <p>An app whose own fields are named like credentials and hold none (a {@code key} naming where
 * a client keeps its servers, a {@code token_env} naming a variable) says so with {@code exempt}:
 * {@code .scrubbingWith(s -> Scrubs.credentials(s, Set.of("key", "token_env")))}.
 *
 * <p>{@link #credentials} masks TEXT: in any string, every credential shape (an {@code sk-} key, a
 * GitHub or GitLab token, an AWS access key id, a Slack token, a JWT, a Bearer or Basic
 * credential, a password in a URL, a secret in a query string), and in JSON, YAML or TOML text,
 * the value of every field whose name says it holds a credential. {@link #credentialsData} masks
 * what that text parses to, and the two agree: parsing masked text gives the masked parse.
 *
 * <p>A masked value is {@code <secret:1a2b3c4d>}, the first 8 hex of the value's UTF-8 sha256:
 * deterministic, so a replay re-derives the same mask, and distinct, so two different keys stay
 * two different masks. Both functions are idempotent (a mask is never masked again), which a
 * boundary's scrub requires. A value under a credential field is kept when it is empty, a variable
 * reference ({@code ${TOKEN}}, {@code {env:TOKEN}}, {@code %TOKEN%}) or already a mask.
 *
 * <p>Every runtime masks the same text to the same bytes ({@code spec/fixtures/credentials.json}).
 * The patterns are ASCII, which is Java's default for {@code \w}, {@code \s} and case-insensitive
 * matching; {@code \b} is spelled as a lookbehind because before JDK 19 Java's {@code \b} counted
 * a non-ASCII letter as a word character; and {@link Pattern#UNIX_LINES} makes {@code .} stop at
 * {@code \n} only, as Python's does, rather than also at {@code \r} and Unicode line separators.
 */
public final class Scrubs {

    private Scrubs() {}

    /** What a masked value looks like. A value matching it whole is never masked again. */
    public static final Pattern MASK = Pattern.compile("<secret:[0-9a-f]{8}>");

    /** A field whose name says it holds a credential. ASCII case-insensitive. */
    static final Pattern SECRET_FIELD = Pattern.compile(
            "key(?!words?|board)|secret|token|passw|auth(?!or(?!i))|credential|cookie|session|bearer",
            Pattern.CASE_INSENSITIVE);

    /** A variable reference names a secret without holding it. Matched whole. */
    private static final Pattern REFERENCE = Pattern.compile(
            "(?:\\$\\{(?:env:)?\\w+\\}|\\{env:\\w+\\}|%\\w+%|\\$(?:env:)?\\w+)");

    /** A delimited reference opening an unquoted value ({@code TOKEN=${MY_TOKEN}}): the value
     *  pattern stops at its {@code }}, so it is matched where the value starts. */
    private static final Pattern REFERENCE_AT = Pattern.compile(
            "\\$\\{(?:env:)?\\w+\\}|\\{env:\\w+\\}|%\\w+%");

    /**
     * Credential shapes, wherever they appear. Group {@code v} is the part masked; the rest is
     * context that says it is a credential ({@code Bearer }, {@code ://user:}…{@code @},
     * {@code ?api_key=}). Usable as they are with {@link Boundary#forbidden(Collection)}.
     */
    public static final List<String> CREDENTIAL_SHAPES = List.of(
            "(?<![\\w-])(?<v>sk-(?:ant-)?[A-Za-z0-9_-]{20,})",
            "(?<![\\w-])(?<v>gh[pousr]_[A-Za-z0-9]{20,})",
            "(?<![\\w-])(?<v>github_pat_[A-Za-z0-9_]{20,})",
            "(?<![\\w-])(?<v>glpat-[A-Za-z0-9_-]{20,})",
            "(?<![A-Za-z0-9])(?<v>AKIA[0-9A-Z]{16})(?![A-Za-z0-9])",
            "(?<![\\w-])(?<v>xox[abpr]-[A-Za-z0-9-]{10,})",
            "(?<![\\w-])(?<v>eyJ[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,}\\.[A-Za-z0-9_-]{10,})",
            // with a digit in it: "use a Bearer token" is prose, not a credential
            "(?<!\\w)(?:Bearer|Basic)\\s+(?<v>(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{8,})",
            // a password in a URL, unless it is a reference
            "://[^:@/\\s\"'<>\\\\]+:(?<v>(?![$%{])[^:@/\\s\"'<>\\\\]+)@",
            "[?&][\\w-]*(?:key|secret|token|passw|auth|session|sig)[\\w-]*=(?<v>[^&\\s\"'<>#\\\\]+)");

    private static final List<Pattern> SHAPES = compile(CREDENTIAL_SHAPES);

    /**
     * A double-quoted string: Python's {@code "(?:[^"\\\n]|\\.)*"}, unrolled. The two match the
     * same text (a body ends only at its first unescaped quote, so there is nothing to backtrack
     * into), but Java's engine recurses once per iteration of a group loop: the alternation form
     * overflowed the stack on a JSON string a few thousand characters long, and this one recurses
     * once per escape instead of once per character.
     */
    private static final String QUOTED = "\"[^\"\\\\\\n]*+(?:\\\\.[^\"\\\\\\n]*+)*+\"";

    /** A {@code key: "value"} (JSON, YAML) or {@code key = "value"} (TOML) pair in text. */
    private static final Pattern PAIR = Pattern.compile(
            // A quoted key never opens at an escaped quote: if every \" of a long string of escaped
            // JSON could open one running to the string's end, the pass would be quadratic.
            "(?<k>(?<!\\\\)" + QUOTED + "|'[^'\\n]*'|(?<![\\w.-])[A-Za-z0-9_.-]+)(?<sep>\\s*[:=]\\s*)"
            + "(?<v>" + QUOTED + "|'[^'\\n]*')",
            Pattern.UNIX_LINES);

    /**
     * The same pair written inside a JSON string, at any depth: {@code \"KEY\": \"value\"} is a
     * JSON document held in a string (a tool result in a Claude Code transcript),
     * {@code \\\"KEY\\\": ...} that string held in another. All four quotes carry the same run of
     * backslashes. Key and value hold no quote, backslash or line break, so the value in the text
     * is the value in its parse, and both mask to the same {@code <secret:...>}.
     */
    private static final Pattern ESCAPED_PAIR = Pattern.compile(
            "(?<!\\\\)(?<q>\\\\+)\"(?<k>[^\"\\\\\\n]+)\\k<q>\"(?<sep>[ \\t]*[:=][ \\t]*)"
            + "(?<q2>\\k<q>)\"(?<v>[^\"\\\\\\n]*)\\k<q>\"",
            Pattern.UNIX_LINES);

    /** ...and with a bare key: {@code KEY=\"value\"}, {@code password: \"value\"}, a dotenv or
     *  YAML line held in a JSON string. */
    private static final Pattern ESCAPED_VALUE = Pattern.compile(
            "(?<![\\w.-])(?<k>[A-Za-z0-9_.-]+)(?<sep>[ \\t]*[:=][ \\t]*)"
            + "(?<q>\\\\+)\"(?<v>[^\"\\\\\\n]*)\\k<q>\"",
            Pattern.UNIX_LINES);

    /**
     * An unquoted assignment: {@code KEY=value}, {@code export KEY=value}, {@code set KEY=value},
     * {@code $env:KEY = value}, a .env line, a {@code --password=...} flag. The value runs to
     * whitespace or the end of the line, and stops at a quote, a backslash (where a JSON escape
     * such as {@code \n} begins), or {@code &;|<>,)}]}. A value starting with {@code =} is a
     * comparison ({@code token == x}), not an assignment. Quoted values are the pair's.
     */
    private static final Pattern ASSIGN = Pattern.compile(
            "(?<![\\w.-])(?<k>[A-Za-z0-9_.-]+)(?<sep>[ \\t]*=[ \\t]*)"
            + "(?<v>(?!=)[^\\s\"'`\\\\\\&;|<>,)}\\]]+)",
            Pattern.UNIX_LINES);

    private static List<Pattern> compile(List<String> patterns) {
        List<Pattern> out = new ArrayList<>(patterns.size());
        for (String p : patterns) out.add(Pattern.compile(p, Pattern.UNIX_LINES));
        return List.copyOf(out);
    }

    /** The mask of one value: {@code <secret:} + the first 8 hex of its UTF-8 sha256 + {@code >}. */
    public static String secret(String value) {
        byte[] digest;
        try {
            digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("every Java platform ships SHA-256", e);
        }
        StringBuilder hex = new StringBuilder("<secret:");
        for (int i = 0; i < 4; i++) hex.append(String.format("%02x", digest[i]));
        return hex.append('>').toString();
    }

    /** Whether a field of this name holds a credential, unless the app exempted the name. */
    public static boolean secretField(String name, Collection<String> exempt) {
        // A name holding a mask is a value an earlier pass masked, read back as the key of the next
        // pair (`token="<secret:...>": "v"`); `secret` in the mask is not the field saying anything.
        return !exempt.contains(name) && !MASK.matcher(name).find() && SECRET_FIELD.matcher(name).find();
    }

    /** A value left as it is under a credential field: empty, a reference, already a mask. */
    static boolean keeps(String value) {
        // A mask ANYWHERE, not only the whole value: a reference kept on a first pass ($AKIA...)
        // can have a shape masked inside it, and what is left ($<secret:...>) is no longer a
        // reference but must not be masked again, or the scrub is not a no-op on its own output.
        return value.isEmpty() || REFERENCE.matcher(value).matches() || MASK.matcher(value).find();
    }

    /** A quoted token's content: a single-quoted one as it stands, a double-quoted one as JSON
     *  reads it, or as it stands when it is not valid JSON. */
    static String unquote(String token) {
        String inner = token.substring(1, token.length() - 1);
        if (token.charAt(0) == '\'') return inner;
        String json = jsonString(inner);
        return json == null ? inner : json;
    }

    /** The body of a JSON string decoded strictly, or null when JSON would refuse it: a raw
     *  control character, an unknown escape, or a {@code \\u} without four hex digits. */
    private static String jsonString(String body) {
        StringBuilder out = new StringBuilder(body.length());
        int n = body.length();
        for (int i = 0; i < n; i++) {
            char c = body.charAt(i);
            if (c < 0x20) return null;
            if (c == '"') return null;
            if (c != '\\') { out.append(c); continue; }
            if (++i >= n) return null;
            char e = body.charAt(i);
            switch (e) {
                case '"' -> out.append('"');
                case '\\' -> out.append('\\');
                case '/' -> out.append('/');
                case 'b' -> out.append('\b');
                case 'f' -> out.append('\f');
                case 'n' -> out.append('\n');
                case 'r' -> out.append('\r');
                case 't' -> out.append('\t');
                case 'u' -> {
                    if (i + 4 >= n) return null;
                    int code = 0;
                    for (int j = 1; j <= 4; j++) {
                        int d = Character.digit(body.charAt(i + j), 16);
                        if (d < 0 || body.charAt(i + j) > 0x7f) return null;
                        code = code * 16 + d;
                    }
                    out.append((char) code);
                    i += 4;
                }
                default -> { return null; }
            }
        }
        return out.toString();
    }

    private static String shapes(String text) {
        for (Pattern shape : SHAPES) {
            Matcher m = shape.matcher(text);
            if (!m.find()) continue;
            StringBuilder out = new StringBuilder(text.length());
            int last = 0;
            do {
                out.append(text, last, m.start("v")).append(secret(m.group("v")));
                last = m.end("v");
            } while (m.find());
            text = out.append(text, last, text.length()).toString();
        }
        return text;
    }

    /** {@code text} with every credential shape and every credential field's value masked. */
    public static String credentials(String text) {
        return credentials(text, Set.of());
    }

    /** {@code text} with every credential shape masked, and every credential field's value but
     *  those of the fields named in {@code exempt}. */
    public static String credentials(String text, Collection<String> exempt) {
        if (text == null) return null;
        Set<String> ex = Set.copyOf(exempt);
        // The passes run until a round changes nothing. A later pass can change how an earlier one
        // reads the text (a shape masked inside a key frees the pair after it), so one round is
        // not idempotent on dense input; a fixed point makes a second scrub a no-op by
        // construction, as in the Python reference.
        while (true) {
            String before = text;
            text = round(text, ex);
            if (text.equals(before)) return text;
        }
    }

    /** One round of the five passes: pair, escaped pair, escaped value, assignment, shapes. */
    private static String round(String text, Set<String> ex) {
        text = chained(PAIR, "v", text, m -> {
            String k = m.group("k");
            // A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
            String name = (k.charAt(0) == '"' || k.charAt(0) == '\'') ? unquote(k) : lastPart(k);
            String quoted = m.group("v");
            String value = unquote(quoted);
            if (!secretField(name, ex) || keeps(value)) return null;
            char quote = quoted.charAt(0);
            return k + m.group("sep") + quote + secret(value) + quote;
        });
        text = chained(ESCAPED_PAIR, "q2", text, m -> {
            String value = m.group("v"), q = m.group("q");
            if (!secretField(m.group("k"), ex) || keeps(value)) return null;
            return q + "\"" + m.group("k") + q + "\"" + m.group("sep") + q + "\"" + secret(value) + q + "\"";
        });
        text = sub(ESCAPED_VALUE, text, m -> {
            String value = m.group("v"), q = m.group("q");
            if (!secretField(lastPart(m.group("k")), ex) || keeps(value)) return null;
            return m.group("k") + m.group("sep") + q + "\"" + secret(value) + q + "\"";
        });
        text = assignments(text, ex);
        return shapes(text);
    }

    /**
     * The unquoted assignments masked, scanning left to right.
     *
     * <p>Unlike the other passes, an assignment left as it is gives its value back to the scan:
     * the search resumes right after its name, not after its value. In {@code a=token=x} the value
     * of {@code a} holds an assignment of its own, and in {@code AKIA...=Bearer = v} a shape masked
     * later turns the first name into a mask and frees {@code Bearer = v}; a scan that had
     * swallowed it would mask it only on a second pass. A value directly followed by a mask is the
     * rest of a value already masked ({@code $env:<secret:...>}) and is kept.
     */
    private static String assignments(String text, Set<String> exempt) {
        Matcher m = ASSIGN.matcher(text);
        Matcher mask = MASK.matcher(text);
        Matcher ref = REFERENCE_AT.matcher(text);
        StringBuilder out = new StringBuilder(text.length());
        int pos = 0;
        // find(int) resets the region to the whole text, so the lookbehind still sees what
        // precedes pos, as Python's pattern.search(text, pos) does.
        while (m.find(pos)) {
            String value = m.group("v");
            int vs = m.start("v"), ve = m.end("v");
            if (!secretField(lastPart(m.group("k")), exempt) || keeps(value)
                    || mask.region(ve, text.length()).lookingAt()
                    || ref.region(vs, text.length()).lookingAt()) {
                out.append(text, pos, m.end("k"));
                pos = m.end("k");
                continue;
            }
            out.append(text, pos, vs).append(secret(value));
            pos = ve;
        }
        return out.append(text, pos, text.length()).toString();
    }

    private static String lastPart(String dotted) {
        return dotted.substring(dotted.lastIndexOf('.') + 1);
    }

    /**
     * {@link #sub}, except that a pair left as it is ({@code fn} returns null) gives its value back
     * when that value opens a pair of its own.
     *
     * <p>In {@code x = "api_key": "v1"} the pair {@code x = "api_key"} is not a credential, and a
     * scan that resumed after its value never saw {@code "api_key": "v1"}. So the scan tries one
     * match anchored at the value's start, and only if none opens there does it skip the value
     * whole: resuming inside a quoted value would read its escaped quotes as the openings of new
     * tokens. {@code valueGroup} names the group where the value's token begins.
     */
    private static String chained(Pattern p, String valueGroup, String text,
                                  java.util.function.Function<Matcher, String> fn) {
        Matcher m = p.matcher(text);
        StringBuilder out = new StringBuilder(text.length());
        int pos = 0, anchor = -1, skip = 0;
        while (true) {
            boolean found;
            if (anchor >= 0) {
                // Python's pattern.match(text, anchor): anchored there, lookbehind still sees
                // what precedes it.
                m.region(anchor, text.length()).useTransparentBounds(true).useAnchoringBounds(false);
                found = m.lookingAt();
            } else {
                found = m.find(pos);
            }
            if (!found) {
                if (anchor >= 0) {
                    anchor = -1;
                    out.append(text, pos, skip);
                    pos = skip;
                    continue;
                }
                return out.append(text, pos, text.length()).toString();
            }
            anchor = -1;
            String replaced = fn.apply(m);
            if (replaced == null) {
                int vs = m.start(valueGroup);
                out.append(text, pos, vs);
                pos = anchor = vs;
                skip = m.end();
                continue;
            }
            out.append(text, pos, m.start()).append(replaced);
            pos = m.end();
        }
    }

    /** Python's {@code pattern.sub(fn, text)}: each match replaced by what {@code fn} returns,
     *  or left as it is when {@code fn} returns null. */
    private static String sub(Pattern p, String text, java.util.function.Function<Matcher, String> fn) {
        Matcher m = p.matcher(text);
        StringBuilder out = null;
        int last = 0;
        while (m.find()) {
            String r = fn.apply(m);
            if (r == null) continue;
            if (out == null) out = new StringBuilder(text.length());
            out.append(text, last, m.start()).append(r);
            last = m.end();
        }
        if (out == null) return text;
        return out.append(text, last, text.length()).toString();
    }

    /** {@link #credentials} for parsed data. */
    public static Object credentialsData(Object value) {
        return credentialsData(value, Set.of(), null);
    }

    /** {@link #credentials} for parsed data, with the fields named in {@code exempt} kept. */
    public static Object credentialsData(Object value, Collection<String> exempt) {
        return credentialsData(value, exempt, null);
    }

    /**
     * {@link #credentials} for parsed data: a string under a credential field ({@code key}) is
     * masked whole, any other string as text. Maps and sequences are walked into new ones; other
     * scalars are kept.
     */
    public static Object credentialsData(Object value, Collection<String> exempt, String key) {
        Set<String> ex = exempt instanceof Set<String> s ? s : Set.copyOf(exempt);
        if (value instanceof Map<?, ?> m) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<?, ?> e : m.entrySet()) {
                String k = String.valueOf(e.getKey());
                out.put(k, credentialsData(e.getValue(), ex, k));
            }
            return out;
        }
        if (value instanceof Collection<?> c) {
            List<Object> out = new ArrayList<>(c.size());
            for (Object x : c) out.add(credentialsData(x, ex, null));
            return out;
        }
        if (value instanceof Object[] a) {
            List<Object> out = new ArrayList<>(a.length);
            for (Object x : a) out.add(credentialsData(x, ex, null));
            return out;
        }
        if (value instanceof String s) {
            if (key != null && secretField(key, ex) && !keeps(s)) return secret(s);
            return credentials(s, ex);
        }
        return value;
    }
}
