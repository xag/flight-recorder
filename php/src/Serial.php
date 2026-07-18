<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Boundary value (de)serialization — the PHP half of spec/tape-v1.md's "Value encoding".
 *
 * Everything crossing the recorded boundary is encoded as JSON with revivable markers for
 * datetimes; anything exotic degrades to an opaque marker rather than breaking the recorded
 * call. The failure direction is always "the recording is a bit poorer", never "the app broke
 * because it was being recorded".
 *
 * Traced *internal* values are a different problem, handled by traceJsonable(): they are not
 * inputs to be revived faithfully but claims to be asserted against, they are captured on
 * every executed line, and they include whatever objects the code happens to hold. So they
 * are recorded as data (not reprs — you cannot do arithmetic on `'2'`), document snapshots
 * are unwrapped, and anything long is cut to a prefix that still knows its true length.
 *
 * ## The one PHP-shaped decision: `[]` is a list
 *
 * PHP has a single `array` type that is both sequence and map, so an EMPTY array is genuinely
 * ambiguous where every other runtime is not. This encoder follows PHP's own convention —
 * `array_is_list([])` is true, `json_encode([])` is `[]` — and encodes an empty array as a
 * JSON array. Where the tape REQUIRES an object (`kwargs`, `constants`, `sem.data`), the
 * recorder passes `(object) []` and this encoder honours it, so those positions are `{}` on
 * the wire exactly as the spec demands. A caller who needs an empty map inside a value passes
 * `(object) []` too. Guessing "map" for `[]` would have been the other choice and it is worse:
 * it would silently turn every empty list an app returns into `{}`.
 */
final class Serial
{
    public const MAX_DEPTH = 16;

    /** What a redacted field's value becomes under a bare (null) rule. */
    public const REDACTED = '[REDACTED]';

    /** Caps for traced values. A local can be a 10k-row list, snapshotted on every line. */
    public const TRACE_MAX_ITEMS = 100;
    public const TRACE_MAX_CHARS = 512;

    /** Markers a v1 recorder may emit. */
    public const MARKERS = ['__dt__', '__date__', '__undef__', '__opaque__'];

    /** Reserved by the trace encoding — legal in a tape, never emitted by a recorder. */
    public const TRACE_MARKERS = ['__snap__', '__seq__', '__str__', '__esc__'];

    /**
     * A stable, address-free rendering of a value the tape cannot represent.
     *
     * Never the object's identity. PHP's `spl_object_id` and var_dump's `#3` are per-process
     * counters: recording one is recording a POINTER, which differs on every run, so the
     * effect it belongs to could never match on replay — a divergence with nothing to do with
     * the code under test.
     */
    public static function safeRepr(mixed $v, int $limit = 200): string
    {
        try {
            $s = match (true) {
                $v === null => 'null',
                is_bool($v) => $v ? 'true' : 'false',
                is_int($v), is_float($v) => (string) $v,
                is_string($v) => $v,
                $v instanceof \Closure => '<Closure>',
                $v instanceof \Throwable => $v::class . ': ' . $v->getMessage(),
                is_object($v) => '<' . $v::class . '>',
                is_resource($v) => '<resource(' . get_resource_type($v) . ')>',
                is_array($v) => '<array(' . count($v) . ')>',
                default => '<' . get_debug_type($v) . '>',
            };
        } catch (\Throwable) {
            return '<unreprable>';
        }
        return self::cut($s, $limit);
    }

    /** Cut to `$limit` characters, marking the cut — UTF-8 aware, never mid-codepoint. */
    private static function cut(string $s, int $limit): string
    {
        if (self::len($s) <= $limit) {
            return $s;
        }
        return self::sub($s, 0, $limit - 1) . "\u{2026}";
    }

    /**
     * Character length. Codepoints when ext-mbstring is present, bytes otherwise.
     *
     * The cap is a safety valve on tape size, not a wire-visible quantity, so degrading to
     * bytes when mbstring is absent changes where a long value gets cut and nothing else.
     * Requiring mbstring — which is bundled but not always enabled — to record a tape would
     * be a steeper price than a slightly earlier cut on non-ASCII text.
     */
    private static function len(string $s): int
    {
        return function_exists('mb_strlen') ? mb_strlen($s, 'UTF-8') : strlen($s);
    }

