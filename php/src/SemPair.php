<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** One item of the app's testimony: a span name and the phase it was in. */
final class SemPair implements \Stringable
{
    public function __construct(
        public readonly string $name,
        public readonly string $phase,
    ) {
    }

    public function equals(self $other): bool
    {
        return $this->name === $other->name && $this->phase === $other->phase;
    }

    public function __toString(): string
    {
        return '"' . $this->name . '" ' . $this->phase;
    }
}
