<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Spec;

/**
 * Tape v1 conformance checker — the PHP mirror.
 *
 * `spec/tape-v1.md` is the prose; the checkers are the arbiter. This one is deliberately
 * written against nothing but the JSON: it imports no part of the recorder, so it cannot
 * accidentally bless whatever the PHP implementation happens to do.
 *
 * The six files (Python, JS, .NET, Go, Java, PHP) are the same claim written six times, on
 * purpose. The tape is the contract between runtimes, and a contract checked by one party's
 * code is not checked at all — so each runtime validates every fixture, including the five it
 * did not produce, and a fork of the format fails somebody else's build.
 *
 * Returns a list of human-readable violations; empty means conformant.
 */
final class Validate
{
    public const VERSION = 1;
    public const MAX_DEPTH = 16;

    /**
     * `__undef__` exists for JavaScript, which has two nothings. PHP has one, so a PHP
     * recorder never emits it and a PHP reader revives it as null — the marker costs this
     * runtime nothing and buys the other one exact fidelity.
     */
    private const MARKERS = ['__dt__', '__date__', '__undef__', '__opaque__'];

    /**
     * Reserved by the trace encoding — a *reader* must tolerate them, so they are legal in a
     * tape even though a v1 recorder never emits them.
     */
    private const RESERVED_MARKERS = ['__snap__', '__seq__', '__str__', '__esc__'];

    private const EVENT_KINDS = ['fx', 'db', 'now', 'perf', 'rand', 'sem'];
    private const SEM_PHASES = ['begin', 'end', 'point'];

    /**
     * The runtime names a session header may carry, exactly one of which must be present.
     *
     * Adding a name here is additive — a reader that does not know it ignores the key — but
     * every checker carries the recognized set, so a new runtime lands in all six or its tapes
     * fail conformance in the five it did not teach.
     */
    private const RUNTIMES = ['python', 'node', 'dotnet', 'go', 'java', 'php'];

    /**
     * Validate a whole tape.
     *
     * @return list<string> violations; empty means conformant
     */
    public static function tape(string $text): array
    {
        $out = [];
        $lines = array_values(array_filter(
            explode("\n", $text),
            static fn (string $ln): bool => trim($ln) !== ''
        ));
        if ($lines === []) {
            return ['empty tape: the session header is mandatory'];
        }

        $seqs = [];
        $last = count($lines) - 1;
        foreach ($lines as $i => $ln) {
            try {
                $obj = json_decode($ln, true, 512, JSON_THROW_ON_ERROR);
            } catch (\JsonException $e) {
                // Only the final line may be torn (the process died mid-write).
                if ($i === $last) {
                    continue;
                }
                $out[] = "line $i: not JSON ({$e->getMessage()})";
                continue;
            }
            self::line($obj, $i, $out, $i === 0);
            if (is_array($obj) && ($obj['ev'] ?? null) === 'call' && is_int($obj['seq'] ?? null)) {
                $seqs[] = $obj['seq'];
            }
        }

        $expected = range(1, count($seqs));
        if ($seqs !== [] && $seqs !== $expected) {
            $out[] = 'call.seq must be 1-based and monotonic; got [' . implode(', ', $seqs) . ']';
        }

        return $out;
    }

    /** Validate a tape file on disk. @return list<string> */
    public static function file(string $path): array
    {
        $text = @file_get_contents($path);
        if ($text === false) {
            return ["cannot read tape: $path"];
        }
        return self::tape($text);
    }

    /** @param list<string> $out */
    public static function line(mixed $obj, int $i, array &$out, bool $first): void
    {
        if (!is_array($obj) || array_is_list($obj)) {
            $out[] = "line $i: not an object";
            return;
        }
        $ev = $obj['ev'] ?? null;

        if ($first) {
            if ($ev !== 'session') {
                $out[] = "line $i: the first line must be the session header, got ev="
                    . self::show($ev);
                return;
            }
        } elseif ($ev === 'session') {
            $out[] = "line $i: a second session header";
            return;
        }

        if ($ev === 'session') {
            self::session($obj, $i, $out);
            return;
        }
        if ($ev === 'call') {
            self::call($obj, $i, $out);
            return;
        }

        // unknown ev (e.g. the reserved "inflight"): a reader must tolerate it.
    }

