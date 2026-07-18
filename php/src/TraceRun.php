<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** What a traced run produced: the value, and the execution that produced it. */
final class TraceRun
{
    public function __construct(
        public readonly mixed $result,
        public readonly Trace $trace,
    ) {
    }

    public function trace(): Trace
    {
        return $this->trace;
    }
}
