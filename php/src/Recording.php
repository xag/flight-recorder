<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A loaded tape: the analysis layer, plus editing.
 *
 * A tape is data, so this needs no runtime. A Recording reads any conformant tape, recorded by
 * any implementation, and recovers its structure — the calls, and each call's semantic-span
 * tree with the raw events each span enclosed, in order.
 */
final class Recording
{
    /** @var array<string, mixed> */
    public array $header;

    /** @var list<array<string, mixed>> */
    private array $calls;

    /** @var list<string> */
    private array $forbid = [];

    /**
     * @param array<string, mixed>            $header
     * @param list<array<string, mixed>>      $calls
     */
    private function __construct(array $header, array $calls)
    {
        $this->header = $header;
        $this->calls = $calls;
    }

    public static function load(string $path): self
    {
        $text = @file_get_contents($path);
        if ($text === false) {
            throw new \RuntimeException("cannot read recording: $path");
        }
        return self::parse($text);
    }

    /**
     * Parse a tape.
     *
     * A line that will not parse is discarded rather than raised. A truncated final line — the
     * process died mid-write — is the only corruption the format admits, and every line is
     * complete when written, so the rest of the tape is still evidence.
     *
     * A reader MUST ignore an `ev` it does not know. That is the whole forward-compatibility
     * story, and it is why new event kinds need no version bump.
     */
    public static function parse(string $text): self
    {
        $header = null;
        $calls = [];
        foreach (explode("\n", $text) as $ln) {
            if (trim($ln) === '') {
                continue;
            }
            try {
                $obj = Json::decode($ln);
            } catch (\JsonException) {
                continue;
            }
            if (!is_array($obj)) {
                continue;
            }
            $ev = $obj['ev'] ?? null;
            if ($ev === 'session') {
                $header ??= $obj;
            } elseif ($ev === 'call') {
                $calls[] = $obj;
            }
        }
        if ($header === null) {
            throw new \InvalidArgumentException('no session header — not a flight recording?');
        }
        $version = $header['version'] ?? null;
        if (!is_int($version) || $version !== Recorder::FORMAT_VERSION) {
            throw new \InvalidArgumentException(
                'this tape is format version ' . var_export($version, true)
                . ', and this reader implements version ' . Recorder::FORMAT_VERSION
            );
        }
        return new self($header, $calls);
    }

    public function numCalls(): int
    {
        return count($this->calls);
    }

    /** The call at `$i`, or the first call named `$i`. Null when there is none. */
    public function call(int|string $which): ?CallView
    {
        if (is_int($which)) {
            return isset($this->calls[$which]) ? new CallView($this->calls[$which], $which, $this) : null;
        }
        foreach ($this->calls as $i => $c) {
            if (($c['fn'] ?? null) === $which) {
                return new CallView($c, $i, $this);
            }
        }
        return null;
    }

    /** @return list<CallView> */
    public function calls(): array
    {
        $out = [];
        foreach ($this->calls as $i => $c) {
            $out[] = new CallView($c, $i, $this);
        }
        return $out;
    }

    /** @internal used by CallView to write an edit back into the tape */
    public function replaceCall(int $index, array $raw): void
    {
        $this->calls[$index] = $raw;
    }

    /**
     * Arm the tripwire for a save.
     *
     * The write path was guarded and the RE-write path was not, which is the wrong way round:
     * mutation exists precisely to EDIT recorded values, so a tape that passed the tripwire when
     * it was recorded can have a credential put into it by hand and then be saved with nothing
     * looking.
     *
     * A tape does not carry its own forbid patterns. The rules are the boundary's, not the
     * artifact's, and they are deliberately not written onto the tape for a later reader to find
     * and "helpfully" relax.
     */
    public function forbidding(string ...$patterns): self
    {
        foreach ($patterns as $p) {
            $this->forbid[] = $p;
        }
        return $this;
    }

    /** Arm the tripwire from a boundary's declaration. */
    public function forbiddingFrom(?Boundary $b): self
    {
        return $b === null ? $this : $this->forbidding(...$b->forbid);
    }

    /**
     * Write the tape out.
     *
     * The entire tape is built in memory and **every line vetted before any of it touches
     * disk**, so a refusal leaves no half-written file behind and never truncates a good tape to
     * punish a bad edit.
     */
    public function save(string $path): string
    {
        $lines = [Json::encode($this->header)];
        foreach ($this->calls as $c) {
            $lines[] = Json::encode($c);
        }
        foreach ($lines as $ln) {
            $hit = Serial::forbiddenHit($ln, $this->forbid);
            if ($hit !== null) {
                throw new ForbiddenValue($hit, 'the re-saved tape');
            }
        }
        $dir = dirname($path);
        if (!is_dir($dir) && !@mkdir($dir, 0o777, true) && !is_dir($dir)) {
            throw new \RuntimeException("cannot create directory for tape: $dir");
        }
        file_put_contents($path, implode("\n", $lines) . "\n");
        return $path;
    }

    /** The tape as text, exactly as `save()` would write it. */
    public function text(): string
    {
        $lines = [Json::encode($this->header)];
        foreach ($this->calls as $c) {
            $lines[] = Json::encode($c);
        }
        return implode("\n", $lines) . "\n";
    }

    /** The runtime that produced this tape, as named in the header. */
    public function runtime(): ?string
    {
        foreach (['python', 'node', 'dotnet', 'go', 'java', 'php'] as $k) {
            if (array_key_exists($k, $this->header)) {
                return $k;
            }
        }
        return null;
    }
}
