package io.github.xag.flightrecorder;

import io.github.xag.flightrecorder.spec.Validate;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The Java runtime's contribution to the cross-runtime fixture sweep.
 *
 * <p>Two scenarios, recorded and checked against the checker. The generator is env-gated, because
 * these fixtures are bytes under version control and a test that rewrote them on every run would
 * turn "the fixtures changed" into noise nobody reads.
 */
class FixturesTest {

    private static final Path FIXTURES = Paths.get("..", "spec", "fixtures");

    /** Records the plain toy scenario into {@code dir} and returns the tape's text. */
    static String recordToy(Path dir) throws Exception {
        try (Recorder rec = Recorder.open(dir.toString(), Toy.plainBoundary())) {
            rec.call("greet", Recorder.kwargs("user", "alice"), () -> Toy.greet(Map.of("user", "alice")));
            try {
                rec.call("explode", Recorder.kwargs("user", "ghost"),
                        () -> Toy.explode(Map.of("user", "ghost")));
            } catch (Toy.ToyError expected) {
                // The point of this call: a raising effect produces both an fx.err and a non-null
                // call.error.
            }
            return Files.readString(Paths.get(rec.path()), StandardCharsets.UTF_8);
        }
    }

    /** Records the universal enrol scenario into {@code dir} and returns the tape's text. */
    static String recordSemToy(Path dir) throws Exception {
        try (Recorder rec = Recorder.open(dir.toString(), Toy.semBoundary())) {
            Map<String, Object> kw = Recorder.kwargs("user", "alice", "password", "hunter2");
            rec.call("enrol", kw, () -> Toy.enrol(kw));
            return Files.readString(Paths.get(rec.path()), StandardCharsets.UTF_8);
        }
    }

    /**
     * Always runs: both scenarios are recorded to a temp directory and must conform.
     *
     * <p>This is the ungated half. It never touches the committed fixtures — whose timestamps
     * differ every run — but it does prove the recorder still emits a conformant tape, which is the
     * property the committed bytes are only evidence of.
     */
    @Test
    void scenariosConform(@org.junit.jupiter.api.io.TempDir Path tmp) throws Exception {
        String toy = recordToy(tmp.resolve("toy"));
        assertEquals(List.of(), Validate.validateTape(toy), "the toy tape must conform");

        String sem = recordSemToy(tmp.resolve("sem"));
        assertEquals(List.of(), Validate.validateTape(sem), "the sem tape must conform");

        // The sem scenario exists to exercise the whole `sem` surface; if any of these stopped
        // appearing the fixture would still be conformant and would no longer be evidence.
        for (String needle : List.of("\"phase\":\"begin\"", "\"phase\":\"end\"", "\"phase\":\"point\"",
                "\"outcome\":\"ok\"", "\"outcome\":\"error\"", "__dt__")) {
            assertTrue(sem.contains(needle), "the sem tape must contain " + needle);
        }
        // Redaction reached the span data and the kwargs, not merely the top level.
        assertFalse(sem.contains("hunter2"), "the password must not survive anywhere in the tape");
        assertTrue(sem.contains("[REDACTED]"));
    }

    /** The plain scenario must carry all four random shapes — the tape's whole rand surface. */
    @Test
    void toyCarriesEveryRandomShape(@org.junit.jupiter.api.io.TempDir Path tmp) throws Exception {
        String toy = recordToy(tmp);
        for (String m : List.of("\"sample\"", "\"bytes\"", "\"float\"", "\"int\"")) {
            assertTrue(toy.contains("\"m\":" + m), "the toy tape must carry a " + m + " draw");
        }
    }

    /**
     * Regenerates the committed fixtures. Gated, because it overwrites bytes under version control.
     *
     * <p>{@code FR_REGEN_FIXTURES=1 mvn test -Dtest=FixturesTest}
     */
    @Test
    void regenerateFixtures(@org.junit.jupiter.api.io.TempDir Path tmp) throws Exception {
        String gate = System.getenv("FR_REGEN_FIXTURES");
        org.junit.jupiter.api.Assumptions.assumeTrue(gate != null && !gate.isEmpty(),
                "set FR_REGEN_FIXTURES to rewrite the committed fixtures");

        write(FIXTURES.resolve("java-toy.jsonl"), recordToy(tmp.resolve("toy")));
        write(FIXTURES.resolve("java-sem-toy.jsonl"), recordSemToy(tmp.resolve("sem")));
    }

    /** Refuses to write a non-conformant fixture — a bad fixture is worse than no fixture, because
     *  every other runtime's suite will trust it. */
    private static void write(Path target, String text) throws IOException {
        List<String> violations = Validate.validateTape(text);
        assertEquals(List.of(), violations, "refusing to write a non-conformant fixture to " + target);
        Files.createDirectories(target.getParent());
        Files.writeString(target, text, StandardCharsets.UTF_8);
    }
}
