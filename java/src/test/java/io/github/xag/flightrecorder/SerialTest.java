package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/** The boundary value codec, and the two redaction layers. */
class SerialTest {

    @Test
    void awarenessIsPartOfTheValue() {
        // A naive datetime and an aware one are not interchangeable — they compare and format
        // differently — so a codec that normalised one to the other would change behaviour on
        // replay, which is the one thing replay may never do.
        LocalDateTime naive = LocalDateTime.of(2026, 7, 18, 10, 30, 0);
        OffsetDateTime aware = OffsetDateTime.parse("2026-07-18T10:30:00+02:00");

        Map<String, Object> n = Json.asMap(Serial.toJsonable(naive));
        Map<String, Object> a = Json.asMap(Serial.toJsonable(aware));
        assertEquals("2026-07-18T10:30:00", n.get("__dt__"));
        assertEquals("2026-07-18T10:30:00+02:00", a.get("__dt__"));

        assertEquals(naive, Serial.fromJsonable(n));
        assertEquals(aware, Serial.fromJsonable(a));
    }

    @Test
    void aDateIsItsOwnMarker() {
        Map<String, Object> d = Json.asMap(Serial.toJsonable(LocalDate.of(2026, 7, 18)));
        assertEquals("2026-07-18", d.get("__date__"));
        assertEquals(LocalDate.of(2026, 7, 18), Serial.fromJsonable(d));
    }

    @Test
    void undefinedRevivesToNullEvenThoughJavaNeverEmitsIt() {
        // Only the JS runtime writes __undef__. Java has no way to hold "undefined" distinctly, and
        // inventing one would be a worse lie than collapsing it onto null.
        assertNull(Serial.fromJsonable(Map.of("__undef__", true)));
    }

    @Test
    void nanAndInfinityDegradeRatherThanBreakingTheLine() {
        assertTrue(Json.asMap(Serial.toJsonable(Double.NaN)).containsKey("__opaque__"));
        assertTrue(Json.asMap(Serial.toJsonable(Double.POSITIVE_INFINITY)).containsKey("__opaque__"));
    }

    @Test
    void bytesAreEntropyNotStructure() {
        Map<String, Object> m = Json.asMap(Serial.toJsonable(new byte[]{0x01, (byte) 0xff, 0x10}));
        assertEquals("<bytes 3: 01ff10>", m.get("__opaque__"));
    }

    @Test
    void anOpaqueMarkerIsCappedAndCarriesNoIdentity() {
        Object encoded = Serial.toJsonable(new Object() {
            @Override public String toString() { return "x".repeat(500); }
        });
        String s = (String) Json.asMap(encoded).get("__opaque__");
        assertTrue(s.length() <= 200, "opaque payloads are capped at 200: " + s.length());
    }

    @Test
    void aMemoryAddressIsScrubbedBecauseItIsIdentityNotValue() {
        // A default toString() is ClassName@1b6d3586. The address differs on every run, so
        // recording it would make the effect it belongs to never match on replay.
        Object encoded = Serial.toJsonable(new Object());
        String s = (String) Json.asMap(encoded).get("__opaque__");
        assertFalse(s.matches(".*@[0-9a-fA-F]+.*"), s);
    }

    @Test
    void deeplyNestedValuesDegradeRatherThanRecursingForever() {
        Object nested = "leaf";
        for (int i = 0; i < 40; i++) nested = List.of(nested);
        // Encoding must terminate and must not throw; the tape gets poorer, the app stays fine.
        assertNotNull(Json.write(Serial.toJsonable(nested)));
    }

    @Test
    void anObjectsPublicSurfaceIsCamelCasedSoFieldRulesReachIt() {
        record Account(String userName, String password) {}
        Map<String, Object> m = Json.asMap(Serial.toJsonable(new Account("alice", "hunter2")));
        assertEquals("alice", m.get("userName"));
        // A codec that emitted "Password" would route straight past a rule declared as "password".
        assertTrue(m.containsKey("password"));
    }

    @Test
    void aGetterThatThrowsDegradesInsteadOfBreakingTheCall() {
        class Hostile {
            public String getBoom() { throw new IllegalStateException("no"); }
        }
        assertTrue(Json.asMap(Serial.toJsonable(new Hostile())).containsKey("__opaque__"));
    }

    // ------------------------------------------------------------- redaction

