<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** One named claim about an execution. */
final class Invariant
{
    /** @var callable(Trajectory): void */
    private $check;

    /** @param callable(Trajectory): void $check throws or returns; a throw is a violation */
    public function __construct(public readonly string $name, callable $check)
    {
        $this->check = $check;
    }

    public static function of(string $name, callable $check): self
    {
        return new self($name, $check);
    }

    public function assertOn(Trajectory $t): void
    {
        ($this->check)($t);
    }
}
