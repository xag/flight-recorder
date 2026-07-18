<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** How one claim fared. */
final class InvariantResult
{
    public function __construct(
        public readonly string $name,
        public readonly bool $ok,
        public readonly ?string $error = null,
    ) {
    }
}
