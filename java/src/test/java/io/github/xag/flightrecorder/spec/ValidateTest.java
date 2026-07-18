package io.github.xag.flightrecorder.spec;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

import org.junit.jupiter.api.Test;

/**
 * The checker's acceptance test, and the reason the checker exists.
 *
 * <p>{@code spec/fixtures/} holds tapes produced by the other implementations. Every one of them
 * must validate here, unread and unadjusted — that is what makes "the tape is one format" a fact
 * rather than an intention. If a fixture fails, this checker is wrong: the fixtures are the
 * evidence and this file is the claim.
 */
class ValidateTest {

    private static final Path FIXTURES = Path.of("..", "spec", "fixtures");

    private static final String HEADER =
            "{\"ev\":\"session\",\"version\":1,\"started\":\"2026-07-18T10:00:00+02:00\","
            + "\"java\":\"21\",\"constants\":{}}";

    private static String call(String events) {
        return HEADER + "\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\",\"kwargs\":{},\"error\":null,"
                + "\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[" + events + "]}";
    }

    @Test
    void everyFixtureIsConformant() throws IOException {
        List<Path> tapes = new ArrayList<>();
        try (Stream<Path> s = Files.list(FIXTURES)) {
            s.filter(p -> p.toString().endsWith(".jsonl")).sorted().forEach(tapes::add);
        }
        assertFalse(tapes.isEmpty(), "no fixtures found at " + FIXTURES.toAbsolutePath());
        for (Path p : tapes) {
            assertEquals(List.of(), Validate.validateTape(Files.readString(p)), p.getFileName().toString());
        }
    }

