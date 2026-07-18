<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The tape's JSON codec.
 *
 * PHP ships JSON in core, so unlike Java and .NET there is no codec to hand-roll here — but
 * the two disciplines those ports had to implement by hand still have to be *chosen* here,
 * because PHP's defaults get both wrong for this purpose.
 *
 * **Integer and float stay distinct.** `json_encode(1.0)` is `1` by default, which would put
 * `"ms": 1` on the tape where the spec says a number and, worse, would let a `seq` that had
 * become a float sail past a checker that is supposed to reject it. JSON_PRESERVE_ZERO_FRACTION
 * keeps `1.0` rendering as `1.0`. On the way back, `json_decode` already returns int for `1`
 * and float for `1.0`, which is what the conformance checker's int tests depend on.
 *
 * **Floats round-trip exactly.** PHP's `serialize_precision = -1` (the default since 7.1) emits
 * the shortest string that reads back as the same double — the same guarantee Go, Java and JS
 * give. This class asserts it rather than assuming it, because a php.ini that sets
 * `serialize_precision = 17` would silently start writing `0.10000000000000001` and a tape that
 * no longer compares equal to the one another runtime wrote for the same value.
 *
 * Objects decode to associative arrays, not stdClass: everything downstream — the checker, the
 * reader, mutation — walks the tape as arrays, and a tape is data, not a graph of objects.
 */
final class Json
{
    private const ENCODE_FLAGS = JSON_UNESCAPED_SLASHES
        | JSON_UNESCAPED_UNICODE
        | JSON_PRESERVE_ZERO_FRACTION
        | JSON_THROW_ON_ERROR;

    /** Encode a jsonable value to one line of tape. */
    public static function encode(mixed $v): string
    {
        return json_encode($v, self::ENCODE_FLAGS);
    }

    /** Encode for human reading (the reader's --watch, error messages). Not tape. */
    public static function pretty(mixed $v): string
    {
        return json_encode($v, self::ENCODE_FLAGS | JSON_PRETTY_PRINT);
    }

    /**
     * Decode one line of tape. Objects become associative arrays.
     *
     * @throws \JsonException on malformed JSON — the caller decides whether a torn final line
     *                        is tolerable (it is, exactly once, at the end of a tape).
     */
    public static function decode(string $s): mixed
    {
        return json_decode($s, true, 512, JSON_THROW_ON_ERROR | JSON_BIGINT_AS_STRING);
    }

    /**
     * Round a duration to the 2 decimal places the spec fixes for `call.ms`.
     *
     * Fixed by the spec so that two runtimes timing the same call write comparably-shaped
     * numbers, and so a tape diff never reports a difference that is only clock jitter.
     */
    public static function ms(float $milliseconds): float
    {
        return round($milliseconds, 2);
    }

    /**
     * A value's canonical rendering: object keys sorted, integral floats collapsed onto their
     * integer.
     *
     * Replay compares what the code asked now against what it asked when it was recorded, and
     * those two values travelled different roads — one through a live object, one through a
     * file. `30` and `30.0` are the same answer, and map key order is not information. Without
     * this, replay reports a divergence on a value that never changed.
     *
     * Note this is the opposite discipline from `encode()`, deliberately. Writing preserves
     * int-vs-float so a checker can reject `"seq": 1.0`; comparing collapses it so a round trip
     * through JSON is not itself a finding. Both are right for what they do.
     */
    public static function canonical(mixed $v): string
    {
        return self::encode(self::canonicalize($v));
    }

    private static function canonicalize(mixed $v): mixed
    {
        if (is_float($v) && is_finite($v) && floor($v) === $v && abs($v) < 1e15) {
            return (int) $v;
        }
        if ($v instanceof \stdClass) {
            $v = get_object_vars($v);
            if ($v === []) {
                return new \stdClass();
            }
        }
        if (is_array($v)) {
            if (array_is_list($v)) {
                return array_map([self::class, 'canonicalize'], $v);
            }
            ksort($v);
            $out = [];
            foreach ($v as $k => $x) {
                $out[(string) $k] = self::canonicalize($x);
            }
            return $out;
        }
        return $v;
    }

    /** Whether two jsonable values are the same answer, ignoring key order and 30-vs-30.0. */
    public static function equal(mixed $a, mixed $b): bool
    {
        return self::canonical($a) === self::canonical($b);
    }

    /**
     * True when this PHP is configured to round-trip doubles exactly.
     *
     * Called by the conformance suite rather than at import time: a library has no business
     * refusing to load because of an ini setting, but a test suite has every business failing
     * loudly, since the symptom otherwise is a tape that silently stops matching its peers.
     */
    public static function roundTripsFloats(): bool
    {
        return json_encode(0.1, self::ENCODE_FLAGS) === '0.1'
            && json_encode(1.0, self::ENCODE_FLAGS) === '1.0';
    }
}
