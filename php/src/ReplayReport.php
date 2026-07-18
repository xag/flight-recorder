<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * What a replay found.
 *
 * Three signals, kept independent because they answer different questions:
 *
 * - a boundary **divergence** says the recording is stale;
 * - a result/error **mismatch** says the code produces something else;
 * - a **semantic divergence** says the code's own account of what it was doing changed — which
 *   may be a refactor as easily as a bug, so it is reported and does not gate.
 */
final class ReplayReport
{
    public string $fn = '';
    public bool $resultMatch = false;
    public bool $errorMatch = false;
    public ?string $divergence = null;
    public int $eventsConsumed = 0;
    public int $eventsTotal = 0;
    public int $skipped = 0;

    /** @var list<string> */
    public array $writeDivs = [];

    /** @var list<SemPair> */
    public array $semsRecorded = [];

    /** @var list<SemPair> */
    public array $semsReplayed = [];

    public ?string $semDivergence = null;
    public mixed $replayedResult = null;
    public ?string $replayedError = null;

    /** @var list<array<string, mixed>> what the code WOULD have written */
    public array $writes = [];

    /** @var array<string, mixed> */
    public array $kwargs = [];

    public bool $probe = false;
    public ?string $unanswerable = null;
    public Trace $trace;

    public function __construct()
    {
        $this->trace = Trace::empty();
    }

    /**
     * A probe replay is **not gated by match** — a mutated recording is judged by invariants —
     * so its `ok` asks only that the tape could answer the path the mutation produced.
     */
    public function ok(): bool
    {
        if ($this->divergence !== null || $this->unanswerable !== null) {
            return false;
        }
        if ($this->probe) {
            return true;
        }
        return $this->resultMatch
            && $this->errorMatch
            && $this->writeDivs === []
            && $this->eventsConsumed === $this->eventsTotal;
    }

    public function __toString(): string
    {
        return Replay::format(0, $this);
    }
}
