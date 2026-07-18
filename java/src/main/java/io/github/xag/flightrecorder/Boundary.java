package io.github.xag.flightrecorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.BiPredicate;
import java.util.function.Function;
import java.util.function.UnaryOperator;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

/**
 * The app-specific declaration the recorder needs: the constants to pin in the header, the two
 * redaction layers, the forbid tripwire, the per-call gate, and where the session goes besides
 * disk.
 *
 * <p>This is the project's first artifact by design — you declare the nondeterminism boundary
 * before you record across it.
 *
 * <p>Mutators return {@code this} so a boundary reads as a declaration:
 * <pre>{@code
 * Boundary b = new Boundary()
 *     .constant("toy.LIMIT", 3)
 *     .maskFields("password", "token")
 *     .scrubbing("sk-[A-Za-z0-9]{16,}")
 *     .forbidden("BEGIN PRIVATE KEY");
 * }</pre>
 */
public final class Boundary {

    final Map<String, Object> constants = new LinkedHashMap<>();
    final Map<String, Function<Object, Object>> redact = new LinkedHashMap<>();
    final Map<String, Object> headerExtras = new LinkedHashMap<>();
    final List<String> forbid = new ArrayList<>();
    final Map<String, Function<List<Object>, RuntimeException>> revivers = new LinkedHashMap<>();

    UnaryOperator<String> scrub;
    BiPredicate<String, Map<String, Object>> enabled;
    Sink sink;

    /** A constant to pin in the session header, so a tape records the configuration it ran under
     *  and not merely the calls. */
    public Boundary constant(String name, Object value) {
        constants.put(name, value);
        return this;
    }

    /** An extra header key. Preserved verbatim by any reader that rewrites the tape. */
    public Boundary headerExtra(String name, Object value) {
        headerExtras.put(name, value);
        return this;
    }

    /**
     * Layer 1 — redaction by FIELD NAME. Every value under one of these keys becomes
     * {@link Serial#REDACTED}, wherever in the tree it sits.
     */
    public Boundary maskFields(String... names) {
        for (String n : names) redact.put(n, null);
        return this;
    }

    /**
     * Layer 1 with a transform: the field's value is replaced by the transform's output rather
     * than by a flat mask — a last-four, a hash, a length. The output meets the value sweep too,
     * so a transform that shortens rather than masks cannot smuggle the secret past.
     */
    public Boundary redacting(String name, Function<Object, Object> transform) {
        redact.put(name, transform);
        return this;
    }

    /** Layer 2 — redaction by VALUE, with the default mask. */
    public Boundary scrubbing(String pattern) {
        return scrubbing(pattern, Serial.REDACTED);
    }

    /**
     * Layer 2 — redaction by VALUE: every leaf string, wherever it sits, has {@code pattern}
     * replaced by {@code mask}. This catches what no field name can see — a positional argument, a
     * key built by interpolation, a secret quoted mid-sentence in a response body.
     *
     * <p>Calls STACK: call it once per secret shape rather than spelling them all in one regex.
     *
     * <p><b>The mask may not match the pattern, and that is enforced here.</b> Replay re-derives
     * the question, scrubs it the same way, and compares the result against the tape — so scrubbing
     * has to be idempotent, and a mask that matches its own pattern is not: the first pass masks
     * the secret, the second masks the mask, and replay reports a divergence on a value that never
     * changed. Refusing it at declaration time is much kinder than discovering it as a phantom
     * divergence six months later.
     *
     * @throws IllegalArgumentException if the pattern is not a valid regex, or the mask matches it
     */
    public Boundary scrubbing(String pattern, String mask) {
        Pattern p;
        try {
            p = Pattern.compile(pattern);
        } catch (PatternSyntaxException e) {
            throw new IllegalArgumentException("bad scrub pattern \"" + pattern + "\": " + e.getMessage(), e);
        }
        if (p.matcher(mask).find()) {
            throw new IllegalArgumentException(
                    "the mask \"" + mask + "\" itself matches the scrub pattern \"" + pattern + "\", so "
                    + "scrubbing would not be idempotent: a second pass would mask the mask, and replay "
                    + "would report a divergence on a value that never changed. Choose a mask the "
                    + "pattern does not match.");
        }
        UnaryOperator<String> prior = scrub;
        UnaryOperator<String> mine = s -> p.matcher(s).replaceAll(java.util.regex.Matcher.quoteReplacement(mask));
        scrub = prior == null ? mine : s -> mine.apply(prior.apply(s));
        return this;
    }

    /** Layer 2 with an arbitrary transform. It MUST be idempotent — see {@link #scrubbing(String,
     *  String)} for why the library cares. */
    public Boundary scrubbingWith(UnaryOperator<String> transform) {
        UnaryOperator<String> prior = scrub;
        scrub = prior == null ? transform : s -> transform.apply(prior.apply(s));
        return this;
    }

    /**
     * Layer 3 — the tripwire. If this pattern matches any artifact the recorder is about to write
     * (a tape line, a re-saved tape, a trace event), <b>nothing is written</b> and
     * {@link Errors.ForbiddenValue} is raised.
     *
     * <p>Patterns match SHAPES, not values: a credential you can enumerate you can already redact.
     * This is for the one you cannot — the shape of any private key, any bearer token — so that a
     * redaction rule that silently stopped matching fails a build instead of shipping a secret.
     *
     * @throws IllegalArgumentException if the pattern is not a valid regex (checked here, at
     *         declaration time, rather than at the moment it would have fired)
     */
    public Boundary forbidden(String pattern) {
        try {
            Pattern.compile(pattern);
        } catch (PatternSyntaxException e) {
            throw new IllegalArgumentException("bad forbid pattern \"" + pattern + "\": " + e.getMessage(), e);
        }
        forbid.add(pattern);
        return this;
    }

    /**
     * The per-call gate. Null records every call. Consulted with the tool name and its kwargs, so
     * one running server can record a single user's request and leave the rest untouched. A gate
     * that never admits a call leaves no session file at all.
     *
     * <p>A gate that throws is treated as a refusal — it can never break the call it was asked
     * about.
     */
    public Boundary enabledWhen(BiPredicate<String, Map<String, Object>> gate) {
        enabled = gate;
        return this;
    }

    /** Where the session goes besides the local disk. */
    public Boundary publishingTo(Sink s) {
        sink = s;
        return this;
    }

    /**
     * Declares how to rebuild a recorded error with its real type on replay.
     *
     * <p>Code branches on exception type — {@code catch (RateLimited e)} takes a different path
     * from {@code catch (NotFound e)} — so a replay that threw one generic stand-in for every
     * recorded error would send execution down a path the original never took, and then report the
     * resulting difference as a divergence in the code. The reviver is handed the recorded
     * {@code err.args} and returns the real exception.
     *
     * <p>An error type with no reviver becomes {@link Errors.ReplayedEffectError}.
     */
    public Boundary reviving(String errorType, Function<List<Object>, RuntimeException> build) {
        revivers.put(errorType, build);
        return this;
    }
}
