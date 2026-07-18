<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Re-execute a recorded call with the recording as its world.
 *
 * The recorded answers are fed back in order and the replayed code must ask the same questions
 * in the same order. Writes are compared, never executed: replaying a run must not charge the
 * card twice.
 *
 *     $tape   = Recording::load($path);
 *     $report = Replay::replayCall($tape->call('study_status'), $resolver, $boundary);
 *     if (!$report->ok()) { echo Replay::format(0, $report); }
 *
 * The resolver maps a recorded tool name back to the code that implements it:
 * `fn (string $fn, array $kwargs): ?callable`.
 */
final class Replay
{
    /** Replay call `$index` of the tape at `$path`. */
    public static function replay(
        string $path,
        int $index,
        callable $resolve,
        ?Boundary $boundary = null,
        bool $probe = false,
    ): ReplayReport {
        $rec = Recording::load($path);
        $cv = $rec->call($index);
        if ($cv === null) {
            throw new \InvalidArgumentException("no call at index $index in $path");
        }
        return self::replayCall($cv, $resolve, $boundary, $probe);
    }

    /**
     * Replay one call.
     *
     * @param callable(string, array<string, mixed>): (callable|null) $resolve
     */
    public static function replayCall(
        CallView $cv,
        callable $resolve,
        ?Boundary $boundary = null,
        bool $probe = false,
    ): ReplayReport {
        $probe = $probe || $cv->isProbe();
        $events = $cv->events();
        $feed = new Feed($events, $probe, $boundary);

        $report = new ReplayReport();
        $report->fn = $cv->fn();
        $report->eventsTotal = count($events);
        $report->semsRecorded = self::semPairs($events);
        $report->probe = $probe;
        $report->kwargs = $cv->kwargs();

        $body = $resolve($cv->fn(), $report->kwargs);
        if ($body === null) {
            throw new \InvalidArgumentException('no code resolved for "' . $cv->fn() . '"');
        }

        $priorCall = Recorder::$call;
        $priorFeed = Recorder::$feed;
        $priorBoundary = Recorder::setActiveBoundary($boundary);
        Recorder::$call = null;
        Recorder::$feed = $feed;

        // Mark the tracer's tape before the code runs, so the report carries the trace of THIS
        // replay and not of everything the process has done since it started.
        $mark = TraceHook::mark();

        $result = null;
        $failure = null;
        try {
            $result = $body();
        } catch (ReplayDivergence $e) {
            $report->divergence = $e->getMessage();
        } catch (ProbeUnanswerable $e) {
            $report->unanswerable = $e->getMessage();
        } catch (\Throwable $e) {
            $failure = $e;
        } finally {
            Recorder::$call = $priorCall;
            Recorder::$feed = $priorFeed;
            Recorder::setActiveBoundary($priorBoundary);
        }

        $report->trace = TraceHook::since($mark);

        // Sems trailing the last boundary answer — an outermost span's end, most often — were
        // never reached by a pop. Leaving them unread would report a shorter path than recorded.
        $feed->skipSems();

        $report->eventsConsumed = $feed->consumed;
        $report->skipped = $feed->skipped;
        $report->writeDivs = $feed->writeDivs;
        $report->writes = $feed->writes;
        $report->semsReplayed = $feed->sems;
        $report->semDivergence = self::semDivergence($report->semsRecorded, $report->semsReplayed);
        $report->replayedError = $failure === null ? null : Recorder::render($failure);
        $report->errorMatch = $report->replayedError === $cv->error();

        if ($report->divergence === null && $report->unanswerable === null) {
            $rj = Serial::toJsonable($result);
            if ($boundary !== null) {
                $rj = Serial::redactJsonable($rj, $boundary->redact, $boundary->scrub);
            }
            $report->replayedResult = $rj;
            $report->resultMatch = Json::equal($rj, $cv->raw()['result'] ?? null);
        }

        return $report;
    }

    /**
     * @param  list<array<string, mixed>> $events
     * @return list<SemPair>
     */
    public static function semPairs(array $events): array
    {
        $out = [];
        foreach ($events as $e) {
            if (is_array($e) && ($e['k'] ?? null) === 'sem') {
                $out[] = new SemPair((string) ($e['name'] ?? ''), (string) ($e['phase'] ?? ''));
            }
        }
        return $out;
    }

    /**
     * The first place the code's account of itself differs from the recorded one.
     *
     * @param list<SemPair> $recorded
     * @param list<SemPair> $replayed
     */
    public static function semDivergence(array $recorded, array $replayed): ?string
    {
        $n = max(count($recorded), count($replayed));
        for ($i = 0; $i < $n; $i++) {
            $a = $recorded[$i] ?? null;
            $b = $replayed[$i] ?? null;
            if ($a === null || $b === null || !$a->equals($b)) {
                return "semantic divergence at $i: recorded " . ($a === null ? 'nothing' : (string) $a)
                    . ', replayed ' . ($b === null ? 'nothing' : (string) $b)
                    . ' — the code\'s account of what it was doing has changed';
            }
        }
        return null;
    }

    public static function format(int $index, ReplayReport $r): string
    {
        $out = "call $index {$r->fn}: " . ($r->ok() ? 'OK' : 'FAILED') . "\n";
        if ($r->divergence !== null) {
            $out .= '  ' . $r->divergence . "\n";
        }
        if ($r->unanswerable !== null) {
            $out .= '  ' . $r->unanswerable . "\n";
        }
        if (!$r->probe && !$r->resultMatch && $r->divergence === null) {
            $out .= "  result differs from the recording\n";
        }
        if (!$r->probe && !$r->errorMatch) {
            $out .= '  error differs: replayed ' . var_export($r->replayedError, true) . "\n";
        }
        foreach ($r->writeDivs as $w) {
            $out .= "  write divergence: $w\n";
        }
        if ($r->semDivergence !== null) {
            $out .= '  ' . $r->semDivergence . "\n";
        }
        $out .= "  events {$r->eventsConsumed}/{$r->eventsTotal}";
        if ($r->skipped > 0) {
            $out .= ", {$r->skipped} skipped";
        }
        return $out . "\n";
    }
}