    private static function sub(string $s, int $start, int $length): string
    {
        return function_exists('mb_substr')
            ? mb_substr($s, $start, $length, 'UTF-8')
            : substr($s, $start, $length);
    }

    /** @return array{__opaque__: string} */
    private static function opaque(mixed $v): array
    {
        return ['__opaque__' => self::safeRepr($v)];
    }

    /**
     * Encode one boundary value.
     *
     * PHP has one nothing, so `__undef__` is never emitted here — it revives as null. The
     * marker exists for JavaScript, which has two and where a replay can depend on the
     * difference; it costs this runtime nothing and buys that one exactness.
     *
     * PHP likewise has no date-only type: `__date__` is revived (a tape from a Python
     * recorder carries it) but never emitted.
     */
    public static function toJsonable(mixed $v, int $depth = 0): mixed
    {
        if ($depth > self::MAX_DEPTH) {
            return self::opaque($v);
        }
        if ($v === null || is_bool($v) || is_int($v) || is_string($v)) {
            return $v;
        }
        if (is_float($v)) {
            // NAN and ±INF are not JSON. Encoding them would produce a tape no reader can load.
            return is_finite($v) ? $v : self::opaque($v);
        }
        if ($v instanceof \DateTimeInterface) {
            return ['__dt__' => self::isoOf($v)];
        }
        if (is_array($v)) {
            if (array_is_list($v)) {
                return array_map(static fn ($x) => self::toJsonable($x, $depth + 1), $v);
            }
            $out = [];
            foreach ($v as $k => $x) {
                $out[(string) $k] = self::toJsonable($x, $depth + 1);
            }
            return $out;
        }
        if ($v instanceof \stdClass) {
            $out = [];
            foreach (get_object_vars($v) as $k => $x) {
                $out[(string) $k] = self::toJsonable($x, $depth + 1);
            }
            // An empty stdClass is the caller saying "map", and it must survive as `{}`.
            return $out === [] ? new \stdClass() : $out;
        }
        if ($v instanceof \BackedEnum) {
            return $v->value;
        }
        if ($v instanceof \UnitEnum) {
            return $v->name;
        }
        if ($v instanceof Snapshot) {
            return self::snapshotJsonable($v);
        }
        if (is_object($v) && !($v instanceof \Closure) && !($v instanceof \Throwable)) {
            // An object's PUBLIC surface is data, and recording it is what lets replay hand the
            // declared type back to code that asked for one. `get_object_vars` called from here
            // sees only public properties — which is exactly the surface a consumer reads, and
            // the same choice Java makes with record components and public getters.
            //
            // Nothing private is recorded: an object's internals are its own business, and a
            // tape holding them would be recording the client library rather than the answer.
            if ($v instanceof \JsonSerializable) {
                try {
                    return self::toJsonable($v->jsonSerialize(), $depth);
                } catch (\Throwable) {
                    return self::opaque($v);
                }
            }
            $props = get_object_vars($v);
            if ($props !== []) {
                $out = [];
                foreach ($props as $k => $x) {
                    $out[(string) $k] = self::toJsonable($x, $depth + 1);
                }
                return $out;
            }
        }
        // Nothing readable: a closure, a resource, an exception, an object with no public data.
        // Its shape is not the surface a consumer reads, and reviving it faithfully is
        // impossible anyway.
        return self::opaque($v);
    }

    /**
     * ISO-8601 for a datetime, preserving the offset the app actually held.
     *
     * PHP has no naive datetime — every DateTimeInterface carries a timezone — so unlike
     * Python this always renders an offset. That is the value the app saw, and replay hands
     * back something indistinguishable from it, which is all `now.v` promises.
     */
    public static function isoOf(\DateTimeInterface $d): string
    {
        // Microseconds only when the value has them: a whole-second time renders `…:05+02:00`,
        // matching what every other runtime writes for the same instant.
        $micro = (int) $d->format('u');
        return $d->format($micro === 0 ? 'Y-m-d\TH:i:sP' : 'Y-m-d\TH:i:s.uP');
    }

