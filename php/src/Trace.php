<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A traced execution's internal state, queryable: every local, on every executed line.
 *
 * This is the thing that turns "what was `$level` when it went wrong?" from an inference into a
 * lookup — and, with it, an invariant can assert over an **internal** variable, which is the
 * form that catches a bug whose output is perfectly self-consistent and still wrong.
 *
 * ## The artifact
 *
 * JSONL, and deliberately a different file from the tape: a tape is the world's answers, a
 * trace is the code's insides. Header `{"e":"H","trace_version":2}`, then one event per line:
 * `C` a call, `L` a line's changed locals, `R` a return, `X` a raise.
 *
 * **Trace version 2: a traced value is DATA, not a rendering.** Version 1 recorded reprs, and
 * an invariant asserting arithmetic over a repr fails confusingly rather than loudly — `"42"` is
 * not `42`, and finding that out from a failing comparison six frames away is miserable. A
 * version 1 trace is refused rather than half-understood; traces are cheap, regenerate.
 */
final class Trace
{
    public const TRACE_VERSION = 2;

    /** @param list<array<string, mixed>> $events */
    public function __construct(public readonly array $events = [])
    {
    }

    /**
     * An empty trace — empty, never null.
     *
     * A query against it answers "never observed", so a claim about an untraced variable fails
     * honestly instead of passing vacuously. That is the difference between an invariant that is
     * satisfied and one that was never actually checked.
     */
    public static function empty(): self
    {
        return new self([]);
    }

    public static function load(string $path): self
    {
        $text = @file_get_contents($path);
        if ($text === false) {
            throw new \RuntimeException("cannot read trace: $path");
        }
        return self::parse($text);
    }

    public static function parse(string $text): self
    {
        $events = [];
        $lines = array_values(array_filter(
            explode("\n", $text),
            static fn (string $l): bool => trim($l) !== ''
        ));
        foreach ($lines as $i => $ln) {
            try {
                $obj = Json::decode($ln);
            } catch (\JsonException) {
                if ($i === count($lines) - 1) {
                    continue; // only the final line may be torn
                }
                continue;
            }
            if (is_array($obj)) {
                $events[] = $obj;
            }
        }
        if ($events !== [] && ($events[0]['e'] ?? null) === 'H') {
            $v = $events[0]['trace_version'] ?? null;
            if ($v !== self::TRACE_VERSION) {
                throw new \InvalidArgumentException(
                    'this trace was written by an older tracer (version ' . var_export($v, true)
                    . ', need ' . self::TRACE_VERSION . ') — re-run the traced replay to regenerate it'
                );
            }
            array_shift($events);
        }
        return new self($events);
    }

    public function size(): int
    {
        return count($this->events);
    }

    public function isEmpty(): bool
    {
        return $this->events === [];
    }

    /**
     * Every observation of `$name`, in order.
     *
     * The tracer emits only changes, so every entry here is a transition. There is no second
     * filter to apply and no unchanged value to hide.
     *
     * @return list<Obs>
     */
    public function values(string $name): array
    {
        $out = [];
        foreach ($this->events as $e) {
            $kind = $e['e'] ?? null;
            $bag = $kind === 'L' ? ($e['d'] ?? null) : ($kind === 'C' ? ($e['args'] ?? null) : null);
            if (is_array($bag) && array_key_exists($name, $bag)) {
                $out[] = new Obs(
                    (string) ($e['at'] ?? ''),
                    (string) ($e['fn'] ?? ''),
                    $name,
                    Serial::fromTraceJsonable($bag[$name])
                );
            }
        }
        return $out;
    }

    public function first(string $name): ?Obs
    {
        $v = $this->values($name);
        return $v === [] ? null : $v[0];
    }

    public function last(string $name): ?Obs
    {
        $v = $this->values($name);
        return $v === [] ? null : $v[count($v) - 1];
    }

    /** @return list<string> every variable the trace observed, sorted */
    public function names(): array
    {
        $seen = [];
        foreach ($this->events as $e) {
            foreach ([($e['d'] ?? null), ($e['args'] ?? null)] as $bag) {
                if (is_array($bag)) {
                    foreach (array_keys($bag) as $k) {
                        $seen[(string) $k] = true;
                    }
                }
            }
        }
        $names = array_keys($seen);
        sort($names);
        return $names;
    }

