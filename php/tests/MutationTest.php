<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Invariant;
use Xag\FlightRecorder\Invariants;
use Xag\FlightRecorder\Mutate;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Recording;
use Xag\FlightRecorder\Trajectory;

/** Editing a tape into a world that never happened, and judging what the real code does in it. */
final class MutationTest extends TestCase
{
    use TempDir;

    private function greetTape(): Recording
    {
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));
        return Recording::load($rec->path());
    }

    /** The REAL code ran against the edited world — not a simulation of it. */
    public function testAMutatedAnswerFlowsThroughTheRealCode(): void
    {
        $tape = $this->greetTape();
        $call = Mutate::on($tape->call(0));
        $call->effect('store.get')->setResult(['name' => 'Zara', 'x' => 3]);

        $r = $call->replay(Toy::resolver(), Toy::plainBoundary());
        self::assertNull($r->divergence, (string) $r);
        self::assertSame('Zara', $r->replayedResult['name']);
    }

    public function testEveryEditMarksTheCallAProbeAndThatSurvivesASave(): void
    {
        $tape = $this->greetTape();
        self::assertFalse($tape->call(0)->isProbe());

        Mutate::on($tape->call(0))->effect('store.get')->setResult(['name' => 'Zara']);
        self::assertTrue($tape->call(0)->isProbe());

        $out = $this->tempDir() . '/edited.jsonl';
        $tape->save($out);
        self::assertTrue(Recording::load($out)->call(0)->isProbe());
    }

    public function testProbeReplayStopsComparingArgumentsButStillGatesOnOrder(): void
    {
        $tape = $this->greetTape();
        $call = Mutate::on($tape->call(0));
        $call->effect('store.get')->setResult(['name' => 'Zara', 'x' => 99]);

        $r = $call->replay(Toy::resolver(), Toy::plainBoundary());
        self::assertTrue($r->probe);
        self::assertNull($r->divergence);
        self::assertTrue($r->ok(), (string) $r);
    }

    public function testAProbeIsJudgedByItsClaimsNotByAMatch(): void
    {
        $tape = $this->greetTape();
        $call = Mutate::on($tape->call(0));
        $call->effect('store.get')->setResult(['name' => 'Zara', 'x' => 3]);

        $invariants = [
            Invariant::of('a name always comes back', static function (Trajectory $t): void {
                if (($t->resultArray()['name'] ?? '') === '') {
                    throw new \RuntimeException('no name in the result');
                }
            }),
            Invariant::of('the greeting is written down', static function (Trajectory $t): void {
                if ($t->writes === []) {
                    throw new \RuntimeException('nothing was written');
                }
            }),
        ];

        $report = $call->check(Toy::resolver(), $invariants, Toy::plainBoundary());
        self::assertTrue($report->ok(), (string) $report);
        self::assertSame([], $report->violations());
    }

    /** One claim written badly must not take down the twenty written well. */
    public function testAClaimThatFailsIsReportedWithoutTakingTheRunDown(): void
    {
        $tape = $this->greetTape();
        $call = Mutate::on($tape->call(0));
        $call->effect('store.get')->setResult(['name' => 'Zara']);

        $invariants = [
            Invariant::of('this one fails', static function (Trajectory $t): void {
                throw new \RuntimeException('deliberately false');
            }),
            Invariant::of('this one is broken', static function (Trajectory $t): void {
                /** @phpstan-ignore-next-line intentionally calling a method that does not exist */
                $t->noSuchMethod();
            }),
            Invariant::of('this one holds', static function (Trajectory $t): void {
            }),
        ];

        $report = $call->check(Toy::resolver(), $invariants, Toy::plainBoundary());
        self::assertCount(2, $report->violations());
        self::assertTrue($report->results[2]->ok);
        self::assertFalse($report->ok());
    }

    public function testAnInvariantAboutAnUntracedVariableFailsRatherThanPassingVacuously(): void
    {
        $tape = $this->greetTape();
        $call = Mutate::on($tape->call(0));
        $call->effect('store.get')->setResult(['name' => 'Zara']);

        $inv = [Invariant::of('level was observed', static function (Trajectory $t): void {
            if ($t->trace->last('level') === null) {
                throw new \RuntimeException('level: never observed');
            }
        })];

        $report = $call->check(Toy::resolver(), $inv, Toy::plainBoundary());
        self::assertFalse($report->ok());
        self::assertStringContainsString('never observed', $report->violations()[0]->error);
    }

    public function testTheClockCanBeRunBackwards(): void
    {
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('twice', [], static function (): array {
            return [Recorder::now()->format('c'), Recorder::now()->format('c')];
        });
        $tape = Recording::load($rec->path());

        $call = Mutate::on($tape->call(0));
        $before = $call->clock()->times();
        self::assertCount(2, $before);

        $call->clock()->setTimes('1999-12-31T23:59:59+00:00', '1999-01-01T00:00:00+00:00');
        $after = $call->clock()->times();

        self::assertNotSame($before, $after);
        self::assertSame('1999-12-31T23:59:59+00:00', $after[0]);
        self::assertTrue($tape->call(0)->isProbe());
    }

    public function testTheClockCanBeReversed(): void
    {
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('twice', [], static function (): array {
            return [Recorder::now()->format('c'), Recorder::now()->format('c')];
        });
        $tape = Recording::load($rec->path());

        $call = Mutate::on($tape->call(0));
        $before = $call->clock()->times();
        $call->clock()->reverse();
        self::assertSame(array_reverse($before), $call->clock()->times());
    }

    public function testAReadCanBeEmptied(): void
    {
        $tape = $this->greetTape();
        $call = Mutate::on($tape->call(0));
        $call->read('stream')->setEmpty();

        $r = $call->replay(Toy::resolver(), Toy::plainBoundary());
        self::assertNull($r->divergence, (string) $r);
        self::assertTrue($r->ok(), (string) $r);
    }

    public function testAnEffectCanBeMadeToFail(): void
    {
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        try {
            $rec->call('explode', ['user' => 'ghost'], static fn (): mixed => Toy::explode(['user' => 'ghost']));
        } catch (\Throwable) {
        }
        $tape = Recording::load($rec->path());

        $call = Mutate::on($tape->call(0));
        $call->effect('store.boom')->setError('ToyError', 'the store is down', 500);

        $r = $call->replay(Toy::resolver(), Toy::plainBoundary());
        self::assertSame('the store is down', $r->replayedError, (string) $r);
    }

    /**
     * This impeaches neither the code nor the recording, only their pairing — so it must not be
     * dressed up as "the code changed".
     */
    public function testAMutationOntoAnUnanswerablePathIsNotReportedAsADivergence(): void
    {
        $tape = $this->greetTape();
        $cv = $tape->call(0);

        $raw = $cv->raw();
        $raw['events'] = [$raw['events'][0]];   // the tape now runs out almost immediately
        $raw['probe'] = true;
        $cv->setRaw($raw);

        $r = $tape->call(0);
        $report = \Xag\FlightRecorder\Replay::replayCall($r, Toy::resolver(), Toy::plainBoundary(), true);

        self::assertNotNull($report->unanswerable);
        self::assertNull($report->divergence);
        self::assertFalse($report->ok());
    }
}
