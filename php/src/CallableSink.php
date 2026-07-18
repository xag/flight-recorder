<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A Sink made from a plain callable, so `publishingTo(fn ($name, $text) => ...)` works without
 * anyone having to declare a class for a one-line publisher.
 */
final class CallableSink implements Sink
{
    /** @var callable(string, string): void */
    private $fn;

    /** @param callable(string, string): void $fn */
    public function __construct(callable $fn)
    {
        $this->fn = $fn;
    }

    public function publish(string $name, string $text): void
    {
        ($this->fn)($name, $text);
    }
}
