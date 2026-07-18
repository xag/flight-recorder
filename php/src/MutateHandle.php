<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** The editable face of one recorded call. See Mutate. */
final class MutateHandle
{
    /** @var array<string, mixed> */
    private array $raw;

    public function __construct(private readonly CallView $cv)
    {
        $this->raw = $cv->raw();
    }

    public function view(): CallView
    {
        return $this->cv;
    }

    /** @return list<array<string, mixed>> */
    private function events(): array
    {
        $e = $this->raw['events'] ?? [];
        return is_array($e) ? array_values($e) : [];
    }

    /**
     * Write one edited event back, and mark the call a probe.
     *
     * @internal
     * @param array<string, mixed> $ev
     */
    public function setEvent(int $index, array $ev): void
    {
        $events = $this->events();
        $events[$index] = $ev;
        $this->raw['events'] = $events;
        $this->raw['probe'] = true;
        $this->cv->setRaw($this->raw);
    }

    /** The index of the `$n`th event matching a predicate, or null. */
    private function find(callable $pred, int $n): ?int
    {
        $seen = 0;
        foreach ($this->events() as $i => $e) {
            if (is_array($e) && $pred($e)) {
                if ($seen === $n) {
                    return $i;
                }
                $seen++;
            }
        }
        return null;
    }

    public function effect(string $name, int $occurrence = 0): EffectHandle
    {
        $i = $this->find(
            static fn (array $e): bool => ($e['k'] ?? null) === 'fx' && ($e['fn'] ?? null) === $name,
            $occurrence
        );
        if ($i === null) {
            throw new \InvalidArgumentException("no fx event \"$name\" #$occurrence in this call");
        }
        return new EffectHandle($this, $i, $this->events()[$i]);
    }

    /**
     * A chained READ — never a write.
     *
     * A write carries `args`, not `res`, and emptying one would produce an event the format
     * forbids rather than a world worth replaying against.
     */
    public function read(?string $op = null, int $occurrence = 0): ReadHandle
    {
        $i = $this->find(
            static fn (array $e): bool => ($e['k'] ?? null) === 'db'
                && array_key_exists('res', $e)
                && ($op === null || ($e['op'] ?? null) === $op),
            $occurrence
        );
        if ($i === null) {
            throw new \InvalidArgumentException(
                'no db read ' . ($op === null ? '' : "\"$op\" ") . "#$occurrence in this call"
            );
        }
        return new ReadHandle($this, $i, $this->events()[$i]);
    }

    public function rand(int $occurrence = 0): RandHandle
    {
        $i = $this->find(static fn (array $e): bool => ($e['k'] ?? null) === 'rand', $occurrence);
        if ($i === null) {
            throw new \InvalidArgumentException("no rand event #$occurrence in this call");
        }
        return new RandHandle($this, $i, $this->events()[$i]);
    }

    public function clock(): ClockHandle
    {
        return new ClockHandle($this);
    }

    /** @return list<int> the indices of every `now` event, in order */
    public function clockIndices(): array
    {
        $out = [];
        foreach ($this->events() as $i => $e) {
            if (is_array($e) && ($e['k'] ?? null) === 'now') {
                $out[] = $i;
            }
        }
        return $out;
    }

    /** @return list<array<string, mixed>> */
    public function rawEvents(): array
    {
        return $this->events();
    }

    public function setKwarg(string $key, mixed $value): self
    {
        $kwargs = $this->raw['kwargs'] ?? [];
        $kwargs = is_array($kwargs) ? $kwargs : [];
        $kwargs[$key] = Serial::toJsonable($value);
        $this->raw['kwargs'] = Recorder::mapOf($kwargs);
        $this->raw['probe'] = true;
        $this->cv->setRaw($this->raw);
        return $this;
    }

    public function markProbe(): self
    {
        $this->raw['probe'] = true;
        $this->cv->setRaw($this->raw);
        return $this;
    }

    /**
     * Replay the edited call and judge it by the claims.
     *
     * @param list<Invariant> $invariants
     */
    public function check(callable $resolve, array $invariants, ?Boundary $boundary = null): InvariantReport
    {
        return Invariants::checkCall($this->cv, $resolve, $invariants, $boundary, true);
    }

    /** Replay the edited call without claims. */
    public function replay(callable $resolve, ?Boundary $boundary = null): ReplayReport
    {
        return Replay::replayCall($this->cv, $resolve, $boundary, true);
    }
}
