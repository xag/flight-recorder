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
