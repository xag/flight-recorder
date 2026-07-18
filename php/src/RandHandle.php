<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** One recorded random draw, editable. */
final class RandHandle
{
    /** @param array<string, mixed> $ev */
    public function __construct(
        private readonly MutateHandle $owner,
        private readonly int $index,
        private array $ev,
    ) {
    }

    /** @return list<int> */
    public function indices(): array
    {
        return array_map('intval', array_values((array) ($this->ev['idx'] ?? [])));
    }

    /**
     * Choose the draw.
     *
     * `m` and `kk` are rewritten alongside `idx` so the edited event stays conformant — an
     * edit that left `kk` naming a count the array no longer has would produce a tape the
     * checkers reject, and a mutation session should not have to know that.
     */
    public function setIndices(int ...$idx): self
    {
        foreach ($idx as $i) {
            if ($i < 0) {
                throw new \InvalidArgumentException("a drawn position cannot be negative: $i");
            }
        }
        $this->ev['m'] = 'sample';
        $this->ev['idx'] = array_values($idx);
        $this->ev['kk'] = count($idx);
        if (!isset($this->ev['n']) || !is_int($this->ev['n'])) {
            $this->ev['n'] = $idx === [] ? 0 : max($idx) + 1;
        }
        $this->owner->setEvent($this->index, $this->ev);
        return $this;
    }

    /** Set the value of a `float` or `int` draw. */
    public function setValue(int|float $v): self
    {
        $this->ev['v'] = $v;
        $this->owner->setEvent($this->index, $this->ev);
        return $this;
    }
}
