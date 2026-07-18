<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The execution an invariant judges.
 *
 * Always the **replayed** execution, never the recorded one. The recorded result is the thing
 * being questioned; asserting over it would only confirm the tape agrees with itself.
 */
final class Trajectory
{
    /**
     * @param list<array<string, mixed>> $writes what the code WOULD have written
     * @param list<SemPair>              $sems
     * @param array<string, mixed>       $kwargs
     * @param list<array<string, mixed>> $events
     */
    public function __construct(
        public readonly mixed $result,
        public readonly ?string $error,
        public readonly array $writes,
        public readonly array $sems,
        public readonly array $kwargs,
        public readonly array $events,
        public readonly Trace $trace,
    ) {
    }

    /** The result as an array, or an empty one — so a claim never dies on a type. */
    public function resultArray(): array
    {
        return is_array($this->result) ? $this->result : [];
    }
}
