<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** A replay, and how every claim about it fared. */
final class InvariantReport
{
    /** @param list<InvariantResult> $results */
    public function __construct(
        public readonly ?ReplayReport $replay,
        public readonly array $results,
    ) {
    }

    /**
     * The bar differs by mode, deliberately.
     *
     * A strict replay must also MATCH the recording. A probe replay is a deliberately mutated
     * world where a different result is the entire point — so there the claims are the whole
     * verdict, and the replay only has to have been answerable.
     */
    public function ok(): bool
    {
        $claims = true;
        foreach ($this->results as $r) {
            $claims = $claims && $r->ok;
        }
        if ($this->replay === null) {
            return $claims;
        }
        if ($this->replay->probe) {
            return $claims
                && $this->replay->divergence === null
                && $this->replay->unanswerable === null;
        }
        return $claims && $this->replay->ok();
    }

    /** @return list<InvariantResult> */
    public function violations(): array
    {
        return array_values(array_filter($this->results, static fn (InvariantResult $r): bool => !$r->ok));
    }

    public function __toString(): string
    {
        return Invariants::format($this);
    }
}
