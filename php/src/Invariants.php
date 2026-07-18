<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Claims that must hold on every execution.
 *
 * Recordings answer *"same?"*. Invariants answer *"right?"* — and the difference matters,
 * because a bug records just as faithfully as a fix does. A tape pins behaviour to whatever the
 * code did on the day; an invariant pins it to what the code is **supposed** to do, so it still
 * bites when the recorded behaviour was itself wrong.
 *
 * Because a tape is data and this layer only consumes it, an invariant written here judges a
 * tape written by *any* runtime.
 */
final class Invariants
{
    /**
     * Check a tape's call against a list of claims.
     *
     * @param list<Invariant> $invariants
     */
    public static function check(
        Recording $rec,
        int $index,
        callable $resolve,
        array $invariants,
        ?Boundary $boundary = null,
        bool $probe = false,
    ): InvariantReport {
        $cv = $rec->call($index);
        if ($cv === null) {
            throw new \InvalidArgumentException("no call at index $index");
        }
        return self::checkCall($cv, $resolve, $invariants, $boundary, $probe);
    }

    /** @param list<Invariant> $invariants */
    public static function checkCall(
        CallView $cv,
        callable $resolve,
        array $invariants,
        ?Boundary $boundary = null,
        bool $probe = false,
    ): InvariantReport {
        $replay = Replay::replayCall($cv, $resolve, $boundary, $probe);

        $t = new Trajectory(
            Serial::fromJsonable($replay->replayedResult),
            $replay->replayedError,
            $replay->writes,
            $replay->semsReplayed,
            $replay->kwargs,
            $cv->events(),
            $replay->trace,
        );

        $results = [];
        foreach ($invariants as $inv) {
            $results[] = self::safely($inv, $t);
        }
        return new InvariantReport($replay, $results);
    }

    /**
     * Run one claim, catching everything.
     *
     * A broken invariant is a finding about that invariant, not a reason the whole run dies —
     * one claim written badly must not take down the twenty written well, or nobody learns what
     * the other twenty said.
     */
    private static function safely(Invariant $inv, Trajectory $t): InvariantResult
    {
        try {
            $inv->assertOn($t);
            return new InvariantResult($inv->name, true);
        } catch (\Throwable $e) {
            $msg = $e->getMessage();
            return new InvariantResult(
                $inv->name,
                false,
                $msg === '' ? Recorder::shortName($e) : $msg
            );
        }
    }

    public static function format(InvariantReport $r): string
    {
        $out = $r->replay === null ? '' : Replay::format(0, $r->replay);
        foreach ($r->results as $res) {
            $out .= $res->ok
                ? "  ok   {$res->name}\n"
                : "  FAIL {$res->name} — {$res->error}\n";
        }
        return rtrim($out, " \n") . "\n";
    }
}