    /** @param list<string> $out */
    private static function session(array $obj, int $i, array &$out): void
    {
        if (($obj['version'] ?? null) !== self::VERSION) {
            $out[] = "line $i: version must be " . self::VERSION . ', got '
                . self::show($obj['version'] ?? null);
        }
        if (!self::isTzAware($obj['started'] ?? null)) {
            $out[] = "line $i: session.started must be timezone-aware ISO-8601";
        }
        $constants = $obj['constants'] ?? null;
        if (!is_array($constants) || array_is_list($constants) && $constants !== []) {
            $out[] = "line $i: session.constants must be an object";
        } else {
            self::value($constants, "line $i.constants", $out);
        }

        $runtimes = array_values(array_filter(
            self::RUNTIMES,
            static fn (string $k): bool => array_key_exists($k, $obj)
        ));
        if (count($runtimes) !== 1) {
            $out[] = "line $i: session must name exactly one runtime ("
                . implode('|', self::RUNTIMES) . '), got [' . implode(', ', $runtimes) . ']';
        }
    }

    /** @param list<string> $out */
    private static function call(array $obj, int $i, array &$out): void
    {
        $seq = $obj['seq'] ?? null;
        if (!is_int($seq) || $seq < 1) {
            $out[] = "line $i: call.seq must be an int >= 1";
        }
        if (!is_string($obj['fn'] ?? null)) {
            $out[] = "line $i: call.fn must be a string";
        }
        $kwargs = $obj['kwargs'] ?? null;
        if (!self::isObject($kwargs)) {
            $out[] = "line $i: call.kwargs must be an object";
        } else {
            self::value($kwargs, "line $i.kwargs", $out);
        }
        if (array_key_exists('result', $obj)) {
            self::value($obj['result'], "line $i.result", $out);
        }
        if (!array_key_exists('error', $obj)) {
            $out[] = "line $i: call must carry 'error' (null when it did not raise)";
        } elseif ($obj['error'] !== null && !is_string($obj['error'])) {
            $out[] = "line $i: call.error must be a string or null";
        }
        if (!self::isTzAware($obj['ts'] ?? null)) {
            $out[] = "line $i: call.ts must be timezone-aware ISO-8601";
        }
        $ms = $obj['ms'] ?? null;
        if (!is_int($ms) && !is_float($ms)) {
            $out[] = "line $i: call.ms must be a number";
        }
        $evs = $obj['events'] ?? null;
        if (!is_array($evs) || !array_is_list($evs)) {
            $out[] = "line $i: call.events must be an array";
        } else {
            foreach ($evs as $j => $e) {
                self::event($e, "line $i.events[$j]", $out);
            }
            self::semNesting($evs, "line $i", $out);
        }
    }

    /**
     * A boundary value: JSON, with at most a marker at any node.
     *
     * @param list<string> $out
     */
    public static function value(mixed $v, string $path, array &$out, int $depth = 0): void
    {
        if ($depth > self::MAX_DEPTH) {
            $out[] = "$path: nested deeper than " . self::MAX_DEPTH
                . '; must degrade to __opaque__';
            return;
        }
        if ($v === null || is_string($v) || is_int($v) || is_float($v) || is_bool($v)) {
            return;
        }
        if (!is_array($v)) {
            $out[] = "$path: " . get_debug_type($v) . ' is not JSON';
            return;
        }
        if (array_is_list($v)) {
            foreach ($v as $i => $x) {
                self::value($x, "{$path}[$i]", $out, $depth + 1);
            }
            return;
        }
        if (count($v) === 1) {
            $k = (string) array_key_first($v);
            $payload = $v[array_key_first($v)];
            if (in_array($k, self::MARKERS, true)) {
                if (($k === '__dt__' || $k === '__date__') && !self::isIso($payload)) {
                    $out[] = "$path: $k payload is not ISO-8601: " . self::show($payload);
                }
                if ($k === '__undef__' && $payload !== true) {
                    $out[] = "$path: __undef__ payload must be true";
                }
                if ($k === '__opaque__') {
                    if (!is_string($payload)) {
                        $out[] = "$path: __opaque__ payload must be a string";
                    } elseif (mb_strlen($payload, 'UTF-8') > 200) {
                        $out[] = "$path: __opaque__ payload exceeds 200 chars";
                    }
                }
                return;
            }
            if (in_array($k, self::RESERVED_MARKERS, true)) {
                return; // reserved: legal, not interpreted here
            }
        }
        foreach ($v as $k => $x) {
            self::value($x, "$path.$k", $out, $depth + 1);
        }
    }

