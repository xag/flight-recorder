<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Record what the outside world told your code, as one JSONL tape per session.
 *
 * The cardinal rule, for this class and for the boundary declarations it consumes:
 * **INSTRUMENT, NEVER DUPLICATE.** Nothing here evaluates a query, computes a date, or knows
 * what any value means. It records the questions your code asked the world and the answers it
 * got; on replay it feeds those answers back and checks the questions still match.
 *
 *     $rec = Recorder::open('.flight', $boundary);
 *     $out = $rec->call('study_status', ['user' => $user], function () use ($user) {
 *         $row  = Recorder::effect('store.get', [$user], fn () => $store->get($user));
 *         $seen = Recorder::now();
 *         return ['level' => $row['level'], 'seen' => $seen];
 *     });
 *
 * ## The ambient
 *
 * Exactly one of two things is installed at a time: a CallBuffer while recording, a Feed while
 * replaying. Every primitive below asks which, and does one of three things — run live and
 * unrecorded (no ambient), run live and write down what happened (recording), or serve the
 * recorded answer without running anything (replaying).
 *
 * PHP's share-nothing request model makes this a plain static: there is one request per
 * process-slot and no thread can inherit or lose it, so the `InheritableThreadLocal` dance that
 * Java needs — and the `Recorder.propagate` caveat that comes with it, whose failure mode is
 * silent under-recording — has no analogue here and no cost. The save-and-restore discipline in
 * `call()` remains, because calls nest.
 */
final class Recorder
{
    public const FORMAT_VERSION = 1;

    /** The call currently being recorded, if any. */
    public static ?CallBuffer $call = null;

    /** The tape currently being replayed from, if any. */
    public static ?Feed $feed = null;

    private static ?int $perfOrigin = null;

    private ?string $path = null;
    private int $seq = 0;
    private string $text = '';

    private function __construct(
        private readonly string $dir,
        private readonly Boundary $boundary,
    ) {
    }

    /**
     * Open a recorder over a directory.
     *
     * The directory is created; the file is not. The first call the gate admits creates it — so
     * a gate that never fires leaves nothing behind, and a process that records nothing is
     * indistinguishable from one with the recorder uninstalled.
     */
    public static function open(string $dir, Boundary $boundary): self
    {
        if (!is_dir($dir) && !@mkdir($dir, 0o777, true) && !is_dir($dir)) {
            throw new \RuntimeException("cannot create recording directory: $dir");
        }
        return new self($dir, $boundary);
    }

    /** The tape's path, or null until the first admitted call has written to it. */
    public function path(): ?string
    {
        return $this->path;
    }

    public function boundary(): Boundary
    {
        return $this->boundary;
    }

    /**
     * Record one tool call: its inputs, every answer the world gave it, and its outcome.
     *
     * The body's exception, if any, propagates **exactly as it was, never wrapped** — the
     * recorder's whole promise is that a recorded run behaves like an unrecorded one, and
     * wrapping would change what the app's own `catch` clauses see.
     *
     * @template T
     * @param  array<string, mixed> $kwargs
     * @param  callable(): T        $body
     * @return T
     */
    public function call(string $fn, array $kwargs, callable $body): mixed
    {
        if (!$this->boundary->admits($fn, $kwargs)) {
            return $body();
        }

        $prior = self::$call;
        $priorBoundary = self::setActiveBoundary($this->boundary);
        $buffer = new CallBuffer();
        self::$call = $buffer;

        $started = new \DateTimeImmutable();
        $t0 = hrtime(true);
        $result = null;
        $failure = null;
        try {
            $result = $body();
        } catch (\Throwable $e) {
            $failure = $e;
        } finally {
            self::$call = $prior;
            self::setActiveBoundary($priorBoundary);
        }
        $ms = Json::ms((hrtime(true) - $t0) / 1e6);

        $rules = $this->boundary->redact;
        $scrub = $this->boundary->scrub;
        $line = [
            'ev' => 'call',
            'seq' => $this->seq + 1,
            'fn' => $fn,
            'kwargs' => self::mapOf(Serial::redactJsonable(Serial::toJsonable($kwargs), $rules, $scrub)),
            'events' => $buffer->events,
            'result' => $failure !== null
                ? null
                : Serial::redactJsonable(Serial::toJsonable($result), $rules, $scrub),
            'error' => $failure === null ? null : self::render($failure),
            'ts' => Serial::isoOf($started),
            'ms' => $ms,
        ];
        if ($buffer->events === []) {
            $line['events'] = [];
        }

        try {
            $this->ensureOpen();
            $this->write(Json::encode($line));
            $this->seq++;
        } catch (ForbiddenValue $e) {
            // The one failure never swallowed — unless the body already failed, in which case
            // the app's own error is the more important truth and this one would mask it.
            if ($failure === null) {
                throw $e;
            }
        } catch (\Throwable $e) {
            if ($failure === null) {
                throw $e;
            }
        }

        if ($failure !== null) {
            throw $failure;
        }
        return $result;
    }

