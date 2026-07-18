<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Where a session goes besides local disk, so recordings are retrievable from a machine you
 * have no shell on.
 *
 * The sink is handed the session file's **name and its full current text**, after the header
 * and again after every completed call. Being handed the whole session each time is what makes
 * an overwriting sink — S3 `PutObject`, a KV `set` — sufficient, and it means a published tape
 * is never half a tape.
 *
 * It is best-effort: a `publish` that throws is swallowed, because recording must never be the
 * reason a call fails. Hand the bytes off and return — a `publish` that blocks stalls the call
 * that triggered it. PHP's request lifecycle gives you no background thread to hide the latency
 * in, so a slow sink is felt directly by the user; queue it, or accept the cost knowingly.
 */
interface Sink
{
    public function publish(string $name, string $text): void;
}
