package io.github.xag.flightrecorder;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

/**
 * A traced value costs a bounded amount, whatever it holds: one node budget over each local, every
 * level included; maps cut like sequences, with their true size; a cycle named rather than
 * walked; and one depth limit that carries through an object's getters.
 */
class TraceBudgetTest {

    /** One level of a wide graph: a hundred references to the same next level. Shared, not a
     *  cycle: the graph fans out a hundredfold per level, which no cycle guard sees. */
    public static final class Level {
        private final List<Object> items;
        Level(List<Object> items) { this.items = items; }
        public List<Object> getItems() { return items; }
    }

    private static Object wide(int levels, int width) {
        Object leaf = "x";
        for (int l = 0; l < levels; l++) {
            List<Object> items = new ArrayList<>();
            for (int i = 0; i < width; i++) items.add(leaf);
            leaf = new Level(items);
        }
        return leaf;
    }

    @Test
    void aWideObjectGraphIsTracedWithinTheBudget() {
        Object graph = wide(3, 100); // 100 × 100 × 100 leaves through the objects' getters
        String out = assertTimeoutPreemptively(Duration.ofSeconds(5),
                () -> Json.write(TraceValue.toTraceJsonable(graph)));
        assertTrue(out.contains("beyond the trace budget"), "the budget must have cut the walk");
        assertTrue(out.length() < 200_000, "a bounded encoding, not a million leaves: " + out.length() + " chars");
    }

    @Test
    void theBudgetIsPerValue() {
        // Each local of a line gets its own budget: a huge first local does not starve the next.
        TraceHook.Sink sink = new TraceHook.Sink(null, null);
        TraceHook.Sink prior = TraceHook.sink();
        TraceHook.setSink(sink);
        try {
            long f = TraceHook.enter("f", "a.java:1", new String[]{"big", "small"},
                    new Object[]{wide(3, 100), List.of(1, 2, 3)});
            TraceHook.exit(f);
        } finally {
            TraceHook.setSink(prior);
        }
        Map<String, Object> args = Json.asMap(sink.snapshot().events.get(0).get("args"));
        assertEquals(List.of(1L, 2L, 3L), args.get("small"));
    }

    /** Two getters that both lead back to the object itself. */
    public static final class Loop {
        public Loop getLeft() { return this; }
        public Loop getRight() { return this; }
        public int getN() { return 7; }
    }

    @Test
    void aSelfReferencingObjectIsNamedNotWalked() {
        Object enc = assertTimeoutPreemptively(Duration.ofSeconds(5), () -> TraceValue.toTraceJsonable(new Loop()));
        Map<String, Object> m = Json.asMap(enc);
        assertNotNull(m, "an object is traced as its surface: " + enc);
        assertEquals(Map.of("__opaque__", "<Loop (cycle)>"), m.get("left"));
        assertEquals(Map.of("__opaque__", "<Loop (cycle)>"), m.get("right"));
        assertEquals(7L, m.get("n"));
    }

    @Test
    void aLongMapIsCutWithItsTrueLength() {
        Map<String, Object> big = new LinkedHashMap<>();
        for (int i = 0; i < 150; i++) big.put("k" + i, i);
        Object enc = TraceValue.toTraceJsonable(big);
        Map<String, Object> marker = Json.asMap(Json.asMap(enc).get("__map__"));
        assertNotNull(marker, "a long map is a __map__ marker: " + Json.write(enc).substring(0, 60));
        assertEquals(150L, marker.get("len"));
        assertEquals(TraceValue.MAX_ITEMS, Json.asMap(marker.get("head")).size());
        assertEquals(150, TraceValue.lengthOf(enc), "the TRUE size stays assertable");
        Object revived = TraceValue.fromTraceJsonable(Json.parse(Json.write(enc)));
        assertEquals(TraceValue.MAX_ITEMS, ((Map<?, ?>) revived).size());
        assertEquals(0L, ((Map<?, ?>) revived).get("k0"));
    }

    @Test
    void aUserMapShapedLikeTheMapMarkerIsEscaped() {
        Map<String, Object> user = Map.of("__map__", 1);
        Object enc = TraceValue.toTraceJsonable(user);
        assertEquals(Map.of("__esc__", Map.of("__map__", 1L)), enc);
        assertEquals(Map.of("__map__", 1L), TraceValue.fromTraceJsonable(enc));
    }

    public record Box(Object inner) {}

    private static int nesting(Object enc) {
        if (enc instanceof Map<?, ?> m) {
            int d = 0;
            for (Object x : m.values()) d = Math.max(d, nesting(x));
            return d + 1;
        }
        if (enc instanceof List<?> l) {
            int d = 0;
            for (Object x : l) d = Math.max(d, nesting(x));
            return d + 1;
        }
        return 0;
    }

    @Test
    void theDepthLimitCarriesThroughAnObjectsGetters() {
        // Boxes in maps in boxes: the trace codec's depth used to restart at zero each time a value
        // was handed to the object walk, so the real limit was the sum of both.
        Object v = "bottom";
        for (int i = 0; i < 60; i++) v = i % 2 == 0 ? new Box(v) : Map.of("m", v);
        int depth = nesting(TraceValue.toTraceJsonable(v));
        assertTrue(depth <= TraceValue.MAX_DEPTH + 2, "nesting " + depth + " exceeds the trace depth limit");
    }
}
