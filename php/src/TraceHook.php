<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * What instrumented code calls.
 *
 * **Public because it is a compile target** — the rewriter splices calls to these methods into a
 * copy of your sources, and that copy has to be able to see them.
 *
 * Two absolute rules govern everything here:
 *
 * 1. **It must never throw into the observed frame.** Every entry point is wrapped. An exception
 *    raised here would propagate into the very execution the trace exists to explain, turning
 *    the instrument into the bug.
 * 2. **It must never change what it observes.** Nothing is handed back to the program except by
 *    the identity passthrough in `returned()`, and no value is stringified outside the encoder's
 *    own guarded path.
 */
final class TraceHook
{
    public const ENV_PATH = 'FLIGHT_RECORDER_TRACE';
    public const ENV_FORBID = 'FLIGHT_RECORDER_TRACE_FORBID';
    public const REFUSAL_SUFFIX = '.forbidden';

    private static ?TraceSink $sink = null;
    private static int $frames = 0;

    /** @var array<int, TraceFrame> */
    private static array $table = [];

    public static function setSink(?TraceSink $s): ?TraceSink
    {
        $prior = self::$sink;
        self::$sink = $s;
        return $prior;
    }

    public static function sink(): ?TraceSink
    {
        return self::$sink;
    }

    public static function live(): bool
    {
        return self::$sink !== null;
    }

    public static function count(): int
    {
        return self::$sink?->count() ?? 0;
    }

    /** Where the tracer's tape stands right now, so a replay can take only what follows. */
    public static function mark(): int
    {
        return self::count();
    }

    /** Everything traced since `$from`. */
    public static function since(int $from): Trace
    {
        return self::$sink?->snapshot($from) ?? Trace::empty();
    }

    public static function refusalPath(string $tracePath): string
    {
        return $tracePath . self::REFUSAL_SUFFIX;
    }

    public static function at(string $file, int $line): string
    {
        return basename($file) . ':' . $line;
    }

    /**
     * A function was entered. Returns the frame id the rewriter threads through the other hooks.
     *
     * @param array<string, mixed> $vars the frame's locals, as `get_defined_vars()` gave them
     */
    public static function enter(string $fn, string $at, array $vars): int
    {
        try {
            if (self::$sink === null) {
                return 0;
            }
            $id = ++self::$frames;
            $frame = new TraceFrame($at, $fn);
            $args = [];
            foreach (self::observable($vars) as $name => $value) {
                $enc = Serial::traceJsonable($value);
                $frame->seen[$name] = self::key($enc);
                $args[$name] = $enc;
            }
            self::$table[$id] = $frame;
            self::$sink->emit(['e' => 'C', 'fn' => $fn, 'at' => $at, 'args' => Recorder::mapOf($args)]);
            return $id;
        } catch (\Throwable) {
            return 0;
        }
    }

    /**
     * A statement is about to run: report what the *previous* one changed.
     *
     * **The delta is reported at the PREVIOUS statement's location, not this one.** A hook fires
     * *before* a statement runs, so what it sees is the work the last statement did; blaming the
     * upcoming line would put the wrong line number on every value in the trace. This is the
     * single easiest thing to get wrong in a tracer, and it is wrong in a way that looks
     * plausible.
     *
     * @param array<string, mixed> $vars
     */
    public static function line(int $frame, string $fn, string $at, array $vars): void
    {
        try {
            $f = self::$table[$frame] ?? null;
            if (self::$sink === null || $f === null) {
                return;
            }
            $delta = [];
            foreach (self::observable($vars) as $name => $value) {
                $enc = Serial::traceJsonable($value);
                $k = self::key($enc);
                if (($f->seen[$name] ?? null) !== $k) {
                    $f->seen[$name] = $k;
                    $delta[$name] = $enc;
                }
            }
            $reportAt = $f->lastAt;
            $f->lastAt = $at;
            if ($delta !== []) {
                self::$sink->emit(['e' => 'L', 'fn' => $fn, 'at' => $reportAt, 'd' => $delta]);
            }
        } catch (\Throwable) {
            // never into the observed frame
        }
    }

    /**
     * A value is being returned. **Identity passthrough.**
     *
     * The rewriter wraps `return $expr` as `return TraceHook::returned($f, …, $expr)` rather than
     * declaring a temporary, so the rewrite can never change a type, a reference, or an
     * evaluation order the original relied on.
     *
     * @template T
     * @param  T $value
     * @return T
     */
    public static function returned(int $frame, string $fn, string $at, mixed $value): mixed
    {
        try {
            if (self::$sink !== null && isset(self::$table[$frame])) {
                self::$sink->emit([
                    'e' => 'R', 'fn' => $fn, 'at' => $at, 'v' => Serial::traceJsonable($value),
                ]);
            }
        } catch (\Throwable) {
            // never into the observed frame
        }
        return $value;
    }

    public static function raise(int $frame, string $fn, string $at, \Throwable $t): void
    {
        try {
            if (self::$sink !== null && isset(self::$table[$frame])) {
                self::$sink->emit([
                    'e' => 'X',
                    'fn' => $fn,
                    'at' => $at,
                    'type' => Recorder::shortName($t),
                    'v' => $t->getMessage(),
                ]);
            }
        } catch (\Throwable) {
            // never into the observed frame
        }
    }

    /**
     * The frame is done.
     *
     * Named `leave` rather than `exit` because `exit` is a language construct in PHP and a
     * spliced `TraceHook::exit(...)` reads badly even where the parser tolerates it.
     */
    public static function leave(int $frame): void
    {
        // So a long run does not accumulate one entry per invocation forever.
        unset(self::$table[$frame]);
    }

    /** Reset between traced runs, so one run's frames never leak into the next. */
    public static function reset(): void
    {
        self::$table = [];
        self::$frames = 0;
    }

    /**
     * The observable locals: everything the author wrote, and nothing the rewriter added.
     *
     * `get_defined_vars()` cannot help seeing the frame variable spliced in beside them. Tracing
     * the tracer's own bookkeeping would be noise in every delta of every line.
     *
     * @param  array<string, mixed> $vars
     * @return array<string, mixed>
     */
    private static function observable(array $vars): array
    {
        $out = [];
        foreach ($vars as $name => $value) {
            if (!str_starts_with((string) $name, '__fr')) {
                $out[(string) $name] = $value;
            }
        }
        return $out;
    }

    private static function key(mixed $encoded): string
    {
        try {
            return Json::canonical($encoded);
        } catch (\Throwable) {
            return '<unencodable>';
        }
    }
}
