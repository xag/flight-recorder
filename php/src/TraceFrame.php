<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * One invocation's tracing state.
 *
 * Frames are per-invocation, not per-function: recursive calls to the same function each get
 * their own, so a delta is a change within one execution rather than an artifact of two
 * executions interleaving.
 *
 * @internal
 */
final class TraceFrame
{
    /**
     * The last encoded value seen for each name, as canonical JSON.
     *
     * Comparison is on the ENCODED value, not the live one, because comparing live objects would
     * call user code — and would call a type-changing transition equal.
     *
     * @var array<string, string>
     */
    public array $seen = [];

    public function __construct(public string $lastAt, public readonly string $fn)
    {
    }
}
