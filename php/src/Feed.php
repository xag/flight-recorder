<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The recording, serving as the world.
 *
 * Events are popped in order and the replayed code must ask the same questions in the same
 * order; a different question at position *n* is precisely where behaviour changed. Writes are
 * compared, never executed.
 */
final class Feed
{
    private int $pos = 0;
    public int $consumed = 0;
    public int $skipped = 0;

    /** @var list<string> */
    public array $writeDivs = [];

    /** @var list<array<string, mixed>> */
    public array $writes = [];

    /** @var list<SemPair> */
    public array $sems = [];

    /** @param list<array<string, mixed>> $events */
    public function __construct(
        private readonly array $events,
        public readonly bool $probe,
        private readonly ?Boundary $boundary,
    ) {
    }

    public function total(): int
    {
        return count($this->events);
    }

    /**
     * Advance past semantic events.
     *
     * They are the app's testimony, never evidence, and are never fed back to anything — but
     * they are still *consumed*, or a replay would report a shorter path than the one recorded.
     */
    public function skipSems(): void
    {
        while ($this->pos < count($this->events)
            && ($this->events[$this->pos]['k'] ?? null) === 'sem') {
            $this->pos++;
            $this->consumed++;
        }
    }

    /**
     * Under mutation a query's CONTENT changes — it flows from mutated data — but its SHAPE does
     * not. So probe matching compares shapes: `collection("u").where("x", ">", 0)` becomes
     * `collection.where`.
     */
    private static function skeleton(string $sig): string
    {
        return (string) preg_replace('/\([^()]*\)/', '', $sig);
    }

    private function matches(array $ev, string $kind, ?string $sig, ?string $op, ?string $fn): bool
    {
        if (($ev['k'] ?? null) !== $kind) {
            return false;
        }
        if ($kind === 'db' && $sig !== null) {
            if ($op !== null && ($ev['op'] ?? null) !== $op) {
                return false;
            }
            $evSig = (string) ($ev['sig'] ?? '');
            return $this->probe
                ? self::skeleton($evSig) === self::skeleton($sig)
                : $evSig === $sig;
        }
        if ($kind === 'fx' && $fn !== null) {
            return ($ev['fn'] ?? null) === $fn;
        }
        return true;
    }

    private static function want(string $kind, ?string $sig, ?string $op, ?string $fn): string
    {
        if ($sig !== null) {
            return $kind . ' ' . ($op ?? '') . ' ' . $sig;
        }
        if ($fn !== null) {
            return $kind . ' ' . $fn;
        }
        return $kind;
    }

    /**
     * Take the next event, asserting the code asked the question the tape answers.
     *
     * @return array<string, mixed>
     */
    public function popExpect(string $kind, ?string $sig = null, ?string $op = null, ?string $fn = null): array
    {
        $this->skipSems();

        if ($this->probe) {
            // Scan forward: a mutation legitimately changes which questions get asked.
            for ($j = $this->pos; $j < count($this->events); $j++) {
                $ev = $this->events[$j];
                if (($ev['k'] ?? null) === 'sem') {
                    continue;
                }
                if ($this->matches($ev, $kind, $sig, $op, $fn)) {
                    for ($x = $this->pos; $x < $j; $x++) {
                        if (($this->events[$x]['k'] ?? null) !== 'sem') {
                            $this->skipped++;
                        }
                    }
                    $this->consumed += ($j - $this->pos) + 1;
                    $this->pos = $j + 1;
                    return $ev;
                }
            }
            throw new ProbeUnanswerable(
                'the replayed code asked for "' . self::want($kind, $sig, $op, $fn)
                . '" but the recording holds no further such event — the mutation sent execution '
                . 'down a path this recording cannot answer'
            );
        }

        if ($this->pos >= count($this->events)) {
            throw new ReplayDivergence(
                'replay asked for a "' . $kind . '" event at position ' . $this->pos
                . ' but the recording is exhausted — the replayed code takes a longer path than '
                . 'the recorded one'
            );
        }
        $ev = $this->events[$this->pos];
        if (!$this->matches($ev, $kind, $sig, $op, $fn)) {
            throw new ReplayDivergence(
                'boundary divergence at event ' . $this->pos . ":\n  recorded: "
                . self::brief($ev) . "\n  replayed: " . self::want($kind, $sig, $op, $fn)
            );
        }
        $this->pos++;
        $this->consumed++;
        return $ev;
    }

    // --- serving answers ----------------------------------------------------------------

    public function now(): \DateTimeImmutable
    {
        $ev = $this->popExpect('now');
        $v = (string) ($ev['v'] ?? '');
        try {
            return new \DateTimeImmutable($v);
        } catch (\Throwable) {
            return new \DateTimeImmutable();
        }
    }

    public function perf(): float
    {
        $ev = $this->popExpect('perf');
        return (float) ($ev['v'] ?? 0.0);
    }

    private function expectRand(string $method): array
    {
        $ev = $this->popExpect('rand');
        $m = $ev['m'] ?? null;
        if ($m !== $method) {
            throw new ReplayDivergence(
                'random divergence: replayed code drew "' . $method
                . '" but the recording holds a "' . (string) $m . '" draw here'
            );
        }
        return $ev;
    }

