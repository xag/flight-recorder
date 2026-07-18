<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * One recorded call, read off a tape.
 *
 * The event maps handed out here are the **live** maps from the parsed tape, which is what lets
 * Mutate edit a call in place and then save it.
 */
final class CallView
{
    /** @param array<string, mixed> $raw */
    public function __construct(
        private array $raw,
        private readonly int $index,
        private readonly Recording $owner,
    ) {
    }

    public function fn(): string
    {
        return (string) ($this->raw['fn'] ?? '');
    }

    public function index(): int
    {
        return $this->index;
    }

    /** @return array<string, mixed> the call's inputs, revived */
    public function kwargs(): array
    {
        $k = Serial::fromJsonable($this->raw['kwargs'] ?? []);
        return is_array($k) ? $k : [];
    }

    /** The call's return value, revived. */
    public function result(): mixed
    {
        return Serial::fromJsonable($this->raw['result'] ?? null);
    }

    public function error(): ?string
    {
        $e = $this->raw['error'] ?? null;
        return is_string($e) ? $e : null;
    }

    /** @return list<array<string, mixed>> every answer the world gave, in order */
    public function events(): array
    {
        $e = $this->raw['events'] ?? [];
        return is_array($e) ? array_values($e) : [];
    }

    /** The `$n`th event of a kind, or null. */
    public function event(string $kind, int $n = 0): ?array
    {
        $seen = 0;
        foreach ($this->events() as $e) {
            if (is_array($e) && ($e['k'] ?? null) === $kind) {
                if ($seen === $n) {
                    return $e;
                }
                $seen++;
            }
        }
        return null;
    }

    /**
     * Mark this call a probe: it has been edited, so replay stops comparing arguments.
     *
     * Persisted to the tape, so a saved mutated call can never later be mistaken for a strict
     * regression pin.
     */
    public function markProbe(): self
    {
        $this->raw['probe'] = true;
        $this->owner->replaceCall($this->index, $this->raw);
        return $this;
    }

    public function isProbe(): bool
    {
        return ($this->raw['probe'] ?? false) === true;
    }

    /** @return array<string, mixed> */
    public function raw(): array
    {
        return $this->raw;
    }

    /** @param array<string, mixed> $raw */
    public function setRaw(array $raw): void
    {
        $this->raw = $raw;
        $this->owner->replaceCall($this->index, $raw);
    }

    /**
     * The call's semantic skeleton, recovered from ORDER alone.
     *
     * No event carries a parent pointer: a span contains every event between its `begin` and its
     * `end`. That works because a span is well-nested by construction — it wraps the body it
     * encloses — which is exactly why a recorder that cannot guarantee nesting must not emit
     * `sem` at all.
     */
    public function spans(): SpanNode
    {
        $root = new SpanNode($this->fn(), 'call', $this->error() !== null ? 'error' : 'ok');
        $stack = [$root];

        foreach ($this->events() as $e) {
            if (!is_array($e)) {
                continue;
            }
            $top = $stack[count($stack) - 1];
            if (($e['k'] ?? null) !== 'sem') {
                $top->events[] = $e;
                continue;
            }
            $phase = $e['phase'] ?? null;
            $name = (string) ($e['name'] ?? '');
            $data = isset($e['data']) && is_array($e['data']) ? $e['data'] : null;

            if ($phase === 'begin') {
                $node = new SpanNode($name, 'span', '', $data);
                $top->children[] = $node;
                $stack[] = $node;
            } elseif ($phase === 'end') {
                if (count($stack) > 1) {
                    $top->outcome = (string) ($e['outcome'] ?? '');
                    array_pop($stack);
                }
            } elseif ($phase === 'point') {
                $top->children[] = new SpanNode($name, 'point', '', $data);
            }
        }
        return $root;
    }

    /**
     * The semantic skeleton, rendered top-down — what to read before descending into JSONL.
     *
     * The shape is fixed across every runtime: a tape written by any implementation renders
     * character-for-character identically here, and a test in each port asserts exactly that.
     */
    public function renderSpans(): string
    {
        $out = '';
        self::renderNode($this->spans(), 0, $out);
        return rtrim($out, "\n");
    }

    private static function renderNode(SpanNode $n, int $depth, string &$out): void
    {
        $indent = str_repeat('  ', $depth);
        if ($n->phase === 'point') {
            $out .= $indent . '- ' . $n->name . self::renderData($n->data) . "\n";
            return;
        }
        $out .= $indent . $n->name . '  ' . ($n->outcome === 'error' ? 'ERROR' : 'ok')
            . self::renderCount($n->events) . "\n";
        foreach ($n->children as $c) {
            self::renderNode($c, $depth + 1, $out);
        }
    }

    /** @param list<array<string, mixed>> $events */
    private static function renderCount(array $events): string
    {
        $n = count($events);
        if ($n === 0) {
            return '';
        }
        $kinds = [];
        foreach ($events as $e) {
            $kinds[(string) ($e['k'] ?? '')] = true;
        }
        if (count($kinds) === 1) {
            return '  (' . $n . ' ' . array_key_first($kinds) . ')';
        }
        return '  (' . $n . ' events)';
    }

    /** @param array<string, mixed>|null $data */
    private static function renderData(?array $data): string
    {
        if ($data === null || $data === []) {
            return '';
        }
        ksort($data);
        $parts = [];
        foreach ($data as $k => $v) {
            $parts[] = $k . '=' . (is_string($v) ? '"' . $v . '"' : Json::encode($v));
        }
        return '  ' . implode(' ', $parts);
    }
}