    @Test
    void layerOneMasksByFieldNameWhereverItSits() {
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("outer", Map.of("inner", Map.of("password", "hunter2")));
        Boundary b = new Boundary().maskFields("password");

        String out = Json.write(Serial.redact(Serial.toJsonable(tree), b.redact, b.scrub));
        assertFalse(out.contains("hunter2"));
        assertTrue(out.contains("[REDACTED]"));
    }

    @Test
    void layerTwoSweepsEveryLeafStringIncludingPositionalArgumentsAndProse() {
        Boundary b = new Boundary().scrubbing("sk-[A-Za-z0-9]+");
        Object tree = Serial.toJsonable(List.of(
                "sk-abc123",                                   // a positional argument
                Map.of("note", "the key is sk-def456, keep it safe")));  // prose mid-sentence

        String out = Json.write(Serial.redact(tree, b.redact, b.scrub));
        assertFalse(out.contains("sk-abc123"));
        assertFalse(out.contains("sk-def456"));
    }

    @Test
    void objectKeysAreNotSweptSoTapesStayComparableAcrossRuntimes() {
        Boundary b = new Boundary().scrubbing("secret");
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("secret", "value");
        Map<String, Object> out = Json.asMap(Serial.redact(Serial.toJsonable(tree), b.redact, b.scrub));
        assertTrue(out.containsKey("secret"), "the KEY is structure, not content");
    }

    @Test
    void aFieldRulesOwnOutputAlsoMeetsTheSweep() {
        // A transform that shortens rather than masks must not be able to smuggle the secret past.
        Boundary b = new Boundary()
                .redacting("token", v -> "sk-" + v)     // deliberately re-introduces the shape
                .scrubbing("sk-[A-Za-z0-9]+");
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("token", "abc123");
        String out = Json.write(Serial.redact(Serial.toJsonable(tree), b.redact, b.scrub));
        assertFalse(out.contains("sk-abc123"));
    }

    @Test
    void aRuleThatThrowsDegradesToRedactedRatherThanLeaking() {
        Boundary b = new Boundary().redacting("password", v -> { throw new RuntimeException("nope"); });
        Map<String, Object> tree = new LinkedHashMap<>();
        tree.put("password", "hunter2");
        String out = Json.write(Serial.redact(Serial.toJsonable(tree), b.redact, b.scrub));
        assertFalse(out.contains("hunter2"), "the failure direction is masked, never leaked");
        assertTrue(out.contains("[REDACTED]"));
    }

    @Test
    void scrubbingIsIdempotentAndAMaskThatMatchesItsPatternIsRefused() {
        // Replay re-derives the question, scrubs it the same way, and compares against the tape —
        // so a mask that matches its own pattern would mask the mask on the second pass and report
        // a divergence on a value that never changed. Refuse it at declaration time.
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> new Boundary().scrubbing("[A-Z]+", "REDACTED"));
        assertTrue(e.getMessage().contains("idempotent"), e.getMessage());
    }

    @Test
    void scrubbingStacksSoEachSecretShapeGetsItsOwnLine() {
        Boundary b = new Boundary()
                .scrubbing("sk-[a-z0-9]+")
                .scrubbing("ghp_[A-Za-z0-9]+");
        Object tree = Serial.toJsonable(List.of("sk-abc123", "ghp_XYZ789"));
        String out = Json.write(Serial.redact(tree, b.redact, b.scrub));
        assertFalse(out.contains("sk-abc123"));
        assertFalse(out.contains("ghp_XYZ789"));
    }

    // ------------------------------------------------------------------ json

    @Test
    void integersAndFloatsAreDistinguishedOnTheWayIn() {
        // The checker must be able to reject `"seq": 1.0` where an int is required.
        assertTrue(Json.isInt(Json.parse("1")));
        assertFalse(Json.isInt(Json.parse("1.0")));
        assertTrue(Json.isNumber(Json.parse("1.0")));
    }

    @Test
    void canonicalFormMakesThirtyEqualThirtyPointZeroAndIgnoresKeyOrder() {
        assertTrue(Json.equal(Json.parse("30"), Json.parse("30.0")));
        assertTrue(Json.equal(Json.parse("{\"a\":1,\"b\":2}"), Json.parse("{\"b\":2,\"a\":1}")));
        assertFalse(Json.equal(Json.parse("30"), Json.parse("31")));
    }

    @Test
    void aRoundTripSurvivesEscapesAndUnicode() {
        String s = "line\nbreak \"quoted\" \\ backslash — é 日本";
        assertEquals(s, Json.parse(Json.write(s)));
    }
}