    /**
     * Write the session header, once, on the first admitted call.
     *
     * The header is vetted **before the file exists**. A tripwire hit here must leave no session
     * file at all: creating the file and then refusing to write into it would leave an empty
     * tape on disk, which reads as "a recording that captured nothing" rather than as "a
     * recording that was refused" — and the difference matters to whoever finds it later.
     */
    private function ensureOpen(): void
    {
        if ($this->path !== null) {
            return;
        }
        $rules = $this->boundary->redact;
        $scrub = $this->boundary->scrub;

        $header = ['ev' => 'session', 'version' => self::FORMAT_VERSION, 'php' => PHP_VERSION];
        $header['constants'] = self::mapOf(
            Serial::redactJsonable(Serial::toJsonable($this->boundary->constants), $rules, $scrub)
        );
        foreach ($this->boundary->headerExtras as $k => $v) {
            $header[$k] = Serial::redactJsonable(Serial::toJsonable($v), $rules, $scrub);
        }
        $header['started'] = Serial::isoOf(new \DateTimeImmutable());

        $rendered = Json::encode($header);
        $hit = Serial::forbiddenHit($rendered, $this->boundary->forbid);
        if ($hit !== null) {
            throw new ForbiddenValue($hit, 'the session header');
        }

        // The nonce is not decoration: two processes starting in the same second would
        // otherwise produce the same file name, and a name-keyed sink would have one silently
        // overwrite the other's tape.
        $name = sprintf(
            'flight-%s-%d-%08x.jsonl',
            (new \DateTimeImmutable())->format('Ymd-His'),
            getmypid() ?: 0,
            random_int(0, 0xFFFFFFFF)
        );
        $this->path = rtrim($this->dir, '/\\') . DIRECTORY_SEPARATOR . $name;
        $this->text = $rendered . "\n";
        file_put_contents($this->path, $this->text);
        $this->publish();
    }

    /**
     * Render, guard, write, publish — in that order, so nothing reaches a file or a sink
     * unvetted. Nothing is written on a hit.
     */
    private function write(string $rendered): void
    {
        $hit = Serial::forbiddenHit($rendered, $this->boundary->forbid);
        if ($hit !== null) {
            throw new ForbiddenValue($hit, 'a recorded call');
        }
        $this->text .= $rendered . "\n";
        file_put_contents($this->path, $rendered . "\n", FILE_APPEND);
        $this->publish();
    }

    /** Best-effort: a sink that throws must never be the reason a call fails. */
    private function publish(): void
    {
        if ($this->boundary->sink === null || $this->path === null) {
            return;
        }
        try {
            $this->boundary->sink->publish(basename($this->path), $this->text);
        } catch (\Throwable) {
            // swallowed on purpose
        }
    }

    /** The session's text so far, as a sink would have received it. */
    public function text(): string
    {
        return $this->text;
    }

    // --- the boundary primitives -------------------------------------------------------

    /**
     * The wall clock.
     *
     * PHP has one datetime type and it always carries a timezone, so — unlike Python, where
     * `datetime.now()` is naive and comparing it with an aware value raises — there is no
     * awareness distinction here for a replay to preserve. This emits an aware `now.v`, which
     * the spec permits (`now.v` MAY be naive, it is not required to be), and revives a naive
     * one off another runtime's tape into the default timezone.
     */
    public static function now(): \DateTimeImmutable
    {
        if (self::$feed !== null) {
            return self::$feed->now();
        }
        $t = new \DateTimeImmutable();
        self::emit(['k' => 'now', 'v' => Serial::isoOf($t)]);
        return $t;
    }

