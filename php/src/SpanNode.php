<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * One node of a call's semantic skeleton: the call itself, a span, or a point.
 *
 * The raw events a span enclosed hang under it, which is what makes the testimony refutable: a
 * span claiming to have charged a card, with no call beneath it to the thing that charges
 * cards, is a claim a reader can refute.
 */
final class SpanNode
{
    /** @var list<array<string, mixed>> */
    public array $events = [];

    /** @var list<SpanNode> */
    public array $children = [];

    /**
     * @param string                    $phase "call" | "span" | "point"
     * @param string                    $outcome "ok" | "error"; "" for a point
     * @param array<string, mixed>|null $data
     */
    public function __construct(
        public readonly string $name,
        public readonly string $phase,
        public string $outcome = '',
        public readonly ?array $data = null,
    ) {
    }
}
