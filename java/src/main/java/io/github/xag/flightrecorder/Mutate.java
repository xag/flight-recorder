package io.github.xag.flightrecorder;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Editing a recording: visit the world that never happened.
 *
 * <p>A tape is data, so a hostile world is one mutation away. Empty a result, make an effect throw,
 * run the clock backwards, shrink a population — then replay the <b>real code</b> against it and
 * judge what comes out with {@link Invariants}. This is how you test the failure path you could not
 * provoke in production, using the request that actually happened as the starting point.
 *
 * <p>Every edit marks the call a <b>probe</b>, and the flag is written to the tape. That matters
 * twice over: replay stops comparing arguments (a mutated upstream answer legitimately changes
 * every downstream question), and a saved mutated tape can never later be mistaken for a strict
 * regression pin.
 */
public final class Mutate {

    private Mutate() {}

    /** Opens a call for editing. */
    public static Handle on(Recording.CallView cv) { return new Handle(cv); }

    /** Handle onto one call under mutation. */
    public static final class Handle {

        private final Recording.CallView cv;

        Handle(Recording.CallView cv) { this.cv = cv; }

        public Recording.CallView view() { return cv; }

        /** Marks the call a probe. Every mutator does this for you. */
        private void dirty() { cv.markProbe(); }

        // ------------------------------------------------------------ effects

        /** The nth recorded effect named {@code name}. */
        public EffectHandle effect(String name, int occurrence) {
            int seen = 0;
            for (Map<String, Object> e : cv.events()) {
                if ("fx".equals(e.get("k")) && name.equals(e.get("fn"))) {
                    if (seen == occurrence) return new EffectHandle(this, e);
                    seen++;
                }
            }
            throw new IllegalArgumentException(
                    "no effect \"" + name + "\" (occurrence " + occurrence + ") in this call");
        }

        public EffectHandle effect(String name) { return effect(name, 0); }

        /** The nth recorded read. */
        public ReadHandle read(String op, int occurrence) {
            int seen = 0;
            for (Map<String, Object> e : cv.events()) {
                if (!"db".equals(e.get("k")) || !e.containsKey("res")) continue;
                if (op != null && !op.equals(e.get("op"))) continue;
                if (seen == occurrence) return new ReadHandle(this, e);
                seen++;
            }
            throw new IllegalArgumentException("no read" + (op == null ? "" : " \"" + op + "\"")
                    + " (occurrence " + occurrence + ") in this call");
        }

        public ReadHandle read() { return read(null, 0); }

        public ReadHandle read(String op) { return read(op, 0); }

        /** The nth recorded random draw. */
        public RandHandle rand(int occurrence) {
            Map<String, Object> e = cv.event("rand", occurrence);
            if (e == null) throw new IllegalArgumentException("no rand draw " + occurrence + " in this call");
            return new RandHandle(this, e);
        }

        public RandHandle rand() { return rand(0); }

        /** The call's recorded clock reads, in order. */
        public ClockHandle clock() { return new ClockHandle(this); }

        /** Replaces one of the call's inputs. */
        public Handle setKwarg(String key, Object value) {
            Map<String, Object> kw = Json.asMap(cv.raw().get("kwargs"));
            if (kw == null) {
                kw = new LinkedHashMap<>();
                cv.raw().put("kwargs", kw);
            }
            kw.put(key, Serial.toJsonable(value));
            dirty();
            return this;
        }

        // ------------------------------------------------------------ verdict

        /**
         * Replays the mutated call and judges it against the claims.
         *
         * <p>The mutated tape is written to a temp file, replayed from there, and deleted — and the
         * temp file is <b>tripwire-guarded too</b>, because it is a real artifact on a real disk
         * however briefly it lives.
         */
        public Invariants.Report check(Replay.Resolver resolve, List<Invariants.Invariant> invariants,
                                       Boundary boundary) {
            dirty();
            Path tmp = null;
            try {
                tmp = Files.createTempFile("flight-probe-", ".jsonl");
                Recording edited = cv.rec;
                if (boundary != null && !boundary.forbid.isEmpty()) {
                    edited.forbidding(boundary.forbid.toArray(new String[0]));
                }
                edited.save(tmp.toString());
                Recording reloaded = Recording.load(tmp.toString());
                Recording.CallView probe = reloaded.call(cv.index());
                return Invariants.checkCall(probe, resolve, invariants, boundary, true);
            } catch (IOException e) {
                throw new RuntimeException("flight-recorder: could not stage the probe tape", e);
            } finally {
                if (tmp != null) try { Files.deleteIfExists(tmp); } catch (IOException ignored) { }
            }
        }

        /** Writes the mutated recording out. Arm {@link Recording#forbidding} first if the boundary
         *  declared patterns — an edit can put a credential back into a tape that passed the guard
         *  when it was recorded. */
        public String save(String path, Boundary boundary) throws IOException {
            dirty();
            if (boundary != null && !boundary.forbid.isEmpty()) {
                cv.rec.forbidding(boundary.forbid.toArray(new String[0]));
            }
            return cv.rec.save(path);
        }
    }

    // ------------------------------------------------------------- the handles

    /** One recorded effect, editable. */
    public static final class EffectHandle {
        private final Handle owner;
        private final Map<String, Object> ev;

