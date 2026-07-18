<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use Xag\FlightRecorder\FlightError;

/**
 * The toy's own error type, carrying the values it was built from.
 *
 * Implementing FlightError is what lets a reviver rebuild it faithfully on replay rather than
 * guessing a structured error back out of a sentence — and the code's own `catch (ToyError)`
 * then fires exactly as it did when recorded.
 */
final class ToyError extends \RuntimeException implements FlightError
{
    public function __construct(string $message, public readonly int $code_ = 0)
    {
        parent::__construct($message);
    }

    /** The reviver form: rebuild from the recorded `err.args`. */
    public static function fromArgs(array $args): self
    {
        return new self((string) ($args[0] ?? ''), (int) ($args[1] ?? 0));
    }

    public function errorArgs(): array
    {
        return [$this->getMessage(), $this->code_];
    }
}