    /** @return list<int> */
    public function sample(int $n, int $k): array
    {
        $ev = $this->expectRand('sample');
        $idx = array_map('intval', array_values((array) ($ev['idx'] ?? [])));
        foreach ($idx as $i) {
            if ($i >= $n) {
                // A mutation may have shrunk the population under a recorded index. That is the
                // tape being incompletely edited, not the code misbehaving.
                throw new ProbeUnanswerable(
                    "the recording drew position $i from a population of $n — the population "
                    . 'shrank under a recorded draw, so this tape cannot answer the mutated path'
                );
            }
        }
        return $idx;
    }

    public function bytes(int $n): string
    {
        $ev = $this->expectRand('bytes');
        $hex = (string) ($ev['hex'] ?? '');
        $b = $hex === '' ? '' : (hex2bin($hex) ?: '');
        return $b;
    }

    public function randFloat(): float
    {
        $ev = $this->expectRand('float');
        return (float) ($ev['v'] ?? 0.0);
    }

    public function randInt(): int
    {
        $ev = $this->expectRand('int');
        return (int) ($ev['v'] ?? 0);
    }

    /** @return list<Snapshot> */
    public function answerQuery(string $op, string $sig): array
    {
        $ev = $this->popExpect('db', $sig, $op);
        $res = $ev['res'] ?? [];
        if (!is_array($res)) {
            return [];
        }
        if (!array_is_list($res)) {
            return [Snapshot::fromArray($res)];
        }
        return array_map(
            static fn ($r): Snapshot => Snapshot::fromArray(is_array($r) ? $r : []),
            array_values($res)
        );
    }

    public function answerQueryOne(string $op, string $sig): Snapshot
    {
        $ev = $this->popExpect('db', $sig, $op);
        $res = $ev['res'] ?? null;
        if (is_array($res) && array_is_list($res)) {
            return $res === [] ? Snapshot::missing() : Snapshot::fromArray((array) $res[0]);
        }
        return Snapshot::fromArray(is_array($res) ? $res : []);
    }

    /**
     * A write: compared, never executed.
     *
     * A mismatch is appended to `writeDivs` rather than thrown, so one wrong write does not stop
     * the replay from reporting everything after it.
     *
     * @param list<mixed> $args
     */
    public function expectWrite(string $op, string $sig, array $args): void
    {
        $jsonable = $this->redacted(Serial::toJsonable($args));
        $this->writes[] = ['op' => $op, 'sig' => $sig, 'args' => $jsonable];
        $ev = $this->popExpect('db', $sig, $op);
        if (!$this->probe && !Json::equal($ev['args'] ?? null, $jsonable)) {
            $this->writeDivs[] = "write $op $sig: recorded " . self::brief($ev['args'] ?? null)
                . ', replayed ' . self::brief($jsonable);
        }
    }

    /** @param list<mixed> $args */
    public function answerEffect(string $name, array $args): mixed
    {
        $ev = $this->popExpect('fx', null, null, $name);
        if (!$this->probe) {
            $mine = $this->redacted(Serial::toJsonable($args));
            if (!Json::equal($ev['args'] ?? null, $mine)) {
                throw new ReplayDivergence(
                    "effect $name called with different arguments than recorded:\n  recorded: "
                    . self::brief($ev['args'] ?? null) . "\n  replayed: " . self::brief($mine)
                );
            }
        }
        if (isset($ev['err']) && is_array($ev['err'])) {
            throw $this->revive($ev['err']);
        }
        return Serial::fromJsonable($ev['res'] ?? null);
    }

    /**
     * Re-apply the boundary's masking to the *replayed* side before comparing.
     *
     * The tape holds MASKED values; the live code produces raw ones. Comparing the two directly
     * reports a divergence on every secret the code legitimately still handles — a phantom
     * finding that says "the code changed" when nothing changed but the masking. This is
     * precisely why both redaction layers must be idempotent: the value compared here has been
     * through the masker once on the record side and once more on this one.
     */
    private function redacted(mixed $jsonable): mixed
    {
        if ($this->boundary === null) {
            return $jsonable;
        }
        return Serial::redactJsonable($jsonable, $this->boundary->redact, $this->boundary->scrub);
    }

    /** Rebuild a recorded error as its real type, if the boundary declared how. */
    private function revive(array $err): \Throwable
    {
        $type = (string) ($err['type'] ?? '');
        $repr = (string) ($err['repr'] ?? '');
        $args = Serial::fromJsonable($err['args'] ?? []);
        $args = is_array($args) ? array_values($args) : [];

        $build = $this->boundary?->revivers[$type] ?? null;
        if ($build !== null) {
            try {
                $t = $build($args);
                if ($t instanceof \Throwable) {
                    return $t;
                }
            } catch (\Throwable) {
                // A reviver that throws must not become the replay's verdict.
            }
        }
        return new ReplayedEffectError($type, $repr, $args);
    }

    // --- testimony ----------------------------------------------------------------------

    public function note(string $name): void
    {
        $this->sems[] = new SemPair($name, 'point');
    }

    /**
     * @template T
     * @param  callable(): T $body
     * @return T
     */
    public function span(string $name, callable $body): mixed
    {
        $this->sems[] = new SemPair($name, 'begin');
        try {
            return $body();
        } finally {
            // The end still lands whether the body returned or threw — the recorded span did,
            // and a shorter sem sequence would look like a changed account.
            $this->sems[] = new SemPair($name, 'end');
        }
    }

    public static function brief(mixed $v, int $limit = 400): string
    {
        try {
            $s = Json::encode($v);
        } catch (\Throwable) {
            $s = Serial::safeRepr($v);
        }
        return strlen($s) <= $limit ? $s : substr($s, 0, $limit - 1) . "\u{2026}";
    }
}