    /** @param list<string> $out */
    private static function snapshot(mixed $s, string $path, array &$out): void
    {
        if (!self::isObject($s)) {
            $out[] = "$path: snapshot must be an object";
            return;
        }
        foreach (['id', 'exists', 'data'] as $key) {
            if (!array_key_exists($key, $s)) {
                $out[] = "$path: snapshot missing '$key'";
            }
        }
        if (array_key_exists('exists', $s) && !is_bool($s['exists'])) {
            $out[] = "$path.exists: must be a bool";
        }
        if (array_key_exists('data', $s)) {
            self::value($s['data'], "$path.data", $out);
        }
    }

    /** @param list<string> $out */
    public static function event(mixed $e, string $path, array &$out): void
    {
        if (!self::isObject($e)) {
            $out[] = "$path: event must be an object";
            return;
        }
        $k = $e['k'] ?? null;
        if (!in_array($k, self::EVENT_KINDS, true)) {
            return; // unknown kind: a reader must ignore it (forward compatibility)
        }

        match ($k) {
            'fx' => self::eventFx($e, $path, $out),
            'db' => self::eventDb($e, $path, $out),
            'now' => self::eventNow($e, $path, $out),
            'perf' => self::eventPerf($e, $path, $out),
            'sem' => self::eventSem($e, $path, $out),
            'rand' => self::eventRand($e, $path, $out),
        };
    }

    /** @param list<string> $out */
    private static function eventFx(array $e, string $path, array &$out): void
    {
        if (!is_string($e['fn'] ?? null)) {
            $out[] = "$path: fx needs a string 'fn'";
        }
        $args = $e['args'] ?? null;
        if (!is_array($args) || !array_is_list($args)) {
            $out[] = "$path: fx needs an array 'args'";
        } else {
            self::value($args, "$path.args", $out);
        }
        $kwargs = $e['kwargs'] ?? null;
        if (!self::isObject($kwargs)) {
            $out[] = "$path: fx needs an object 'kwargs' ({} in JS and PHP)";
        } else {
            self::value($kwargs, "$path.kwargs", $out);
        }
        $hasRes = array_key_exists('res', $e);
        $hasErr = array_key_exists('err', $e);
        if ($hasRes === $hasErr) {
            $out[] = "$path: fx must carry exactly one of 'res' / 'err'";
        }
        if ($hasRes) {
            self::value($e['res'], "$path.res", $out);
        }
        if ($hasErr) {
            $err = $e['err'];
            if (!self::isObject($err) || !is_string($err['type'] ?? null)) {
                $out[] = "$path.err: must be an object with a string 'type'";
            }
        }
    }

    /** @param list<string> $out */
    private static function eventDb(array $e, string $path, array &$out): void
    {
        if (!is_string($e['op'] ?? null)) {
            $out[] = "$path: db needs a string 'op'";
        }
        if (!is_string($e['sig'] ?? null)) {
            $out[] = "$path: db needs a string 'sig'";
        }
        $hasRes = array_key_exists('res', $e);
        $hasArgs = array_key_exists('args', $e);
        if ($hasRes && $hasArgs) {
            $out[] = "$path: db carries 'res' (a read) or 'args' (a write), never both";
        }
        if (!$hasRes && !$hasArgs) {
            $out[] = "$path: db must carry 'res' or 'args'";
        }
        if ($hasRes) {
            $r = $e['res'];
            if (is_array($r) && array_is_list($r)) {
                foreach ($r as $i => $s) {
                    self::snapshot($s, "$path.res[$i]", $out);
                }
            } else {
                self::snapshot($r, "$path.res", $out);
            }
        }
        if ($hasArgs) {
            self::value($e['args'], "$path.args", $out);
        }
    }

    /**
     * ISO-8601, and deliberately NOT required to be timezone-aware.
     *
     * This is an app-visible value, not recorder metadata: the app called now() and got back
     * whatever it got back. Python's `datetime.now()` is naive, and there comparing a naive
     * datetime with an aware one raises — so a replay that "helpfully" handed back an aware
     * value where the recording saw a naive one would change behaviour, which is the one thing
     * replay may never do. Round-trip exactly what the app saw.
     *
     * @param list<string> $out
     */
    private static function eventNow(array $e, string $path, array &$out): void
    {
        if (!self::isIso($e['v'] ?? null)) {
            $out[] = "$path: now.v must be an ISO-8601 string, got " . self::show($e['v'] ?? null);
        }
    }

