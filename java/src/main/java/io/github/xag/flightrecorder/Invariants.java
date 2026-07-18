package io.github.xag.flightrecorder;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Claims that must hold on every execution.
 *
 * <p>Recordings answer <em>"same?"</em>. Invariants answer <em>"right?"</em> — and the difference
 * matters, because a bug records just as faithfully as a fix does. A tape pins behaviour to
 * whatever the code did on the day; an invariant pins it to what the code is <b>supposed</b> to do,
 * so it still bites when the recorded behaviour was itself wrong.
 *
 * <p>The trajectory an invariant judges is the <b>replayed</b> execution, never the recorded one.
 * The recorded result is the thing being questioned; asserting over it would only confirm the tape
 * agrees with itself.
 *
 * <p>Because a tape is data and this layer only consumes it, an invariant written here judges a
 * tape written by <em>any</em> runtime.
 */
public final class Invariants {

    private Invariants() {}

    /** What an invariant is handed: everything the replayed execution did. */
    public static final class Trajectory {
        /** The value the replayed code produced. */
        public Object result;
        /** The replayed error's rendering, or null. */
        public String error;
        /** Every write the replayed code performed ({@code op}/{@code sig}/{@code args}) — writes
         *  are compared, never executed, so this is the record of what it WOULD have written. */
        public List<Map<String, Object>> writes = new ArrayList<>();
        /** The replayed code's own semantic claims. */
        public List<Replay.SemPair> sems = new ArrayList<>();
        /** The call's kwargs, revived. */
        public Map<String, Object> kwargs = Map.of();
        /** The raw boundary events the recording holds for this call. */
        public List<Map<String, Object>> events = new ArrayList<>();
        /**
         * Every local, on every executed line — the claim surface that catches a bug whose output
         * is perfectly self-consistent and still wrong.
         *
         * <p>Empty, never null, when the process is not running instrumented. A claim about an
         * untraced variable therefore FAILS rather than passing vacuously, which is the whole
         * reason it is empty-not-null.
         */
        public Trace trace = Trace.empty();

        /** The result, cast. */
        @SuppressWarnings("unchecked")
        public <T> T resultAs(Class<T> type) {
            return type.isInstance(result) ? (T) result : null;
        }
    }

    /** A claim, and the check that decides it. The check fails by throwing. */
    public record Invariant(String name, Check check) {
        @FunctionalInterface
        public interface Check {
            void assertOn(Trajectory t) throws Exception;
        }
    }

    /** Declares an invariant. */
    public static Invariant of(String name, Invariant.Check check) {
        return new Invariant(name, check);
    }

    /** One claim's verdict. */
    public record Result(String name, boolean ok, String error) {}

    /** The replay's verdict plus every claim's. */
    public static final class Report {
        public Replay.Report replay;
        public List<Result> results = new ArrayList<>();

        /**
         * Whether this run is clean.
         *
         * <p>The bar differs by mode, and deliberately: a strict replay must also MATCH the
         * recording, whereas a probe replay is a deliberately mutated world where a different
         * result is the entire point — so there the claims are the whole verdict, and the replay
         * only has to have been answerable.
         */
        public boolean ok() {
            boolean claims = results.stream().allMatch(Result::ok);
            if (replay == null) return claims;
            if (replay.probe) return claims && replay.divergence == null && replay.unanswerable == null;
            return claims && replay.ok();
        }

        public List<Result> violations() {
            return results.stream().filter(r -> !r.ok()).toList();
        }

        @Override public String toString() { return format(this); }
    }

    /** Replays call {@code index} and judges the resulting trajectory against every claim. */
    public static Report check(Recording rec, int index, Replay.Resolver resolve,
                               List<Invariant> invariants, Boundary boundary, boolean probe) {
        Recording.CallView cv = rec.call(index);
        if (cv == null) {
            throw new IllegalArgumentException(
                    "call " + index + " out of range: " + rec.numCalls() + " call(s) in the session");
        }
        return checkCall(cv, resolve, invariants, boundary, probe);
    }

    /** @see #check(Recording, int, Replay.Resolver, List, Boundary, boolean) */
    public static Report checkCall(Recording.CallView cv, Replay.Resolver resolve,
                                   List<Invariant> invariants, Boundary boundary, boolean probe) {
        Report report = new Report();
        report.replay = Replay.replayCall(cv, resolve, boundary, probe);

        Trajectory t = new Trajectory();
        t.result = Serial.fromJsonable(report.replay.replayedResult);
        t.error = report.replay.replayedError;
        t.writes = report.replay.writes;
        t.sems = report.replay.semsReplayed;
        t.kwargs = report.replay.kwargs;
        t.events = cv.events();
        t.trace = report.replay.trace == null ? Trace.empty() : report.replay.trace;

        for (Invariant inv : invariants) {
            report.results.add(safely(inv, t));
        }
        return report;
    }

    /**
     * Runs one claim, turning a throw into a verdict.
     *
     * <p>A broken invariant is a finding about that invariant, not a reason the whole run dies —
     * one claim written badly must not take down the twenty written well, or nobody learns what the
     * other twenty said.
     */
    private static Result safely(Invariant inv, Trajectory t) {
        try {
            inv.check().assertOn(t);
            return new Result(inv.name(), true, null);
        } catch (Throwable e) {
            String msg = e.getMessage();
            return new Result(inv.name(), false,
                    msg == null || msg.isEmpty() ? e.getClass().getSimpleName() : msg);
        }
    }

    /** A human-readable rendering. */
    public static String format(Report r) {
        StringBuilder b = new StringBuilder();
        if (r.replay != null) b.append(Replay.format(0, r.replay)).append('\n');
        for (Result res : r.results) {
            b.append(res.ok() ? "  ok   " : "  FAIL ").append(res.name());
            if (!res.ok()) b.append(" — ").append(res.error());
            b.append('\n');
        }
        return b.toString().stripTrailing();
    }
}