    /** Revive a boundary value. `__opaque__` is a one-way door by design — it revives as text. */
    public static function fromJsonable(mixed $v): mixed
    {
        if ($v instanceof \stdClass) {
            $v = get_object_vars($v);
        }
        if (is_array($v)) {
            if (!array_is_list($v) && count($v) === 1) {
                $k = array_key_first($v);
                if ($k === '__dt__') {
                    return self::parseDate((string) $v[$k]);
                }
                if ($k === '__date__') {
                    // PHP has no date-only type; a date revives as midnight in the default zone.
                    return self::parseDate((string) $v[$k]);
                }
                if ($k === '__undef__') {
                    return null; // PHP has one nothing
                }
                if ($k === '__opaque__') {
                    return $v[$k];
                }
            }
            $out = [];
            foreach ($v as $k => $x) {
                $out[$k] = self::fromJsonable($x);
            }
            return $out;
        }
        return $v;
    }

    private static function parseDate(string $s): \DateTimeImmutable|string
    {
        try {
            return new \DateTimeImmutable($s);
        } catch (\Throwable) {
            return $s; // an unparseable marker payload is worth more as text than as a crash
        }
    }

    /**
     * Mask a jsonable tree two ways: `$rules` by FIELD NAME, `$scrub` by VALUE.
     *
     * Field rules assume a secret lives in a named field. Often it does not. A value passed
     * POSITIONALLY has no field name to match; nor does one interpolated into a key
     * (`session:{token}`) or sitting mid-sentence in a body of prose. Any of those walks onto
     * the tape untouched while a tidily-masked copy of itself sits in the next field along.
     * Sweeping every string is the only thing that catches them.
     *
     * The sweep also reaches something field rules structurally cannot: **masking an INPUT
     * poisons everything derived from it.** Mask an identifier by name and the recording holds
     * a key built from the RAW value while replay, handed the mask, builds one from the MASK —
     * a different question, and a divergence that says nothing about the code. A substring
     * sweep is consistent under derivation: `session:{token}` scrubs to exactly what the
     * replayed code builds out of the scrubbed `token`.
     *
     * It is NOT consistent under decryption. If the code recovers a value by decrypting stored
     * ciphertext, no sweep can reach it, and masking either side sends the replayed code down a
     * branch it never took — the recording then reproduces an execution that never happened.
     * Some values have to stay on the tape, and the tape treated accordingly.
     *
     * Both MUST be idempotent. Replay re-derives the question it is about to ask, scrubs it the
     * same way, and compares against the tape — so a value that is ALREADY a mask (it came off
     * the tape) has to scrub to itself, or a redacted recording could never be replayed at all.
     *
     * A rule or a scrub that throws degrades to REDACTED: the failure direction is "masked",
     * never "leaked" and never "broke the recorded call".
     *
     * @param array<string, (callable(mixed): mixed)|null> $rules field name → transform, or null to mask
     * @param (callable(string): string)|null              $scrub swept over every leaf string
     */
    public static function redactJsonable(mixed $v, array $rules, ?callable $scrub = null): mixed
    {
        if ($rules === [] && $scrub === null) {
            return $v;
        }

        $leaf = static function (mixed $x) use ($scrub): mixed {
            if ($scrub === null || !is_string($x)) {
                return $x;
            }
            try {
                return $scrub($x);
            } catch (\Throwable) {
                return self::REDACTED;
            }
        };

        if ($v instanceof \stdClass) {
            $inner = self::redactJsonable(get_object_vars($v), $rules, $scrub);
            return $inner === [] ? new \stdClass() : $inner;
        }

        if (is_array($v)) {
            if (array_is_list($v)) {
                return array_map(static fn ($x) => self::redactJsonable($x, $rules, $scrub), $v);
            }
            $out = [];
            foreach ($v as $k => $x) {
                if (array_key_exists((string) $k, $rules)) {
                    $rule = $rules[(string) $k];
                    if ($rule === null) {
                        $out[$k] = self::REDACTED;
                    } else {
                        try {
                            // The rule's OUTPUT still meets the sweep: a transform that
                            // tokenizes a field is not a licence for the sweep to look away.
                            $out[$k] = $leaf($rule($x));
                        } catch (\Throwable) {
                            $out[$k] = self::REDACTED;
                        }
                    }
                } else {
                    $out[$k] = self::redactJsonable($x, $rules, $scrub);
                }
            }
            return $out;
        }

        return $leaf($v);
    }