        EffectHandle(Handle owner, Map<String, Object> ev) { this.owner = owner; this.ev = ev; }

        /** The recorded answer, revived. */
        public Object result() { return Serial.fromJsonable(ev.get("res")); }

        /** Replaces the answer the world gave. */
        public EffectHandle setResult(Object value) {
            ev.remove("err");
            ev.put("res", Serial.toJsonable(value));
            owner.dirty();
            return this;
        }

        /** Makes this effect fail, so the code takes its error path against a world where it did. */
        public EffectHandle setError(String type, Object... args) {
            ev.remove("res");
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("type", type);
            err.put("repr", type);
            err.put("args", Recorder.jsonableList(List.of(args)));
            ev.put("err", err);
            owner.dirty();
            return this;
        }

        /** @see #setError(String, Object...) */
        public EffectHandle setError(Throwable t) {
            return setError(t.getClass().getSimpleName(), Recorder.render(t));
        }
    }

    /** One recorded read, editable. */
    public static final class ReadHandle {
        private final Handle owner;
        private final Map<String, Object> ev;

        ReadHandle(Handle owner, Map<String, Object> ev) { this.owner = owner; this.ev = ev; }

        /** The recorded snapshots. */
        public Object result() { return Serial.fromJsonable(ev.get("res")); }

        /**
         * Replaces what the read returned.
         *
         * <p>A plain value is wrapped into snapshot shape ({@code {id, exists, data}}) for you; a
         * map already shaped like a snapshot is kept as it is. A list becomes a list of snapshots.
         * This is convenience, not magic: writing the wrapper by hand every time is how a probe
         * session becomes tedious enough that nobody runs one.
         */
        public ReadHandle setResult(Object value) {
            ev.put("res", wrap(value));
            owner.dirty();
            return this;
        }

        /** Empties the read — the corpus is gone, the row is missing, the list came back empty. */
        public ReadHandle setEmpty() {
            ev.put("res", new ArrayList<>());
            owner.dirty();
            return this;
        }

        private static Object wrap(Object value) {
            if (value instanceof List<?> l) {
                List<Object> out = new ArrayList<>();
                for (int i = 0; i < l.size(); i++) out.add(wrapOne(l.get(i), "row" + i));
                return out;
            }
            return wrapOne(value, "row0");
        }

        private static Object wrapOne(Object value, String id) {
            if (value instanceof Snapshot s) return s.jsonable();
            Map<String, Object> m = Json.asMap(value);
            if (m != null && m.containsKey("id") && m.containsKey("exists") && m.containsKey("data")) {
                return Serial.toJsonable(m);
            }
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("id", id);
            out.put("exists", true);
            out.put("data", Serial.toJsonable(value));
            return out;
        }
    }

    /** One recorded random draw, editable. */
    public static final class RandHandle {
        private final Handle owner;
        private final Map<String, Object> ev;

        RandHandle(Handle owner, Map<String, Object> ev) { this.owner = owner; this.ev = ev; }

        public List<Object> indices() {
            List<Object> idx = Json.asList(ev.get("idx"));
            return idx == null ? List.of() : idx;
        }

        /**
         * Replaces the positions drawn.
         *
         * <p>Positions, not members — which is exactly what makes this editable: the draw stays a
         * draw from whatever population the mutated tape now holds.
         */
        public RandHandle setIndices(int... idx) {
            List<Object> out = new ArrayList<>();
            for (int i : idx) {
                if (i < 0) throw new IllegalArgumentException("a drawn position cannot be negative: " + i);
                out.add((long) i);
            }
            ev.put("m", "sample");
            ev.put("idx", out);
            ev.put("kk", (long) out.size());
            owner.dirty();
            return this;
        }

        /** Replaces a uniform draw. */
        public RandHandle setValue(Object v) {
            ev.put("v", Serial.toJsonable(v));
            owner.dirty();
            return this;
        }
    }

    /** The call's recorded clock reads. */
    public static final class ClockHandle {
        private final Handle owner;

        ClockHandle(Handle owner) { this.owner = owner; }

        private List<Map<String, Object>> reads() {
            List<Map<String, Object>> out = new ArrayList<>();
            for (Map<String, Object> e : owner.cv.events()) {
                if ("now".equals(e.get("k"))) out.add(e);
            }
            return out;
        }

        /** Every recorded clock read, in order. */
        public List<String> times() {
            List<String> out = new ArrayList<>();
            for (Map<String, Object> e : reads()) out.add(Json.asString(e.get("v")));
            return out;
        }

        /** Replaces the clock reads, in order. */
        public ClockHandle setTimes(String... isoTimes) {
            List<Map<String, Object>> rs = reads();
            for (int i = 0; i < rs.size() && i < isoTimes.length; i++) {
                rs.get(i).put("v", isoTimes[i]);
            }
            owner.dirty();
            return this;
        }

        /** Runs the clock backwards — the classic "what does this do when time goes the wrong
         *  way?" probe, which no amount of waiting will provoke in production. */
        public ClockHandle reverse() {
            List<Map<String, Object>> rs = reads();
            List<Object> vals = new ArrayList<>();
            for (Map<String, Object> e : rs) vals.add(e.get("v"));
            for (int i = 0; i < rs.size(); i++) rs.get(i).put("v", vals.get(vals.size() - 1 - i));
            owner.dirty();
            return this;
        }
    }
}
