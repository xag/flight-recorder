<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The events raised by one in-flight call, in the order the world answered them.
 *
 * Internal. It exists so a primitive can append an event without knowing anything about files,
 * and so an invariant can read the events while the run is still going.
 */
final class CallBuffer
{
    /** @var list<array<string, mixed>> */
    public array $events = [];

    private int $sid = 0;

    /** @param array<string, mixed> $event */
    public function add(array $event): void
    {
        $this->events[] = $event;
    }

    /**
     * The next span id, unique within this call.
     *
     * Call-scoped and 1-based, because an `end` names its `begin` by sid and a reader walking
     * one call must never have to look outside it to resolve a pair.
     */
    public function nextSid(): int
    {
        return ++$this->sid;
    }
}
