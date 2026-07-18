<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** The call's clock reads, editable as a sequence. */
final class ClockHandle
{
    public function __construct(private readonly MutateHandle $owner)
    {
    }

    /** @return list<string> the recorded times, in the order the code read them */
    public function times(): array
    {
        $out = [];
        $events = $this->owner->rawEvents();
        foreach ($this->owner->clockIndices() as $i) {
            $out[] = (string) ($events[$i]['v'] ?? '');
        }
        return $out;
    }

    /** Replace as many clock reads as values given. */
    public function setTimes(string ...$isoTimes): self
    {
        $events = $this->owner->rawEvents();
        foreach ($this->owner->clockIndices() as $n => $i) {
            if (!array_key_exists($n, $isoTimes)) {
                break;
            }
            $ev = $events[$i];
            $ev['v'] = $isoTimes[$n];
            $this->owner->setEvent($i, $ev);
            $events = $this->owner->rawEvents();
        }
        return $this;
    }

    /**
     * Run time backwards.
     *
     * The classic "what does this do when the clock goes the wrong way?" probe, which no amount
     * of waiting will provoke in production.
     */
    public function reverse(): self
    {
        return $this->setTimes(...array_reverse($this->times()));
    }
}
