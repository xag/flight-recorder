package io.github.xag.flightrecorder;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * A document's recordable surface: identity, existence, data.
 *
 * <p>Deliberately only these three. They are the only surface a well-behaved consumer reads, and
 * recording more would tie the tape to one client library's snapshot object — which is exactly the
 * coupling a frozen wire format exists to avoid.
 */
public final class Snapshot {

    public final String id;
    public final boolean exists;
    public final Object data;

    public Snapshot(String id, boolean exists, Object data) {
        this.id = id;
        this.exists = exists;
        this.data = data;
    }

    /** A snapshot that was found. */
    public static Snapshot of(String id, Object data) { return new Snapshot(id, true, data); }

    /** A snapshot that was looked for and was not there — an answer, not an absence of one. */
    public static Snapshot missing(String id) { return new Snapshot(id, false, null); }

    Map<String, Object> jsonable() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("exists", exists);
        // A snapshot that does not exist has no data, and writing whatever the client happened to
        // leave in the field would record a value the app could not legitimately have read.
        m.put("data", exists ? Serial.toJsonable(data) : null);
        return m;
    }

    static Snapshot fromJsonable(Object v) {
        Map<String, Object> m = Json.asMap(v);
        if (m == null) return new Snapshot(null, false, null);
        Object id = m.get("id");
        Object exists = m.get("exists");
        return new Snapshot(
                id == null ? null : String.valueOf(id),
                Boolean.TRUE.equals(exists),
                Serial.fromJsonable(m.get("data")));
    }

    @Override
    public String toString() {
        return "Snapshot(id=" + id + ", exists=" + exists + ", data=" + data + ")";
    }
}
