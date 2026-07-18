<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Editing a recording.
 *
 * A tape is data, so a hostile world is one mutation away. Empty a result, make an effect
 * throw, run the clock backwards, shrink a population — then replay the **real code** against it
 * and judge what comes out with `Invariants`. This is how you test the failure path you could
 * not provoke in production, using the request that actually happened as the starting point.
 *
 *     $call = Mutate::on($tape->call(0));
 *     $call->read('stream')->setEmpty();
 *     $call->clock()->reverse();
 *     $report = $call->check($resolver, $invariants, $boundary);
 *
 * **Every edit marks the call a probe.** That matters twice over: replay stops comparing
 * arguments (a mutated upstream answer legitimately changes every downstream question), and a
 * saved mutated tape can never later be mistaken for a strict regression pin.
 */
final class Mutate
{
    public static function on(CallView $cv): MutateHandle
    {
        return new MutateHandle($cv);
    }
}
