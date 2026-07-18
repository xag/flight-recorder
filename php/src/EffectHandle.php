<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * One recorded effect, editable.
 *
 * The format's "exactly one of `res` / `err`" rule is maintained by construction: setting either
 * removes the other, so no edit can produce an event no checker would accept.
 */
final class EffectHandle
{
    /** @param array<string, mixed> $ev */
    public function __construct(
        private readonly MutateHandle $owner,
        private readonly int $index,
        private array $ev,
    ) {
    }

    public function result(): mixed
    {
        return Serial::fromJsonable($this->ev['res'] ?? null);
    }

    public function setResult(mixed $value): self
    {
        unset($this->ev['err']);
        $this->ev['res'] = Serial::toJsonable($value);
        $this->owner->setEvent($this->index, $this->ev);
        return $this;
    }

    /** Make the effect fail. The reviver named by `$type` rebuilds it on replay. */
    public function setError(string $type, mixed ...$args): self
    {
        unset($this->ev['res']);
        $this->ev['err'] = [
            'type' => $type,
            'repr' => (string) ($args[0] ?? $type),
            'args' => array_values(array_map(
                static fn (mixed $a): mixed => Serial::toJsonable($a),
                $args
            )),
        ];
        $this->owner->setEvent($this->index, $this->ev);
        return $this;
    }

    public function setThrowable(\Throwable $t): self
    {
        return $this->setError(Recorder::shortName($t), Recorder::render($t));
    }
}
