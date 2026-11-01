package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.*;

/**
 * The stock credential scrub, checked byte for byte against the vectors every runtime checks
 * ({@code spec/fixtures/credentials.json}, written by the Python implementation), then used as
 * what it is for: a boundary's scrub and forbid, on a tape and on a trace.
 */
class ScrubsTest {

    private static final Path VECTORS = Paths.get("..", "spec", "fixtures", "credentials.json");

    // Built by concatenation, so this file is not one a scanner reads as carrying a live key.
    private static final String PROSE_KEY = "sk-" + "proj-0123456789abcdefghijklmnop";

    private static Map<String, Object> vectors() throws Exception {
        return Json.asMap(Json.parse(Files.readString(VECTORS, StandardCharsets.UTF_8)));
    }

    private static List<Map<String, Object>> cases() throws Exception {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object c : Json.asList(vectors().get("cases"))) out.add(Json.asMap(c));
        assertFalse(out.isEmpty(), "the vectors must hold cases");
        return out;
    }

    private static List<String> exempt(Map<String, Object> c) {
        List<String> out = new ArrayList<>();
        for (Object x : Json.asList(c.get("exempt"))) out.add((String) x);
        return out;
    }

    @Test
    void everyVectorMasksToTheSameBytesAsEveryOtherRuntime() throws Exception {
        assertEquals(vectors().get("mask"), Scrubs.MASK.pattern());
        int texts = 0, data = 0;
        for (Map<String, Object> c : cases()) {
            if (c.containsKey("text")) {
                assertEquals(c.get("masked"), Scrubs.credentials((String) c.get("text"), exempt(c)),
                        "text case, exempt " + exempt(c));
                texts++;
            } else {
                Object got = Scrubs.credentialsData(c.get("data"), exempt(c));
                assertEquals(Json.canonical(c.get("masked_data")), Json.canonical(got), "data case, exempt " + exempt(c));
                data++;
            }
        }
        assertTrue(texts > 0 && data > 0);
    }

    @Test
    void aMaskIsNeverMaskedAgain() throws Exception {
        for (Map<String, Object> c : cases()) {
            if (c.containsKey("text")) {
                String once = Scrubs.credentials((String) c.get("text"), exempt(c));
                assertEquals(once, Scrubs.credentials(once, exempt(c)));
            } else {
                Object once = Scrubs.credentialsData(c.get("data"), exempt(c));
                assertEquals(Json.canonical(once), Json.canonical(Scrubs.credentialsData(once, exempt(c))));
            }
        }
    }

    @Test
    void theShapesAsForbidPassAScrubbedText() throws Exception {
        // The recommended pairing: whatever the scrub leaves, no shape fires on, so declaring both
        // never refuses a recording the scrub already cleaned. Compiled as Boundary.forbidden
        // compiles them, with no flags.
        List<Pattern> shapes = Scrubs.CREDENTIAL_SHAPES.stream().map(Pattern::compile).toList();
        for (Map<String, Object> c : cases()) {
            if (!c.containsKey("text")) continue;
            String masked = Scrubs.credentials((String) c.get("text"), exempt(c));
            for (Pattern p : shapes) assertFalse(p.matcher(masked).find(), p.pattern() + " fired on " + masked);
        }
        assertTrue(shapes.stream().anyMatch(p -> p.matcher("{\"note\": \"" + PROSE_KEY + "\"}").find()));
    }

    /** Edges the vectors do not reach, decided by the Python implementation. */
    @Test
    void theEdgesOfTheRegexDialectAreDecidedAsPythonDecidesThem() {
        String[][] edges = {
                // \s includes \x0B (Python's \v)
                {"Bearer\u000babc12345678", "Bearer\u000b<secret:e06787ae>"},
                // \b is ASCII: a non-ASCII letter is not a word character
                {"éBearer abc12345678", "éBearer <secret:e06787ae>"},
                {"xésk-ant-api03-abcdefghijklmnopqrstu", "xé<secret:dad3d2e6>"},
                // a double-quoted value is read as JSON: escapes decoded, a surrogate pair whole
                {"{\"token\": \"\\ud83d\\ude00x\"}", "{\"token\": \"<secret:10f5e9cd>\"}"},
                // ...and as it stands when JSON would refuse it
                {"{\"token\": \"x\\qy\"}", "{\"token\": \"<secret:e8a16705>\"}"},
                {"{\"token\": \"a\tb\"}", "{\"token\": \"<secret:894891f8>\"}"},
                {"{\"t\\u006fken\": \"zz\"}", "{\"t\\u006fken\": \"<secret:4a60bf7d>\"}"},
                {"key = 'v1' # c", "key = '<secret:3bfc2695>' # c"},
                {"Token: \"abc\"\r\n", "Token: \"<secret:ba7816bf>\"\r\n"},
                // `.` stops at \n only: an escaped \r is still inside the string
                {"password: \"x\\\ry\"", "password: \"<secret:e80bd008>\""},
                {"api.secret = 'q'", "api.secret = '<secret:8e35c2cd>'"},
                {"\"auth x\": \"zz\"", "\"auth x\": \"<secret:4a60bf7d>\""},
        };
        for (String[] e : edges) assertEquals(e[1], Scrubs.credentials(e[0]), e[0]);
        assertEquals("<secret:ba7816bf>", Scrubs.secret("abc"));
        assertEquals(Json.parse("{\"a\":[{\"token\":\"<secret:2d711642>\"},\"Bearer <secret:e06787ae>\"],"
                        + "\"token\":5,\"session\":{\"k\":\"v\"}}"),
                Scrubs.credentialsData(Json.parse("{\"a\":[{\"token\":\"x\"},\"Bearer abc12345678\"],"
                        + "\"token\":5,\"session\":{\"k\":\"v\"}}")));
    }

    @Test
    void anExemptedFieldIsKeptButNotTheShapeInsideIt() {
        List<String> exempt = List.of("key", "token_env", "auth");
        assertEquals(Map.of("key", "mcpServers"), Scrubs.credentialsData(Map.of("key", "mcpServers"), exempt));
        assertNotEquals(Map.of("key", "mcpServers"), Scrubs.credentialsData(Map.of("key", "mcpServers")));
        String sk = "sk-ant-" + "api03-abcdefghijklmnopqrstu";
        assertFalse(Json.write(Scrubs.credentialsData(Map.of("key", sk), exempt)).contains(sk));
    }

    // ------------- what actually leaked: a pair inside a JSON string, and unquoted assignments
    //
    // The domain is tests/test_scrubs.py's, so the two runtimes are checked over the same values.

    private static final List<String> KEYS = List.of("ALPACA_API_KEY_ID", "api_token", "Authorization",
            "password", "session", "key", "token_env", "auth", "author", "keywords", "command", "url", "DB");
    private static final List<String> EXEMPT = List.of("key", "token_env", "auth");
    private static final List<String> VALUES = List.of("PKRAW0001", "s3cr\"et\\x", "Bearer abc123def456",
            "sk-ant-" + "api03-abcdefghijklmnopqrstu", "ghp_" + "abcdefghijklmnopqrstuvwxyz0123",
            "AKIA" + "ABCDEFGHIJKLMNOP", "postgres://u:pw1234@h/db", "https://x.io/mcp?api_key=abc123&x=1",
            "${MY_TOKEN}", "{env:MY_TOKEN}", "$MY_TOKEN", "", "mcpServers", "use a Bearer token", "\u00e9",
            "<secret:0123abcd>", "curl \"https://x.io/?token=abc123\" -s", "x://u:ab\\c@h",
            "curl \"https://u:pw1234@h/x\" -s");

    /** Python's {@code json.dumps(s, ensure_ascii=False)} for a string. */
    private static String dumps(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                case '\b' -> b.append("\\b");
                case '\f' -> b.append("\\f");
                default -> {
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
                }
            }
        }
        return b.append('"').toString();
    }

    private static String nest(String text, int depth) {
        for (int i = 0; i < depth; i++) text = dumps(text);
        return text;
    }

    private static boolean nestable(String v) {
        return v.indexOf('"') < 0 && v.indexOf('\\') < 0 && v.indexOf('\n') < 0;
    }

    @Test
    void aPairNestedInJsonStringsMasksAsItsParseDoes() {
        for (int depth = 1; depth <= 3; depth++) {
            for (List<String> exempt : List.of(List.<String>of(), EXEMPT)) {
                for (String k : KEYS) {
                    for (String v : VALUES) {
                        if (!nestable(v)) continue;
                        String json = "{\n  \"servers\": {\n    \"x\": {\n      " + dumps(k) + ": " + dumps(v)
                                + "\n    }\n  }\n}";
                        String toml = dumps(k) + " = " + dumps(v) + "\n";
                        for (String inner : List.of(json, toml)) {
                            String outer = nest(inner, depth);
                            String masked = Scrubs.credentials(outer, exempt);
                            assertEquals(Scrubs.credentials(nest(inner, depth - 1), exempt), Json.parse(masked), outer);
                            assertEquals(masked, Scrubs.credentials(masked, exempt), outer);
                        }
                    }
                }
            }
        }
    }

    @Test
    void theTranscriptRowThatLeakedIsMasked() {
        // A trace line read with a tool, held as the tool result's content in one JSONL row of a
        // Claude Code transcript; and that row once more inside the tape that recorded it.
        String keyValue = "PKRAW" + "0001TESTVALUE";
        String traceLine = "{\"e\": \"L\", \"d\": {\"env\": {\"ALPACA_API_SECRET_KEY\": " + dumps(keyValue)
                + ", \"PATH\": \"/usr/bin\"}}}";
        String row = "{\"type\": \"user\", \"message\": {\"content\": [{\"type\": \"tool_result\", "
                + "\"tool_use_id\": \"t1\", \"content\": " + dumps(traceLine) + "}]}}";
        for (String text : List.of(row, dumps(row))) {
            String masked = Scrubs.credentials(text);
            assertFalse(masked.contains(keyValue), "the key must be masked at every depth");
            assertTrue(masked.contains("/usr/bin"), "what is not a credential is kept");
            assertEquals(masked, Scrubs.credentials(masked));
        }
    }

    private static final List<java.util.function.BinaryOperator<String>> ASSIGNMENTS = List.of(
            (k, v) -> k + "=" + v,
            (k, v) -> "export " + k + "=" + v,
            (k, v) -> "set " + k + "=" + v,
            (k, v) -> "$env:" + k + " = " + v,
            (k, v) -> k + "=\"" + v + "\"",
            (k, v) -> k + "='" + v + "'",
            (k, v) -> "$env:" + k + " = \"" + v + "\"",
            (k, v) -> "--" + k + "=" + v);

    @Test
    void anAssignmentMasksItsValueIffTheNameHoldsACredential() {
        int masked = 0, kept = 0;
        for (String k : KEYS) {
            for (String v : VALUES) {
                if (v.isEmpty() || v.charAt(0) == '=' || v.chars().anyMatch(c -> " \t\r\n\"'`\\&;|<>,)}]".indexOf(c) >= 0)) continue;
                for (var form : ASSIGNMENTS) {
                    String text = form.apply(k, v) + " # done";
                    String expect;
                    if (Scrubs.secretField(k, List.of()) && !Scrubs.keeps(v)) {
                        expect = form.apply(k, Scrubs.secret(v)) + " # done";
                        masked++;
                    } else {
                        expect = form.apply(k, Scrubs.credentials(v)) + " # done";
                        kept++;
                    }
                    assertEquals(expect, Scrubs.credentials(text), text);
                }
            }
        }
        assertTrue(masked > 0 && kept > 0);
    }

    @Test
    void aLongJsonStringDoesNotOverflowTheRegexEngine() {
        // Java's engine recursed once per character of a quoted string under the alternation
        // form of the pattern, and a 3,400-character nested .env overflowed the stack.
        StringBuilder b = new StringBuilder("\"");
        for (int i = 0; i < 100_000; i++) b.append("API_TOKEN=v").append(i).append("\\n");
        String text = b.append('"').toString();
        String masked = assertDoesNotThrow(() -> Scrubs.credentials(text));
        assertFalse(masked.contains("=v17\\n"));
    }

    // Found by differential fuzzing: texts the scrub masked differently on a second pass. A scrub
    // must be a no-op on its own output, or a replay that re-scrubs what it read off the tape
    // compares against something else.
    private static final String AKIA = "AKIA" + "ABCDEFGHIJKLMNOP";
    private static final List<String> SECOND_PASS = List.of(
            "'" + AKIA + "=Bearer  = Bearer ,",           // a shape masked later freed an assignment
            "token = $env:" + AKIA + "\\\"KEY",            // a kept reference had a shape masked inside
            "{\"token\": \"$" + AKIA + "\"}",
            "{\"token\": \"${" + AKIA + "}\"}",
            AKIA + " = \"api_key\": \"v1\"",              // a pair left as it is swallowed the next pair's key
            "token=\"token\": \"{\"x\"",                  // a masked value read back as a key holds "secret"
            "$token='pw' = \"api_key\"Bearer",
            // a later pass changed how an earlier one read the text (the passes run to a fixed point)
            AKIA + ":\"\"key\":\"y\"",
            "token=v:\"auth:\"y\"",
            "token=-=\"\"KEY\":\"1\"",
            "session=d:\\\"passw:\\\"y\\\"",
            "token=_x=\"token=\"token\": \"\t)&{",
            "token=\"token\": \"_;x=\" token=\"token\": \"0123456789");

    /** The fragments the differential fuzzing of the ports used (tests/test_scrubs.py). */
    private static final List<String> FRAGMENTS = List.of("\\", "\\\\", "\\\"", "\\\\\\\"", "\"", "\"\"", "'", "`",
            "$", "$env:", "${X}", "%X%", "--", "==", "=", " = ", ":", ": ", "token", "secretBearer", "api_key",
            "password", "a.b.", "KEY", "v1", AKIA, "Bearer ", "\u00e9", "\n", " ", "\t", "<", ">", "&", "?",
            "://", "@", "{", "}", ")", ",", ";", "<secret:0123abcd>", "\"api_key\"", "${", "a=", "token=",
            "'pw'", "x = ", "\": \"", "\"token\": \"", "x=\"", "auth:", "passw:");

    @Test
    void theScrubIsAFixedPointOverDenseFragments() {
        java.util.Random rng = new java.util.Random(17);
        for (int i = 0; i < 20_000; i++) {
            StringBuilder b = new StringBuilder();
            int n = 1 + rng.nextInt(16);
            for (int j = 0; j < n; j++) b.append(FRAGMENTS.get(rng.nextInt(FRAGMENTS.size())));
            String text = b.toString();
            List<String> exempt = i % 2 == 1 ? List.of("password") : List.of();
            String once = Scrubs.credentials(text, exempt);
            assertEquals(once, Scrubs.credentials(once, exempt), text);
        }
    }

    @Test
    void aPairLeftAsItIsGivesBackAValueThatOpensAPair() {
        assertEquals("x = \"api_key\": \"" + Scrubs.secret("v1") + "\"", Scrubs.credentials("x = \"api_key\": \"v1\""));
        String e = "\\\"";
        String text = e + "x" + e + ": " + e + "api_key" + e + ": " + e + "v1" + e;
        assertFalse(Scrubs.credentials(text).contains("v1"));
    }

    @Test
    void aLongStringOfEscapedJsonScrubsInLinearTime() {
        // A transcript is one long string of escaped JSON. When every escaped quote could open a
        // quoted token running to the string's end, the pass was quadratic.
        java.util.Random rng = new java.util.Random(3);
        String alphabet = "\"ab :";
        StringBuilder inner = new StringBuilder();
        for (int i = 0; i < 50_000; i++) inner.append(alphabet.charAt(rng.nextInt(alphabet.length())));
        Scrubs.credentials(dumps("warm up the patterns"));
        for (String text : List.of(dumps(inner.toString()), dumps(dumps(inner.toString())))) {
            long t = System.nanoTime();
            Scrubs.credentials(text);
            long ms = (System.nanoTime() - t) / 1_000_000;
            assertTrue(ms < 1000, "took " + ms + " ms for " + text.length() + " chars");
        }
    }

    @Test
    void theScrubIsANoOpOnItsOwnOutput() {
        for (String text : SECOND_PASS) {
            String once = Scrubs.credentials(text);
            assertEquals(once, Scrubs.credentials(once), text);
            assertFalse(once.contains(AKIA), text);
        }
    }

    @Test
    void anUnchangedAssignmentGivesItsValueBackToTheScan() {
        assertEquals("a=token=" + Scrubs.secret("abc123"), Scrubs.credentials("a=token=abc123"));
    }

    @Test
    void aReferenceOpeningAnUnquotedValueIsKept() {
        for (String text : List.of("TOKEN=${MY_TOKEN}", "TOKEN={env:MY_TOKEN}", "TOKEN=%MY_TOKEN%", "TOKEN=$MY_TOKEN")) {
            assertEquals(text, Scrubs.credentials(text));
        }
    }

    @Test
    void aComparisonIsNotAnAssignment() {
        for (String text : List.of("if token == other:", "token != x", "password==hash(p)", "token<=3")) {
            assertEquals(text, Scrubs.credentials(text));
        }
    }

    // ------------------------------------------- as a Boundary declares it

    @Test
    void aBoundaryDeclaringTheStockScrubWritesNoCredential(@TempDir Path tmp) throws Exception {
        Boundary b = new Boundary()
                .scrubbingWith(Scrubs::credentials)
                .forbidden(Scrubs.CREDENTIAL_SHAPES);

        try (Recorder rec = Recorder.open(tmp.toString(), b)) {
            Object out = rec.call("leak", Recorder.kwargs("note", "use " + PROSE_KEY + " here"), () -> "ok");
            assertEquals("ok", out);
            String tape = Files.readString(Path.of(rec.path()), StandardCharsets.UTF_8);
            assertFalse(tape.contains(PROSE_KEY));
            assertTrue(Scrubs.MASK.matcher(tape).find(), "the tape carries the mask");
        }

        Tracer.Run run = Tracer.run(List.of("src/test/resources/traced/TracedToy.java"),
                "TracedToy", "holdsASecret", b, PROSE_KEY);
        String trace = run.trace().toJsonl();
        assertFalse(trace.contains(PROSE_KEY));
        assertEquals(Scrubs.secret(PROSE_KEY), run.trace().last("secret").value());
        assertEquals("Bearer " + Scrubs.secret(PROSE_KEY), run.trace().last("header").value());

        try (Stream<Path> files = Files.walk(tmp)) {
            for (Path p : files.filter(Files::isRegularFile).toList()) {
                assertFalse(Files.readString(p, StandardCharsets.UTF_8).contains(PROSE_KEY), p.toString());
            }
        }
    }
}