    /**
     * A separate kind from `now` because it is a separate clock: monotonic, arbitrary origin,
     * not a wall time. Feeding a wall time back into it would be a category error.
     *
     * @param list<string> $out
     */
    private static function eventPerf(array $e, string $path, array &$out): void
    {
        $v = $e['v'] ?? null;
        if (!is_int($v) && !is_float($v)) {
            $out[] = "$path: perf.v must be a number (milliseconds), got " . self::show($v);
        }
    }

    /**
     * Testimony, not evidence.
     *
     * The checker validates its SHAPE and says nothing about its content: `name` is the app's
     * own vocabulary and no implementation may interpret it. A checker that knew what a span
     * name meant would have given the library semantics, which is the one thing the library is
     * not allowed to have.
     *
     * @param list<string> $out
     */
    private static function eventSem(array $e, string $path, array &$out): void
    {
        $name = $e['name'] ?? null;
        if (!is_string($name) || $name === '') {
            $out[] = "$path: sem needs a non-empty string 'name'";
        }
        $phase = $e['phase'] ?? null;
        if (!in_array($phase, self::SEM_PHASES, true)) {
            $out[] = "$path: sem.phase must be one of begin|end|point, got " . self::show($phase);
        }
        if (!is_int($e['sid'] ?? null)) {
            $out[] = "$path: sem needs an int 'sid', unique within the call";
        }
        if (array_key_exists('data', $e)) {
            if (!self::isObject($e['data'])) {
                $out[] = "$path: sem.data must be an object";
            } else {
                self::value($e['data'], "$path.data", $out);
            }
        }
        if (array_key_exists('outcome', $e)) {
            if ($phase !== 'end') {
                $out[] = "$path: sem.outcome belongs to an 'end', not a " . self::show($phase);
            }
            if (!in_array($e['outcome'], ['ok', 'error'], true)) {
                $out[] = "$path: sem.outcome must be 'ok' or 'error', got "
                    . self::show($e['outcome']);
            }
        }
    }

    /** @param list<string> $out */
    private static function eventRand(array $e, string $path, array &$out): void
    {
        $m = $e['m'] ?? null;
        if ($m === 'sample') {
            foreach (['n', 'kk'] as $key) {
                if (!is_int($e[$key] ?? null)) {
                    $out[] = "$path: rand.$key must be an int";
                }
            }
            $idx = $e['idx'] ?? null;
            $allInts = is_array($idx) && array_is_list($idx);
            if ($allInts) {
                foreach ($idx as $x) {
                    if (!is_int($x)) {
                        $allInts = false;
                        break;
                    }
                }
            }
            if (!$allInts) {
                $out[] = "$path: rand.idx must be an array of ints";
            } elseif (is_int($e['n'] ?? null)) {
                $bad = array_values(array_filter(
                    $idx,
                    static fn (int $x): bool => $x < 0 || $x >= $e['n']
                ));
                if ($bad !== []) {
                    $out[] = "$path: rand.idx [" . implode(', ', $bad)
                        . "] out of range for population {$e['n']}";
                }
                if (is_int($e['kk'] ?? null) && count($idx) !== $e['kk']) {
                    $out[] = "$path: rand.idx has " . count($idx)
                        . " positions but kk={$e['kk']}";
                }
            }
        } elseif ($m === 'bytes') {
            $n = $e['n'] ?? null;
            if (!is_int($n) || $n < 0) {
                $out[] = "$path: rand.n must be a non-negative int";
            }
            $hx = $e['hex'] ?? null;
            if (!is_string($hx) || ($hx !== '' && preg_match('/^[0-9a-f]+$/', $hx) !== 1)) {
                $out[] = "$path: rand.hex must be a lowercase hex string";
            } elseif (is_int($n) && strlen($hx) !== 2 * $n) {
                $out[] = "$path: rand.hex is " . strlen($hx) . " chars but n=$n implies " . 2 * $n;
            }
        } elseif ($m === 'float') {
            $v = $e['v'] ?? null;
            if ((!is_int($v) && !is_float($v)) || $v < 0.0 || $v >= 1.0) {
                $out[] = "$path: rand.v must be a number in [0, 1), got " . self::show($v);
            }
        } elseif ($m === 'int') {
            if (!is_int($e['v'] ?? null)) {
                $out[] = "$path: rand.v must be an int, got " . self::show($e['v'] ?? null);
            }
        } else {
            $out[] = "$path: rand.m must be one of sample|bytes|float|int, got " . self::show($m);
        }
    }