    /** Where an int is required, {@code 1.0} is not one. The whole int/float discipline in the
     *  parser exists for this line. */
    @Test
    void floatWhereAnIntIsRequired() {
        assertTrue(Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":1.0,\"fn\":\"f\","
                + "\"kwargs\":{},\"error\":null,\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,"
                + "\"events\":[]}").stream().anyMatch(v -> v.contains("call.seq")));
        assertTrue(Validate.validateTape(call("{\"k\":\"sem\",\"name\":\"a\",\"phase\":\"point\","
                + "\"sid\":1.0}")).stream().anyMatch(v -> v.contains("int 'sid'")));
    }

    @Test
    void sessionNamesExactlyOneRuntime() {
        assertFalse(Validate.validateTape(
                "{\"ev\":\"session\",\"version\":1,\"started\":\"2026-07-18T10:00:00+02:00\","
                + "\"java\":\"21\",\"go\":\"1.22\",\"constants\":{}}").isEmpty());
        assertFalse(Validate.validateTape(
                "{\"ev\":\"session\",\"version\":1,\"started\":\"2026-07-18T10:00:00+02:00\","
                + "\"constants\":{}}").isEmpty());
        assertEquals(List.of(), Validate.validateTape(HEADER));
    }

    /** call.ts is recorder metadata and must be aware; now.v is a value the app was handed and
     *  must not be normalised. The asymmetry is deliberate — see spec/tape-v1.md. */
    @Test
    void awarenessIsRequiredOfMetadataOnly() {
        assertFalse(Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\","
                + "\"kwargs\":{},\"error\":null,\"ts\":\"2026-07-18T10:00:00\",\"ms\":1,"
                + "\"events\":[]}").isEmpty());
        assertEquals(List.of(), Validate.validateTape(call("{\"k\":\"now\",\"v\":\"2026-07-18T10:00:00\"}")));
    }

    @Test
    void callMustCarryError() {
        assertFalse(Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\","
                + "\"kwargs\":{},\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[]}").isEmpty());
    }

    @Test
    void spansNestAndClose() {
        List<String> crossed = Validate.validateTape(call(
                "{\"k\":\"sem\",\"name\":\"a\",\"phase\":\"begin\",\"sid\":1},"
                + "{\"k\":\"sem\",\"name\":\"b\",\"phase\":\"begin\",\"sid\":2},"
                + "{\"k\":\"sem\",\"name\":\"a\",\"phase\":\"end\",\"sid\":1},"
                + "{\"k\":\"sem\",\"name\":\"b\",\"phase\":\"end\",\"sid\":2}"));
        assertTrue(crossed.stream().anyMatch(v -> v.contains("not well-nested")));

        assertTrue(Validate.validateTape(call("{\"k\":\"sem\",\"name\":\"a\",\"phase\":\"begin\",\"sid\":1}"))
                .stream().anyMatch(v -> v.contains("never closed")));

        assertTrue(Validate.validateTape(call(
                "{\"k\":\"sem\",\"name\":\"a\",\"phase\":\"point\",\"sid\":1},"
                + "{\"k\":\"sem\",\"name\":\"b\",\"phase\":\"point\",\"sid\":1}"))
                .stream().anyMatch(v -> v.contains("is reused")));
    }

    @Test
    void randShapes() {
        assertFalse(Validate.validateTape(call("{\"k\":\"rand\",\"m\":\"bytes\",\"n\":2,\"hex\":\"AB12\"}")).isEmpty());
        assertFalse(Validate.validateTape(call("{\"k\":\"rand\",\"m\":\"bytes\",\"n\":3,\"hex\":\"ab12\"}")).isEmpty());
        assertFalse(Validate.validateTape(call("{\"k\":\"rand\",\"m\":\"float\",\"v\":1.0}")).isEmpty());
        assertFalse(Validate.validateTape(call("{\"k\":\"rand\",\"m\":\"sample\",\"n\":3,\"kk\":2,\"idx\":[0,3]}")).isEmpty());
        assertFalse(Validate.validateTape(call("{\"k\":\"rand\",\"m\":\"spin\"}")).isEmpty());
        assertEquals(List.of(), Validate.validateTape(call("{\"k\":\"rand\",\"m\":\"bytes\",\"n\":2,\"hex\":\"ab12\"}")));
    }

    /** Forward compatibility is the whole reason the format can grow without a version bump. */
    @Test
    void unknownEvAndUnknownKindAreIgnored() {
        assertEquals(List.of(), Validate.validateTape(
                HEADER + "\n{\"ev\":\"inflight\",\"whatever\":1}\n"
                + "{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\",\"kwargs\":{},\"error\":null,"
                + "\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[{\"k\":\"future\",\"x\":1}]}"));
    }

    /** Only the final line may be torn — a process that died mid-write. Anywhere else it is
     *  corruption, and silence about it would be the worst possible answer. */
    @Test
    void onlyTheFinalLineMayBeTorn() {
        assertEquals(List.of(), Validate.validateTape(
                HEADER + "\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\",\"kwargs\":{},\"error\":null,"
                + "\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[]}\n{\"ev\":\"call\",\"seq"));
        assertFalse(Validate.validateTape(
                HEADER + "\n{\"ev\":\"call\",\"seq\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\","
                + "\"kwargs\":{},\"error\":null,\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,"
                + "\"events\":[]}").isEmpty());
    }

    @Test
    void seqIsOneBasedAndContiguous() {
        assertFalse(Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":2,\"fn\":\"f\","
                + "\"kwargs\":{},\"error\":null,\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,"
                + "\"events\":[]}").isEmpty());
    }

    @Test
    void valueModel() {
        assertFalse(Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\","
                + "\"kwargs\":{\"x\":{\"__opaque__\":\"" + "z".repeat(201) + "\"}},\"error\":null,"
                + "\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[]}").isEmpty());
        assertFalse(Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":1,\"fn\":\"f\","
                + "\"kwargs\":{\"x\":{\"__undef__\":1}},\"error\":null,"
                + "\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[]}").isEmpty());
        // Reserved markers are legal and not interpreted.
        assertEquals(List.of(), Validate.validateTape(HEADER + "\n{\"ev\":\"call\",\"seq\":1,"
                + "\"fn\":\"f\",\"kwargs\":{\"x\":{\"__snap__\":\"anything at all\"}},\"error\":null,"
                + "\"ts\":\"2026-07-18T10:00:00+02:00\",\"ms\":1,\"events\":[]}"));
    }

    @Test
    void emptyTape() {
        assertEquals(List.of("empty tape: the session header is mandatory"), Validate.validateTape(""));
    }
}