    /**
     * The monotonic clock, in milliseconds.
     *
     * A separate event kind from `now` because it is a separate clock: monotonic, arbitrary
     * origin, not a wall time. Feeding a wall time back into it would be a category error.
     */
    public static function perf(): float
    {
        if (self::$feed !== null) {
            return self::$feed->perf();
        }
        // Relative to first use, the way `performance.now()` is relative to page load. The
        // origin is arbitrary by spec, but `hrtime()` counts from boot, and a tape carrying
        // eight-digit millisecond readings tells a reader nothing except how long the machine
        // had been up.
        self::$perfOrigin ??= hrtime(true);
        $v = Json::ms((hrtime(true) - self::$perfOrigin) / 1e6);
        self::emit(['k' => 'perf', 'v' => $v]);
        return $v;
    }

    /**
     * One effect: a function whose (args → result/exception) IS the external world.
     *
     * Under record it calls the real thing and writes down what came back. Under replay it
     * returns the recorded answer without calling anything. **This is not a mock**: nothing
     * here knows what the effect does.
     *
     * @template T
     * @param  list<mixed>   $args
     * @param  callable(): T $real
     * @return T
     */
    public static function effect(string $name, array $args, callable $real): mixed
    {
        if (self::$feed !== null) {
            return self::$feed->answerEffect($name, $args);
        }
        if (self::$call === null) {
            return $real();
        }
        try {
            $res = $real();
        } catch (\Throwable $t) {
            self::emit([
                'k' => 'fx',
                'fn' => $name,
                'args' => self::listOf(Serial::toJsonable($args)),
                'kwargs' => new \stdClass(),
                'err' => [
                    'type' => self::shortName($t),
                    'repr' => self::cut(self::render($t), 300),
                    'args' => self::errorArgs($t),
                ],
            ]);
            throw $t;
        }
        self::emit([
            'k' => 'fx',
            'fn' => $name,
            'args' => self::listOf(Serial::toJsonable($args)),
            'kwargs' => new \stdClass(),
            'res' => Serial::toJsonable($res),
        ]);
        return $res;
    }

    /**
     * Wrap a client object so named methods are recorded and everything else passes through.
     *
     * PHP cannot patch a function the way Python patches a module — a global function is not a
     * rebindable name — so, as in Node, .NET, Go and Java, the boundary is the *object*. The
     * mechanism is `__call` on a decorator, which needs no interface: PHP dispatches undefined
     * method calls there at run time, so unlike Java's `reflect.Proxy` this works on a concrete
     * class with no interface to implement and no code generation.
     *
     * `$prefix` qualifies the recorded name (`kv.read`), so two clients never collide on the
     * tape.
     */
    public static function wrapAs(string $prefix, object $target, string ...$methods): Wrapped
    {
        return new Wrapped($prefix, $target, $methods);
    }

    /** Wrap using the target's short class name as the prefix. */
    public static function wrap(object $target, string ...$methods): Wrapped
    {
        $n = strrpos($target::class, '\\');
        return new Wrapped($n === false ? $target::class : substr($target::class, $n + 1), $target, $methods);
    }

    /**
     * Draw `k` positions from a population of `n`.
     *
     * Records POSITIONS, not members, which is what lets replay pick the same members from a
     * population a mutation has since changed.
     *
     * @return list<int>
     */
    public static function sampleIndices(int $n, int $k): array
    {
        if (self::$feed !== null) {
            return self::$feed->sample($n, $k);
        }
        $k = max(0, min($k, $n));
        $idx = [];
        $pool = range(0, max(0, $n - 1));
        for ($i = 0; $i < $k; $i++) {
            $j = random_int(0, count($pool) - 1);
            $idx[] = $pool[$j];
            array_splice($pool, $j, 1);
        }
        self::emit(['k' => 'rand', 'm' => 'sample', 'n' => $n, 'kk' => $k, 'idx' => $idx]);
        return $idx;
    }

    /** Raw entropy. The draw IS the value, so the bytes themselves are recorded. */
    public static function randBytes(int $n): string
    {
        if (self::$feed !== null) {
            return self::$feed->bytes($n);
        }
        $b = $n > 0 ? random_bytes($n) : '';
        self::emit(['k' => 'rand', 'm' => 'bytes', 'n' => $n, 'hex' => bin2hex($b)]);
        return $b;
    }

