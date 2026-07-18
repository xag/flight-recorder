<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The boundary declaration: the one app-specific artifact.
 *
 * A program's execution is fully determined by its code plus its nondeterministic inputs. A
 * Boundary names those inputs and nothing more — the constants to pin in the session header,
 * the two redaction layers, the forbid tripwire, the per-call gate, the error revivers, and
 * the sink. It is the project's first artifact by design: you declare the nondeterminism
 * boundary before you record across it.
 *
 * The recorder cannot know about an input it was never told crosses the boundary. When an app
 * grows a new one — an HTTP call, a clock read, a new random use — it is added here. That is
 * the whole maintenance contract.
 *
 * Every mutator returns `$this`, so a boundary reads as a declaration:
 *
 *     $boundary = (new Boundary())
 *         ->constant('App\Tools::LIMIT', App\Tools::LIMIT)
 *         ->maskFields('password', 'token')
 *         ->scrubbing('/sk-live-[A-Za-z0-9]+/')
 *         ->forbidden('/-----BEGIN [A-Z ]*PRIVATE KEY-----/')
 *         ->reviving(RateLimited::class, fn (array $a) => new RateLimited(...$a));
 *
 * ## The three layers of masking
 *
 * 1. **By field name** (`maskFields`, `redacting`) — keyed on where a value sits.
 * 2. **By value** (`scrubbing`, `scrubbingWith`) — keyed on what a value looks like, swept
 *    over every leaf string wherever it sits. A secret with no field name is invisible to (1):
 *    passed positionally it lands in `args` with nothing but an index; baked into a cache key
 *    or dropped mid-sentence into a body it is a substring of a value nobody named.
 * 3. **The tripwire** (`forbidden`) — states the property the first two cannot: *this tape
 *    carries no credential*. Matched against the fully-masked line about to be written.
 */
final class Boundary
{
    /** @var array<string, mixed> */
    public array $constants = [];

    /** @var array<string, mixed> */
    public array $headerExtras = [];

    /** @var array<string, (callable(mixed): mixed)|null> field name → transform, null = flat mask */
    public array $redact = [];

    /** @var (callable(string): string)|null */
    public $scrub = null;

    /** @var list<string> PCRE patterns, delimiters included */
    public array $forbid = [];

    /** @var array<string, callable(list<mixed>): \Throwable> */
    public array $revivers = [];

    /** @var (callable(string, array<string, mixed>): bool)|null */
    public $enabled = null;

    public ?Sink $sink = null;

    /**
     * Pin an env-derived constant into the session header, so replay restores the world the
     * code was configured with rather than the one the replaying machine happens to have.
     */
    public function constant(string $name, mixed $value): self
    {
        $this->constants[$name] = $value;
        return $this;
    }

    /** An extra header key — a schema digest, a build id, anything a later reader will want. */
    public function headerExtra(string $name, mixed $value): self
    {
        $this->headerExtras[$name] = $value;
        return $this;
    }

    /** Layer 1: mask these field names flat, wherever they sit in a recorded payload. */
    public function maskFields(string ...$names): self
    {
        foreach ($names as $n) {
            $this->redact[$n] = null;
        }
        return $this;
    }

    /**
     * Layer 1 with a transform — tokenize rather than blank out, so a value stays joinable.
     *
     * The transform must be deterministic AND idempotent: replay re-applies it to values that
     * have already been through it once. Its output also meets the value sweep, so a transform
     * that shortens rather than masks cannot smuggle the secret past.
     *
     * @param callable(mixed): mixed $transform
     */
    public function redacting(string $name, callable $transform): self
    {
        $this->redact[$name] = $transform;
        return $this;
    }

    /**
     * Layer 2: replace everything matching `$pattern` with `$mask`, in every leaf string.
     *
     * Stacks — call it once per secret shape rather than building one unreadable mega-regex.
     *
     * **A mask that matches its own pattern is refused here, at declaration time.** Replay
     * re-derives the question it is about to ask, scrubs it the same way, and compares the
     * result against the tape — so scrubbing has to be idempotent, and a mask that matches its
     * own pattern is not: the first pass masks the secret, the second masks the mask, and
     * replay reports a divergence on a value that never changed. Refusing it now is much
     * kinder than discovering it as a phantom divergence six months later.
     */
    public function scrubbing(string $pattern, string $mask = Serial::REDACTED): self
    {
        if (@preg_match($pattern, '') === false) {
            throw new \InvalidArgumentException(
                "scrubbing() was given a pattern PCRE cannot compile: $pattern"
            );
        }
        if (preg_match($pattern, $mask) === 1) {
            throw new \InvalidArgumentException(
                "the mask '$mask' matches its own pattern $pattern, so scrubbing would not be "
                . 'idempotent: the first pass masks the secret and the second masks the mask, '
                . 'and replay would report a divergence on a value that never changed'
            );
        }
        return $this->scrubbingWith(
            static fn (string $s): string => (string) preg_replace($pattern, $mask, $s)
        );
    }

    /**
     * Layer 2 with an arbitrary transform. Must be idempotent, for the reason above.
     *
     * @param callable(string): string $transform
     */
    public function scrubbingWith(callable $transform): self
    {
        $prior = $this->scrub;
        $this->scrub = $prior === null
            ? $transform
            : static fn (string $s): string => $transform($prior($s));
        return $this;
    }

    /**
     * Layer 3, the tripwire: refuse to write any line matching `$pattern`.
     *
     * Match **shapes, not values**: a credential you can enumerate you can already redact.
     * This is for the one you cannot — the shape of any private key, any bearer token — so
     * that a redaction rule which silently stopped matching fails a build instead of shipping
     * a secret.
     *
     * The pattern is compiled immediately, purely to validate it: a tripwire that turns out to
     * be malformed at the moment it should have fired is not a tripwire.
     */
    public function forbidden(string $pattern): self
    {
        if (@preg_match($pattern, '') === false) {
            throw new \InvalidArgumentException(
                "forbidden() was given a pattern PCRE cannot compile: $pattern"
            );
        }
        $this->forbid[] = $pattern;
        return $this;
    }

    /**
     * Record one call, not one deployment: a per-call gate.
     *
     * A gate that throws is a refusal — it can never break the call it was asked about. A gate
     * that never admits leaves no session file at all, so a process that records nothing is
     * indistinguishable from one with the recorder uninstalled.
     *
     * @param callable(string, array<string, mixed>): bool $gate
     */
    public function enabledWhen(callable $gate): self
    {
        $this->enabled = $gate;
        return $this;
    }

    /** Publish the session somewhere besides local disk. See Sink. */
    public function publishingTo(Sink|callable $sink): self
    {
        $this->sink = $sink instanceof Sink ? $sink : new CallableSink($sink);
        return $this;
    }

    /**
     * Rebuild a recorded error as its real type on replay.
     *
     * Code branches on exception type — `catch (RateLimited $e)` takes a different path from
     * `catch (NotFound $e)` — so a replay that threw one generic stand-in for every recorded
     * error would send execution down a path the original never took, and then report the
     * resulting difference as a divergence in the code.
     *
     * The builder receives the recorded `err.args`.
     *
     * @param callable(list<mixed>): \Throwable $build
     */
    public function reviving(string $errorType, callable $build): self
    {
        $this->revivers[$errorType] = $build;
        return $this;
    }

    /** Whether the gate admits this call. A gate that throws is a refusal. */
    public function admits(string $fn, array $kwargs): bool
    {
        if ($this->enabled === null) {
            return true;
        }
        try {
            return (bool) ($this->enabled)($fn, $kwargs);
        } catch (\Throwable) {
            return false;
        }
    }
}
