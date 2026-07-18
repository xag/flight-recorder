<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The stand-in for a recorded error the boundary declared no reviver for.
 *
 * The original type is named in the message rather than hidden, because a replay that silently
 * substituted a generic error would send the code down a different `catch` than the one it
 * took — and would then report the resulting difference as a change in the code.
 */
final class ReplayedEffectError extends \RuntimeException
{
    /** @param list<mixed> $args */
    public function __construct(
        public readonly string $type,
        public readonly string $repr,
        public readonly array $args = [],
    ) {
        parent::__construct("$type: $repr");
    }
}
