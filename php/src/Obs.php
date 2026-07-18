<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** One observation of one variable: where, in what function, and what it became. */
final class Obs implements \Stringable
{
    public function __construct(
        public readonly string $at,
        public readonly string $fn,
        public readonly string $name,
        public readonly mixed $value,
    ) {
    }

    public function __toString(): string
    {
        return $this->name . '=' . Serial::render($this->value, 90)
            . ' at ' . $this->at . ' in ' . $this->fn;
    }
}