    /**
     * `calls("studyStatus")` finds `Toy.studyStatus` without the caller having to know how it
     * was qualified.
     */
    private static function matchFn(string $want, string $got): bool
    {
        if ($want === '') {
            return true;
        }
        return $got === $want || str_ends_with($got, '.' . $want);
    }

    /** @return list<array{at: string, fn: string, args: array<string, mixed>}> */
    public function calls(string $fn = ''): array
    {
        $out = [];
        foreach ($this->events as $e) {
            if (($e['e'] ?? null) === 'C' && self::matchFn($fn, (string) ($e['fn'] ?? ''))) {
                $args = [];
                foreach ((array) ($e['args'] ?? []) as $k => $v) {
                    $args[$k] = Serial::fromTraceJsonable($v);
                }
                $out[] = ['at' => (string) ($e['at'] ?? ''), 'fn' => (string) ($e['fn'] ?? ''), 'args' => $args];
            }
        }
        return $out;
    }

    /** @return list<array{at: string, fn: string, value: mixed}> */
    public function returns(string $fn = ''): array
    {
        $out = [];
        foreach ($this->events as $e) {
            if (($e['e'] ?? null) === 'R' && self::matchFn($fn, (string) ($e['fn'] ?? ''))) {
                $out[] = [
                    'at' => (string) ($e['at'] ?? ''),
                    'fn' => (string) ($e['fn'] ?? ''),
                    'value' => Serial::fromTraceJsonable($e['v'] ?? null),
                ];
            }
        }
        return $out;
    }

    /** @return list<array{at: string, fn: string, type: string, detail: string}> */
    public function raised(): array
    {
        $out = [];
        foreach ($this->events as $e) {
            if (($e['e'] ?? null) === 'X') {
                $out[] = [
                    'at' => (string) ($e['at'] ?? ''),
                    'fn' => (string) ($e['fn'] ?? ''),
                    'type' => (string) ($e['type'] ?? ''),
                    'detail' => (string) ($e['v'] ?? ''),
                ];
            }
        }
        return $out;
    }

    /** One variable's timeline, for reading. */
    public function render(string $name): string
    {
        $obs = $this->values($name);
        if ($obs === []) {
            return "$name: never observed";
        }
        $out = '';
        foreach ($obs as $o) {
            $out .= sprintf("  %-28s %s = %s\n", $o->at, $name, Serial::render($o->value, 90));
        }
        return rtrim($out, "\n");
    }

    /** The whole execution, in order. */
    public function timeline(): string
    {
        $out = '';
        foreach ($this->events as $e) {
            $at = (string) ($e['at'] ?? '');
            $fn = (string) ($e['fn'] ?? '');
            $line = match ($e['e'] ?? null) {
                'C' => 'call ' . $fn . '(' . self::renderBag((array) ($e['args'] ?? [])) . ')',
                'L' => self::renderBag((array) ($e['d'] ?? [])),
                'R' => 'return ' . Serial::render(Serial::fromTraceJsonable($e['v'] ?? null), 60),
                'X' => 'THREW ' . (string) ($e['type'] ?? '') . ': ' . (string) ($e['v'] ?? ''),
                default => '',
            };
            $out .= sprintf("%-28s %s\n", $at, $line);
        }
        return rtrim($out, "\n");
    }

    private static function renderBag(array $bag): string
    {
        ksort($bag);
        $parts = [];
        foreach ($bag as $k => $v) {
            $parts[] = $k . '=' . Serial::render(Serial::fromTraceJsonable($v), 60);
        }
        return implode(', ', $parts);
    }

    /** The trace in its artifact form, header included. */
    public function toJsonl(): string
    {
        $out = Json::encode(['e' => 'H', 'trace_version' => self::TRACE_VERSION]) . "\n";
        foreach ($this->events as $e) {
            $out .= Json::encode($e) . "\n";
        }
        return $out;
    }

    public function __toString(): string
    {
        return $this->timeline();
    }
}