    /** A uniform draw in [0, 1). */
    public static function randFloat(): float
    {
        if (self::$feed !== null) {
            return self::$feed->randFloat();
        }
        // 2**53 is where doubles stop counting integers exactly; dividing by it gives a uniform
        // value in [0, 1) that can never round up to 1.0 and fail the spec's own range check.
        $v = random_int(0, (1 << 53) - 1) / (1 << 53);
        self::emit(['k' => 'rand', 'm' => 'float', 'v' => $v]);
        return $v;
    }

    /** A uniform integer draw in [0, n). */
    public static function randInt(int $n): int
    {
        if (self::$feed !== null) {
            return self::$feed->randInt();
        }
        $v = random_int(0, max(0, $n - 1));
        self::emit(['k' => 'rand', 'm' => 'int', 'v' => $v]);
        return $v;
    }

    /**
     * A chained read returning many documents.
     *
     * @param  callable(): list<Snapshot> $real
     * @return list<Snapshot>
     */
    public static function query(string $op, string $sig, callable $real): array
    {
        if (self::$feed !== null) {
            return self::$feed->answerQuery($op, $sig);
        }
        if (self::$call === null) {
            return $real();
        }
        $rows = $real();
        self::emit([
            'k' => 'db',
            'op' => $op,
            'sig' => $sig,
            'res' => array_values(array_map(
                static fn (Snapshot $s): array => Serial::snapshotJsonable($s),
                $rows
            )),
        ]);
        return $rows;
    }

    /**
     * A chained read returning one document.
     *
     * @param callable(): Snapshot $real
     */
    public static function queryOne(string $op, string $sig, callable $real): Snapshot
    {
        if (self::$feed !== null) {
            return self::$feed->answerQueryOne($op, $sig);
        }
        if (self::$call === null) {
            return $real();
        }
        $snap = $real();
        self::emit(['k' => 'db', 'op' => $op, 'sig' => $sig, 'res' => Serial::snapshotJsonable($snap)]);
        return $snap;
    }

    /**
     * A chained write.
     *
     * Under replay this is **not executed** — the question is compared against the tape and the
     * body is skipped. Replaying a run must not charge the card twice.
     *
     * @param list<mixed>  $args
     * @param callable(): void $real
     */
    public static function exec(string $op, string $sig, array $args, callable $real): void
    {
        if (self::$feed !== null) {
            self::$feed->expectWrite($op, $sig, $args);
            return;
        }
        if (self::$call === null) {
            $real();
            return;
        }
        $real();
        self::emit([
            'k' => 'db',
            'op' => $op,
            'sig' => $sig,
            'args' => self::listOf(Serial::toJsonable($args)),
        ]);
    }

    // --- semantic events: the app's own testimony ---------------------------------------

    /**
     * Mark a moment in the app's own vocabulary.
     *
     * Testimony, never evidence: nothing interprets `$name`, and replay never feeds one back.
     *
     * @param array<string, mixed> $data
     */
    public static function note(string $name, array $data = []): void
    {
        if (self::$feed !== null) {
            self::$feed->note($name);
            return;
        }
        $call = self::$call;
        if ($call === null) {
            return;
        }
        $ev = ['k' => 'sem', 'name' => $name, 'phase' => 'point', 'sid' => $call->nextSid()];
        if ($data !== []) {
            $ev['data'] = self::semData($data);
        }
        self::emit($ev);
    }

    /**
     * Wrap a stretch of execution in a named span, so the tape reads as testimony next to its
     * evidence.
     *
     * If the body throws, the `end` **still lands**, with `outcome: "error"`, and the exception
     * propagates untouched. A span that vanished on the error path would make a failed run look
     * like a run that told a shorter story — a different and much more confusing finding.
     *
     * @template T
     * @param  array<string, mixed> $data
     * @param  callable(): T        $body
     * @return T
     */
    public static function span(string $name, array $data, callable $body): mixed
    {
        if (self::$feed !== null) {
            return self::$feed->span($name, $body);
        }
        $call = self::$call;
        if ($call === null) {
            return $body();
        }
        $sid = $call->nextSid();
        $begin = ['k' => 'sem', 'name' => $name, 'phase' => 'begin', 'sid' => $sid];
        if ($data !== []) {
            $begin['data'] = self::semData($data);
        }
        self::emit($begin);

        $outcome = 'ok';
        try {
            return $body();
        } catch (\Throwable $t) {
            $outcome = 'error';
            throw $t;
        } finally {
            self::emit([
                'k' => 'sem', 'name' => $name, 'phase' => 'end', 'sid' => $sid, 'outcome' => $outcome,
            ]);
        }
    }

