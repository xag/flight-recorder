package io.github.xag.flightrecorder;

/**
 * Where a session goes besides the local disk — so recordings are retrievable from a machine you
 * have no shell on.
 *
 * <p>{@link #publish} is handed the session file's name and its <b>full current text</b>, after
 * the header and again after every completed call. Being handed the whole session each time is
 * what makes an overwriting sink (S3 {@code PutObject}, a KV {@code set}) sufficient, and means a
 * published tape is never half a tape.
 *
 * <p>It is best-effort: a {@code publish} that throws is swallowed, because recording must never
 * be the reason a call fails. Hand the bytes off and return — a {@code publish} that blocks stalls
 * the call that triggered it.
 */
@FunctionalInterface
public interface Sink {
    void publish(String name, String text);
}
