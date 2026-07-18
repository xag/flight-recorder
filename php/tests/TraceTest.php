<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Trace;
use Xag\FlightRecorder\Tracer;
use Xag\FlightRecorder\TraceRun;

/** Variable-level tracing: every local, on every executed line. */
final class TraceTest extends TestCase
{
    private const SUBJECT = 'Xag\\FlightRecorder\\Traced\\TracedToy';

    private function source(): string
    {
        return __DIR__ . '/resources/TracedToy.php';
    }

    private function grade(array $answers): TraceRun
    {
        return Tracer::run([$this->source()], self::SUBJECT, 'gradePercent', $answers);
    }

    /** Tracing must not change the answer, including when the answer is wrong. */
    public function testTheInstrumentedCopyStillParsesAndStillComputes(): void
    {
        $run = $this->grade([1, 1, -1, 0]);
        self::assertSame(66, $run->result);
        self::assertFalse($run->trace->isEmpty());
    }

    /**
     * Values are DATA, not renderings — this is the whole point of trace version 2, and it is
     * what lets a claim do arithmetic instead of string matching.
     */
    public function testAVariableTimelineIsALookupNotAnInference(): void
    {
        $t = $this->grade([1, 1, -1, 0])->trace;

        self::assertSame(3, $t->last('answered')->value);
        self::assertSame(2, $t->last('correct')->value);
        self::assertSame(66, $t->last('pct')->value);
        self::assertIsInt($t->last('answered')->value);
    }

    public function testTheArgumentsAVariableArrivedWithAreRecorded(): void
    {
        $t = $this->grade([1, 1, -1, 0])->trace;
        $calls = $t->calls('gradePercent');

        self::assertCount(1, $calls);
        self::assertSame([1, 1, -1, 0], $calls[0]['args']['answers']);
    }

    public function testAReturnIsObserved(): void
    {
        $t = $this->grade([1, 1, -1, 0])->trace;
        $returns = $t->returns('gradePercent');

        self::assertCount(1, $returns);
        self::assertSame(66, $returns[0]['value']);
    }

    /**
     * The reason the feature exists.
     *
     * The result passes every plausible check about itself — an integer, between 0 and 100 — and
     * is still wrong. No claim about the RESULT can catch this. The claim that catches it is
     * about an internal variable.
     */
    public function testTheBugIsCondemnedByItsOwnTrace(): void
    {
        $answers = [1, 1, -1, 0];
        $run = $this->grade($answers);

        self::assertGreaterThanOrEqual(0, $run->result);
        self::assertLessThanOrEqual(100, $run->result);

        $answered = $run->trace->last('answered')->value;
        self::assertNotSame(
            count($answers),
            $answered,
            'the percentage is computed over questions ANSWERED, not questions ASKED'
        );
        self::assertSame(3, $answered);
    }

    public function testAnExceptionIsObservedWithTheStateUpToTheThrow(): void
    {
        $threw = false;
        try {
            Tracer::run([$this->source()], self::SUBJECT, 'divide', 7, 0);
        } catch (\DivisionByZeroError) {
            $threw = true;
        }
        self::assertTrue($threw, 'the exception must propagate untouched');

        $run = Tracer::run([$this->source()], self::SUBJECT, 'divide', 7, 2);
        self::assertSame(35, $run->result);
        self::assertSame(70, $run->trace->last('scaled')->value);
    }

    public function testAnUntracedProcessAnswersNeverObservedRatherThanPassingVacuously(): void
    {
        $t = Trace::empty();

        self::assertTrue($t->isEmpty());
        self::assertSame([], $t->values('answered'));
        self::assertNull($t->last('answered'));
        self::assertSame('answered: never observed', $t->render('answered'));
    }

    public function testAVersionOneTraceIsRefusedRatherThanHalfUnderstood(): void
    {
        $this->expectExceptionMessageMatches('/older tracer/');
        Trace::parse('{"e":"H","trace_version":1}' . "\n" . '{"e":"L","fn":"f","at":"x:1","d":{}}');
    }

    public function testTheTraceRoundTripsThroughItsArtifactForm(): void
    {
        $t = $this->grade([1, 1, -1, 0])->trace;
        $again = Trace::parse($t->toJsonl());

        self::assertSame($t->size(), $again->size());
        self::assertSame($t->last('pct')->value, $again->last('pct')->value);
    }

    /**
     * A location past the end of the real file would mean the trace is pointing at a file that
     * exists nowhere on the reader's disk.
     */
    public function testLocationsPointAtTheOriginalFileNotTheInstrumentedOne(): void
    {
        $t = $this->grade([1, 1, -1, 0])->trace;
        $realLines = count(file($this->source()));

        self::assertNotEmpty($t->events);
        foreach ($t->events as $e) {
            $at = (string) $e['at'];
            self::assertStringStartsWith('TracedToy.php:', $at);
            $line = (int) substr($at, strlen('TracedToy.php:'));
            self::assertGreaterThan(0, $line);
            self::assertLessThanOrEqual($realLines, $line, "location $at is past the end of the file");
        }
    }

    /** The delta belongs to the statement that produced it, not the one about to run. */
    public function testADeltaIsReportedAtTheStatementThatCausedIt(): void
    {
        $t = $this->grade([1, 1, -1, 0])->trace;
        $src = file($this->source());

        $obs = $t->first('pct');
        self::assertNotNull($obs);
        $line = (int) substr($obs->at, strlen('TracedToy.php:'));
        self::assertStringContainsString(
            '$pct = intdiv',
            $src[$line - 1],
            'the observation of $pct must name the line that assigned it'
        );
    }
}