    /**
     * The one structural promise `sem` makes: begin/end pairs are well-nested within a call.
     *
     * Enclosure is derived from ORDER — a span contains every event between its begin and its
     * end — so nesting is not decoration, it is the only thing that makes the derivation sound.
     * Two spans that straddle (A begins, B begins, A ends, B ends) would put an event inside
     * both and inside neither, and every reader that walks the stream would build a different
     * tree.
     *
     * A span left open by a process that died mid-call is a separate matter and not a violation
     * here: that call never reached the tape at all. It lives in the `inflight` sidecar, which
     * is an unknown `ev` to this checker, and where an unclosed span is exactly the information
     * the reader wants.
     *
     * @param list<mixed>  $evs
     * @param list<string> $out
     */
    private static function semNesting(array $evs, string $path, array &$out): void
    {
        /** @var list<array{int, string}> $stack */
        $stack = [];
        $seen = [];
        foreach ($evs as $j => $e) {
            if (!self::isObject($e) || ($e['k'] ?? null) !== 'sem') {
                continue;
            }
            $sid = $e['sid'] ?? null;
            $phase = $e['phase'] ?? null;
            $name = (string) ($e['name'] ?? '');
            if (!is_int($sid) || !in_array($phase, self::SEM_PHASES, true)) {
                continue; // already reported by event(); do not compound it
            }

            if ($phase === 'begin' || $phase === 'point') {
                if (isset($seen[$sid])) {
                    $out[] = "{$path}.events[$j]: sem sid $sid is reused — a sid must be unique "
                        . "within the call, or an 'end' cannot name its 'begin'";
                }
                $seen[$sid] = true;
                if ($phase === 'begin') {
                    $stack[] = [$sid, $name];
                }
            } else { // end
                if ($stack === []) {
                    $out[] = "{$path}.events[$j]: sem 'end' (sid $sid) with no open span";
                } elseif ($stack[count($stack) - 1][0] !== $sid) {
                    [$openSid, $openName] = $stack[count($stack) - 1];
                    $out[] = "{$path}.events[$j]: sem spans are not well-nested — 'end' closes "
                        . "sid $sid while sid $openSid ('$openName') is still open. Spans nest; "
                        . 'they never straddle.';
                    // Unwind to it if it is open at all, so one crossing is not reported N times.
                    $open = false;
                    foreach ($stack as [$s, $_]) {
                        if ($s === $sid) {
                            $open = true;
                            break;
                        }
                    }
                    if ($open) {
                        while ($stack !== [] && $stack[count($stack) - 1][0] !== $sid) {
                            array_pop($stack);
                        }
                        array_pop($stack);
                    }
                } else {
                    array_pop($stack);
                }
            }
        }

        foreach ($stack as [$sid, $name]) {
            $out[] = "$path: sem span '$name' (sid $sid) is never closed — a completed call "
                . 'holds no open spans';
        }
    }

    /** A JSON object, as `json_decode(..., true)` renders one. */
    private static function isObject(mixed $v): bool
    {
        return is_array($v) && (!array_is_list($v) || $v === []);
    }

    /**
     * ISO-8601 date or datetime, with an optional offset.
     *
     * Written as a pattern rather than handed to PHP's date parser on purpose: `new
     * DateTimeImmutable()` accepts "now", "tuesday" and a great deal else, so using it as the
     * test would bless strings no other runtime's checker would accept — and the checkers
     * agreeing is the entire point of there being six of them.
     */
    private static function isIso(mixed $s): bool
    {
        if (!is_string($s)) {
            return false;
        }
        return preg_match(
            '/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/',
            $s
        ) === 1;
    }

    private static function isTzAware(mixed $s): bool
    {
        if (!self::isIso($s)) {
            return false;
        }
        return preg_match('/(Z|[+-]\d{2}:?\d{2})$/', (string) $s) === 1;
    }

    /** Render a value for a violation message, close to how the other checkers render it. */
    private static function show(mixed $v): string
    {
        if (is_string($v)) {
            return "'" . $v . "'";
        }
        if ($v === null) {
            return 'null';
        }
        if (is_bool($v)) {
            return $v ? 'true' : 'false';
        }
        if (is_array($v)) {
            return json_encode($v, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?: 'array';
        }
        return (string) $v;
    }
}
