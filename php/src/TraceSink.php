<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Where traced events go, and the tripwire that guards them.
 *
 * The trace is the **worst** artifact to leave unguarded, not the least: it records every local
 * on every executed line — values *before* the code under trace masked any of them — and tracing is exactly
 * what you switch on when debugging the request that went wrong, which is the one carrying the
 * real credential.
 *
 * So `vet()` runs before the in-memory buffer, not merely before the file. An invariant reads
 * these events while the run is still going, and a pathless sink is not private: "in memory" is
 * a statement about latency, not about confinement.
 *
 * The boundary's two masking layers run first, as they do on the tape: its field rules and its
 * value scrub rewrite each event's payload (`args` of a call, `d` of a line, `v` of a return or
 * a raise), never its envelope (`e`, `fn`, `at`, `type`), and the tripwire then reads the masked
 * line. A trace's payload is keyed by local name as a tape's kwargs are keyed by parameter name,
 * so a rule reaches a local exactly as it reaches an argument.
 */
final class TraceSink
{
    /** The keys of a trace event that hold traced values; the rest is the envelope. */
    public const PAYLOAD_KEYS = ['args', 'd', 'v'];

    /** @var list<array<string, mixed>> */
    private array $events = [];

    private ?string $refused = null;

    /** @var list<string> */
    private array $forbid;

    /** @var array<string, (callable(mixed): mixed)|null> */
    private array $redact;

    /** @var (callable(string): string)|null */
    private $scrub;

    public function __construct(
        private readonly ?string $path = null,
        ?Boundary $boundary = null,
    ) {
        $this->forbid = $boundary?->forbid ?? [];
        $this->redact = $boundary?->redact ?? [];
        $this->scrub = $boundary?->scrub;
        if ($this->path !== null) {
            file_put_contents(
                $this->path,
                Json::encode(['e' => 'H', 'trace_version' => Trace::TRACE_VERSION]) . "\n"
            );
        }
    }

    /** The pattern that shut tracing down, or null if it is still running. */
    public function refused(): ?string
    {
        return $this->refused;
    }

    public function count(): int
    {
        return count($this->events);
    }

    public function snapshot(int $from = 0): Trace
    {
        $from = max(0, min($from, count($this->events)));
        return new Trace(array_values(array_slice($this->events, $from)));
    }

    /** @param array<string, mixed> $ev */
    public function emit(array $ev): void
    {
        if ($this->refused !== null) {
            return; // tracing stays off after a hit
        }
        if ($this->redact !== [] || $this->scrub !== null) {
            foreach (self::PAYLOAD_KEYS as $key) {
                if (array_key_exists($key, $ev)) {
                    // Never throws: a rule or scrub that does degrades to REDACTED.
                    $ev[$key] = Serial::redactJsonable($ev[$key], $this->redact, $this->scrub);
                }
            }
        }
        try {
            $line = Json::encode($ev);
        } catch (\Throwable) {
            // Something that cannot be inspected cannot be cleared. Refuse rather than wave it
            // through unread.
            return;
        }
        $hit = Serial::forbiddenHit($line, $this->forbid);
        if ($hit !== null) {
            $this->refuse($hit);
            return;
        }
        $this->events[] = $ev;
        if ($this->path !== null) {
            file_put_contents($this->path, $line . "\n", FILE_APPEND);
        }
    }

    /**
     * A forbidden value reached the trace: destroy what exists and stop.
     *
     * The refusal goes to a **sidecar file** because the guard may be running in a child process
     * whose exit code nobody checks — a traced test run can trip this and still exit 0, so a
     * guard that only shouted into the child's stderr would be a guard nobody enforces. The
     * parent reads the sidecar before the trace, and it wins.
     */
    private function refuse(string $pattern): void
    {
        $this->refused = $pattern;
        $this->events = [];
        if ($this->path !== null) {
            @unlink($this->path);
            @file_put_contents(TraceHook::refusalPath($this->path), $pattern);
        }
        fwrite(
            STDERR,
            "flight-recorder: tracing stopped — a traced value matched a forbidden pattern "
            . "($pattern). The trace was destroyed and nothing further will be recorded.\n"
        );
    }

    public function close(): void
    {
        // Every line is flushed as it is written, so there is nothing buffered to lose. This
        // exists so callers can say they are done without knowing that.
    }
}
