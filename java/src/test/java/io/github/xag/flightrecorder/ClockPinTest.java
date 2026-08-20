package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * {@link Recorder#clockAt} makes a recorded clock read answer the instant plus the time since —
 * in the system zone, like the unpinned reads — and the tape carries that answer as its
 * {@code now} event, exactly as if the machine's clock had said so. Running: two reads are two
 * instants, in order, so an app stamping its writes with the clock still stamps them apart. The
 * pin is lifted on close, nested or not.
 */
class ClockPinTest {

    private static boolean within(Instant got, Instant want) {
        Duration d = Duration.between(want, got);
        return !d.isNegative() && d.getSeconds() < 5;
    }

    @Test
    void aSetClockAnswersTheInstantRunningAndRecordsItAsNow(@TempDir Path tmp) throws Exception {
        Instant at = Instant.parse("2026-08-16T08:00:00Z");
        Instant next = at.plus(Duration.ofDays(1));
        Map<String, Object> none = Map.of();
        Recording tape;
        try (Recorder rec = Recorder.open(tmp.toString(), Toy.semBoundary())) {
            try (Recorder.Pin ignored = Recorder.clockAt(at)) {
                rec.call("tick", none, () -> Recorder.nowOffset());
                Instant first = Recorder.nowOffset().toInstant();
                assertTrue(within(first, at), "first read " + first);
                assertTrue(within(Recorder.now().atZone(ZoneId.systemDefault()).toInstant(), at));
                try (Recorder.Pin inner = Recorder.clockAt(next)) {
                    rec.call("tick", none, () -> Recorder.nowOffset());
                    assertTrue(within(Recorder.nowOffset().toInstant(), next));
                }
                Thread.sleep(5);
                rec.call("tick", none, () -> Recorder.nowOffset());
                Instant later = Recorder.nowOffset().toInstant();
                assertTrue(later.isAfter(first), "a set clock runs; it does not stop");
                assertTrue(within(later, at));
            }
            assertNull(Recorder.PINNED_NOW.get());
            assertTrue(Duration.between(Recorder.nowOffset(), OffsetDateTime.now()).abs().getSeconds() < 5);
            tape = Recording.load(rec.path());
        }

        List<Instant> want = List.of(at, next, at);
        for (int i = 0; i < want.size(); i++) {
            Instant got = OffsetDateTime.parse((String) tape.call(i).event("now").get("v")).toInstant();
            assertTrue(within(got, want.get(i)), "call " + i + " recorded " + got);
        }
    }
}