    /**
     * The first forbid pattern matching `$text`, or null if it is clean.
     *
     * Scans the SERIALIZED record, not the value tree, and that is the whole point. Redaction
     * is field-name driven, so it protects exactly the fields you named; a secret reaches the
     * tape through every path a field name cannot see — a positional argument, a chain
     * signature, an opaque repr, a key, a string some effect built by concatenation. The one
     * thing all of those have in common is that they end up in the line about to be written.
     * So the tripwire reads that line.
     *
     * Returns the PATTERN, never the match. The caller puts this in an exception message, and
     * a tripwire that quotes the credential it caught — into a log, a stack trace, an issue —
     * is the leak it exists to prevent.
     *
     * @param list<string> $patterns PCRE patterns, delimiters included
     */
    public static function forbiddenHit(string $text, array $patterns): ?string
    {
        foreach ($patterns as $p) {
            // A malformed pattern must not take the recording down; but it must not silently
            // wave the line through either, so it counts as a hit.
            $hit = @preg_match($p, $text);
            if ($hit === false || $hit === 1) {
                return $p;
            }
        }
        return null;
    }

    /**
     * Serialize a document snapshot — identity, existence, data; the only surface a
     * well-behaved consumer reads.
     *
     * @return array{id: string|null, exists: bool, data: mixed}
     */
    public static function snapshotJsonable(mixed $snap): array
    {
        if ($snap instanceof Snapshot) {
            return [
                'id' => $snap->id,
                'exists' => $snap->exists,
                'data' => self::toJsonable($snap->exists ? $snap->data : null),
            ];
        }
        $exists = true;
        if (is_object($snap) && (property_exists($snap, 'exists') || method_exists($snap, 'exists'))) {
            $exists = (bool) (method_exists($snap, 'exists') ? $snap->exists() : $snap->exists);
        }
        $data = null;
        if ($exists && is_object($snap) && method_exists($snap, 'toArray')) {
            $data = $snap->toArray();
        }
        $id = is_object($snap) && property_exists($snap, 'id') ? $snap->id : null;
        return [
            'id' => $id === null ? null : (string) $id,
            'exists' => $exists,
            'data' => self::toJsonable($data),
        ];
    }

    /** Compact stable rendering of a chained-call argument, for `db` signatures. */
    public static function short(mixed $v, int $limit = 60): string
    {
        try {
            $s = Json::encode(self::toJsonable($v));
        } catch (\Throwable) {
            $s = self::safeRepr($v);
        }
        return self::cut($s, $limit);
    }

    // --- traced internal values ---------------------------------------------------------

    /** Every single-key marker the trace encoding uses, for escape detection. */
    private const ALL_MARKERS = [
        '__dt__', '__date__', '__undef__', '__opaque__',
        '__snap__', '__seq__', '__str__', '__esc__',
    ];

    /**
     * Encode one traced internal value. Unlike toJsonable() this unwraps document snapshots
     * and caps long values, because it runs on every local of every executed line.
     *
     * It must NEVER throw: it is called from instrumented application code, and an exception
     * there is raised inside the frame being traced — corrupting the very replay the trace is
     * meant to observe. Anything hostile degrades to an opaque marker instead.
     */
    public static function traceJsonable(mixed $v, int $depth = 0): mixed
    {
        try {
            return self::traceEncode($v, $depth);
        } catch (\Throwable) {
            return self::opaque($v);
        }
    }

