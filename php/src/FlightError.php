<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * An error that can state the values it was built from.
 *
 * Implement it on an app's own exception types so a recorded failure can be rebuilt faithfully
 * on replay. Without it, `err.args` holds only the error's rendering, and a reviver has to
 * guess a structured error back out of a sentence.
 */
interface FlightError
{
    /** @return list<mixed> the constructive arguments, in constructor order */
    public function errorArgs(): array;
}
