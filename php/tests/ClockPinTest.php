<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Recording;

/**
 * `Recorder::clockAt` makes a recorded now() answer the instant plus the time since — in the
 * instant's timezone — and the tape carries that answer as its `now` event, exactly as if the
 * machine's clock had said so. Running: two reads are two instants, in order, so an app
 * stamping its writes with the clock still stamps them apart. The pin is lifted on exit,
 * nested or not.
 */
final class ClockPinTest extends TestCase
{
    use TempDir;

    private static function within(\DateTimeImmutable $got, \DateTimeImmutable $want): bool
    {
        $d = (float) $got->format('U.u') - (float) $want->format('U.u');
        return $d >= 0 && $d < 5;
    }

    public function testASetClockAnswersTheInstantRunningAndRecordsItAsNow(): void
    {
        $at = new \DateTimeImmutable('2026-08-16T08:00:00+00:00');
        $next = $at->modify('+1 day');
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $tick = static fn (): string => Recorder::now()->format('Y-m-d\TH:i:s.uP');

        $seen = Recorder::clockAt($at, static function () use ($rec, $at, $next, $tick): array {
            $seen = [$rec->call('tick', [], $tick)];
            $first = Recorder::now();
            self::assertTrue(self::within($first, $at), 'first read ' . $first->format('c'));
            $seen[] = Recorder::clockAt($next, static function () use ($rec, $next, $tick): string {
                $v = $rec->call('tick', [], $tick);
                self::assertTrue(self::within(Recorder::now(), $next));
                return $v;
            });
            usleep(5000);
            $seen[] = $rec->call('tick', [], $tick);
            $later = Recorder::now();
            self::assertGreaterThan($first, $later, 'a set clock runs; it does not stop');
            self::assertTrue(self::within($later, $at));
            return $seen;
        });
        self::assertStringStartsWith('2026-08-16T08:00:0', $seen[0]);
        self::assertStringStartsWith('2026-08-17T08:00:0', $seen[1]);
        self::assertStringStartsWith('2026-08-16T08:00:0', $seen[2]);

        // After the block the pin is gone and the clock is the machine's again.
        $drift = abs(Recorder::now()->getTimestamp() - (new \DateTimeImmutable())->getTimestamp());
        self::assertLessThan(5, $drift);

        $tape = Recording::load($rec->path());
        foreach ($seen as $i => $v) {
            $recorded = new \DateTimeImmutable($tape->call($i)->event('now')['v']);
            self::assertEquals(new \DateTimeImmutable($v), $recorded);
        }
    }
}