    /** Span data is redacted exactly like a boundary value — a claim can quote a secret. */
    private static function semData(array $data): array|\stdClass
    {
        $b = self::activeBoundary();
        $j = Serial::toJsonable($data);
        if ($b !== null) {
            $j = Serial::redactJsonable($j, $b->redact, $b->scrub);
        }
        return self::mapOf($j);
    }

    // --- internals ----------------------------------------------------------------------

    /**
     * The boundary in force for the running call, used to redact in-flight event payloads.
     *
     * Set by `call()` for the duration of a call so a static primitive — which has no recorder
     * instance to ask — can still mask what it is about to buffer.
     */
    private static ?Boundary $active = null;

    public static function activeBoundary(): ?Boundary
    {
        return self::$active;
    }

    public static function setActiveBoundary(?Boundary $b): ?Boundary
    {
        $prior = self::$active;
        self::$active = $b;
        return $prior;
    }

    /**
     * Buffer one event, redacted and vetted first.
     *
     * The guard runs **before the buffer append**, not merely before the file write: the buffer
     * is what becomes the call record, and an invariant can read these events while the run is
     * still going. "In memory" is a statement about latency, not about confinement.
     */
    private static function emit(array $event): void
    {
        $call = self::$call;
        if ($call === null) {
            return;
        }
        $b = self::$active;
        if ($b !== null) {
            $event = self::scrubEvent($event, $b);
            $hit = Serial::forbiddenHit(Json::encode($event), $b->forbid);
            if ($hit !== null) {
                throw new ForbiddenValue($hit, 'a boundary event');
            }
        }
        $call->add($event);
    }

    /**
     * Sweep the payload-bearing keys of one event.
     *
     * Exactly these six. `err` carries a message the app built, which is a classic place for a
     * secret to be interpolated; missing one of these is how a redacted tape leaks anyway.
     */
    private static function scrubEvent(array $event, Boundary $b): array
    {
        foreach (['args', 'kwargs', 'res', 'err', 'result', 'data'] as $key) {
            if (array_key_exists($key, $event)) {
                $event[$key] = Serial::redactJsonable($event[$key], $b->redact, $b->scrub);
            }
        }
        return $event;
    }

    /** A value the tape requires to be an object, forced to `{}` when it is empty. */
    public static function mapOf(mixed $v): array|\stdClass
    {
        if ($v instanceof \stdClass) {
            return $v;
        }
        if (is_array($v)) {
            return $v === [] ? new \stdClass() : $v;
        }
        return new \stdClass();
    }

    /** A value the tape requires to be an array. */
    public static function listOf(mixed $v): array
    {
        return is_array($v) ? array_values($v) : [];
    }

    /** An error's rendering: its message, or its short class name when the message is empty. */
    public static function render(\Throwable $t): string
    {
        $m = $t->getMessage();
        return $m === '' ? self::shortName($t) : $m;
    }

    public static function shortName(\Throwable $t): string
    {
        $c = $t::class;
        $n = strrpos($c, '\\');
        return $n === false ? $c : substr($c, $n + 1);
    }

    /**
     * The constructive values of a recorded error.
     *
     * A structured error can carry its own, so a reviver can rebuild it faithfully on replay
     * rather than guessing from a rendering.
     *
     * @return list<mixed>
     */
    private static function errorArgs(\Throwable $t): array
    {
        if ($t instanceof FlightError) {
            return array_values(Serial::toJsonable($t->errorArgs()));
        }
        return [self::render($t)];
    }

    private static function cut(string $s, int $limit): string
    {
        return strlen($s) <= $limit ? $s : substr($s, 0, $limit - 1) . "\u{2026}";
    }

    /** Build a kwargs map from alternating name/value pairs, for callers who prefer it. */
    public static function kwargs(mixed ...$pairs): array
    {
        $out = [];
        for ($i = 0; $i + 1 < count($pairs); $i += 2) {
            $out[(string) $pairs[$i]] = $pairs[$i + 1];
        }
        return $out;
    }
}
