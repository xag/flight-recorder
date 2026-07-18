<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A traced string cut to a prefix. `count()` is the TRUE length; the value is the head.
 *
 * PHP cannot subclass `string`, so unlike Python's `TruncatedText` this is not a string that
 * lies about its length — it is an object that stringifies to the head and reports the true
 * length through `length()`. An invariant asserting on a prefix still works; one asserting on
 * the full text can see that it is looking at a prefix, which is better than being unable to.
 */
final class TruncatedText implements \Stringable, \Countable, \JsonSerializable
{
    public function __construct(
        public readonly string $head,
        public readonly int $total,
    ) {
    }

    /** The TRUE length of the string, not the length of the traced prefix. */
    public function length(): int
    {
        return $this->total;
    }

    public function count(): int
    {
        return $this->total;
    }

    public function __toString(): string
    {
        return $this->head;
    }

    public function jsonSerialize(): array
    {
        return ['__str__' => ['len' => $this->total, 'head' => $this->head]];
    }
}
