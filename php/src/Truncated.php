<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A traced sequence cut to a prefix. `count()` is the TRUE length; the contents are the first
 * Serial::TRACE_MAX_ITEMS elements.
 *
 * So `count($docs) > 0` is trustworthy while `$docs[500]` is not there to be read. An invariant
 * that asserts on the size of a collection keeps working on a truncated trace; one that reaches
 * past the head gets null rather than a quiet lie.
 *
 * @implements \IteratorAggregate<int, mixed>
 * @implements \ArrayAccess<int, mixed>
 */
final class Truncated implements \Countable, \IteratorAggregate, \ArrayAccess, \JsonSerializable
{
    /** @param list<mixed> $head */
    public function __construct(
        public readonly array $head,
        public readonly int $total,
    ) {
    }

    /** The TRUE length of the sequence, not the length of the traced prefix. */
    public function count(): int
    {
        return $this->total;
    }

    /** How many elements actually made it onto the tape. */
    public function traced(): int
    {
        return count($this->head);
    }

    public function getIterator(): \Traversable
    {
        return new \ArrayIterator($this->head);
    }

    public function offsetExists(mixed $offset): bool
    {
        return isset($this->head[$offset]);
    }

    public function offsetGet(mixed $offset): mixed
    {
        return $this->head[$offset] ?? null;
    }

    public function offsetSet(mixed $offset, mixed $value): void
    {
        throw new \LogicException('a traced value is a record of what happened; it is not writable');
    }

    public function offsetUnset(mixed $offset): void
    {
        throw new \LogicException('a traced value is a record of what happened; it is not writable');
    }

    public function jsonSerialize(): array
    {
        return ['__seq__' => ['len' => $this->total, 'head' => $this->head]];
    }

    public function __toString(): string
    {
        return sprintf('<%d items, first %d traced>', $this->total, count($this->head));
    }
}