    private static function traceEncode(mixed $v, int $depth): mixed
    {
        if ($depth > self::MAX_DEPTH) {
            return self::opaque($v);
        }
        if ($v === null || is_bool($v) || is_int($v)) {
            return $v;
        }
        if (is_float($v)) {
            return is_finite($v) ? $v : self::opaque($v);
        }
        if (is_string($v)) {
            $n = self::len($v);
            if ($n <= self::TRACE_MAX_CHARS) {
                return $v;
            }
            return ['__str__' => ['len' => $n, 'head' => self::sub($v, 0, self::TRACE_MAX_CHARS)]];
        }
        if ($v instanceof \DateTimeInterface) {
            return ['__dt__' => self::isoOf($v)];
        }
        if (self::snapshottable($v)) {
            try {
                return ['__snap__' => self::snapshotJsonable($v)];
            } catch (\Throwable) {
                return self::opaque($v);
            }
        }
        if ($v instanceof \stdClass) {
            return self::traceEncode(get_object_vars($v), $depth);
        }
        if (is_array($v)) {
            if (array_is_list($v)) {
                $n = count($v);
                if ($n <= self::TRACE_MAX_ITEMS) {
                    return array_map(static fn ($x) => self::traceJsonable($x, $depth + 1), $v);
                }
                $head = array_map(
                    static fn ($x) => self::traceJsonable($x, $depth + 1),
                    array_slice($v, 0, self::TRACE_MAX_ITEMS)
                );
                return ['__seq__' => ['len' => $n, 'head' => $head]];
            }
            if (count($v) === 1 && in_array((string) array_key_first($v), self::ALL_MARKERS, true)) {
                // a user array shaped exactly like a marker: escape it so it revives as itself
                $k = (string) array_key_first($v);
                return ['__esc__' => [$k => self::traceJsonable($v[array_key_first($v)], $depth + 1)]];
            }
            $out = [];
            foreach ($v as $k => $x) {
                $out[(string) $k] = self::traceJsonable($x, $depth + 1);
            }
            return $out;
        }
        return self::opaque($v);
    }

    private static function snapshottable(mixed $v): bool
    {
        try {
            return $v instanceof Snapshot
                || (is_object($v)
                    && method_exists($v, 'toArray')
                    && (property_exists($v, 'exists') || method_exists($v, 'exists')));
        } catch (\Throwable) {
            return false;
        }
    }

    /** Revive a traced value into something an invariant can assert on. */
    public static function fromTraceJsonable(mixed $v): mixed
    {
        if ($v instanceof \stdClass) {
            $v = get_object_vars($v);
        }
        if (is_array($v)) {
            if (!array_is_list($v) && count($v) === 1) {
                $k = (string) array_key_first($v);
                $payload = $v[array_key_first($v)];
                if ($payload instanceof \stdClass) {
                    $payload = get_object_vars($payload);
                }
                switch ($k) {
                    case '__dt__':
                    case '__date__':
                        return self::parseDate((string) $payload);
                    case '__undef__':
                        return null;
                    case '__opaque__':
                        return $payload;
                    case '__snap__':
                        return self::fromTraceJsonable($payload);
                    case '__seq__':
                        return new Truncated(
                            array_map([self::class, 'fromTraceJsonable'], (array) ($payload['head'] ?? [])),
                            (int) ($payload['len'] ?? 0)
                        );
                    case '__str__':
                        return new TruncatedText(
                            (string) ($payload['head'] ?? ''),
                            (int) ($payload['len'] ?? 0)
                        );
                    case '__esc__':
                        $out = [];
                        foreach ((array) $payload as $ek => $ex) {
                            $out[$ek] = self::fromTraceJsonable($ex);
                        }
                        return $out;
                }
            }
            $out = [];
            foreach ($v as $k => $x) {
                $out[$k] = self::fromTraceJsonable($x);
            }
            return $out;
        }
        return $v;
    }

    /**
     * The length a traced value reports, or -1 when length is not a thing it has.
     *
     * Reads the `len` of a truncation marker rather than the length of the head, so `count()`
     * stays assertable after truncation — which is the whole reason the markers carry it.
     */
    public static function lengthOf(mixed $encoded): int
    {
        if (is_array($encoded) && !array_is_list($encoded) && count($encoded) === 1) {
            $payload = $encoded[array_key_first($encoded)];
            if (is_array($payload) && is_int($payload['len'] ?? null)) {
                return $payload['len'];
            }
        }
        if (is_string($encoded)) {
            return self::len($encoded);
        }
        if (is_array($encoded)) {
            return count($encoded);
        }
        return -1;
    }

    /** One-line display of a traced value, for --watch. */
    public static function render(mixed $v, int $limit = 90): string
    {
        try {
            $s = Json::encode($v);
        } catch (\Throwable) {
            $s = self::safeRepr($v);
        }
        return self::cut($s, $limit);
    }
}
