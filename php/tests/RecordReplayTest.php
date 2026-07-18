<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Boundary;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Recording;
use Xag\FlightRecorder\Replay;

/** Record a run, then replay it against the real code. */
final class RecordReplayTest extends TestCase
{
    use TempDir;

    private function recordGreet(?Boundary $b = null): string
    {
        $b ??= Toy::plainBoundary();
        $rec = Recorder::open($this->tempDir(), $b);
        $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));
        return $rec->path();
    }

    public function testARecordedRunReplaysExactly(): void
    {
        $b = Toy::plainBoundary();
        $r = Replay::replay($this->recordGreet($b), 0, Toy::resolver(), $b);

        self::assertTrue($r->ok(), (string) $r);
        self::assertTrue($r->resultMatch);
        self::assertTrue($r->errorMatch);
        self::assertSame($r->eventsTotal, $r->eventsConsumed);
        self::assertNull($r->divergence);
    }

    /** Replaying a run must not charge the card twice. */
    public function testTheWriteIsComparedNotExecuted(): void
    {
        $b = Toy::plainBoundary();
        $r = Replay::replay($this->recordGreet($b), 0, Toy::resolver(), $b);

        self::assertCount(1, $r->writes);
        self::assertSame('set', $r->writes[0]['op']);
        self::assertSame([], $r->writeDivs);
    }

    public function testCodeThatAsksADifferentQuestionDiverges(): void
    {
        $b = Toy::plainBoundary();
        $path = $this->recordGreet($b);

        $resolver = static fn (string $fn, array $kwargs): callable => static fn (): array => Toy::greet(['user' => 'bob']);
        $r = Replay::replay($path, 0, $resolver, $b);

        self::assertNotNull($r->divergence);
        self::assertStringContainsString('different arguments', $r->divergence);
        self::assertFalse($r->ok());
    }

    public function testCodeThatStopsAskingDiverges(): void
    {
        $b = Toy::plainBoundary();
        $path = $this->recordGreet($b);

        $resolver = static fn (string $fn, array $kwargs): callable => static fn (): array => ['name' => 'Alice'];
        $r = Replay::replay($path, 0, $resolver, $b);

        self::assertLessThan($r->eventsTotal, $r->eventsConsumed);
        self::assertFalse($r->ok());
    }

    /** Without this the replay would take a path the original never took, and blame the code. */
    public function testARecordedErrorIsRevivedWithItsRealType(): void
    {
        $b = Toy::plainBoundary();
        $rec = Recorder::open($this->tempDir(), $b);
        try {
            $rec->call('explode', ['user' => 'ghost'], static fn (): mixed => Toy::explode(['user' => 'ghost']));
        } catch (\Throwable) {
        }

        $r = Replay::replay($rec->path(), 0, Toy::resolver(), $b);
        self::assertTrue($r->errorMatch, (string) $r);
        self::assertSame('no such key: ghost', $r->replayedError);
    }

    public function testWithoutAReviverTheStandInIsHonestAboutTheType(): void
    {
        $b = Toy::plainBoundary();
        $rec = Recorder::open($this->tempDir(), $b);
        try {
            $rec->call('explode', ['user' => 'ghost'], static fn (): mixed => Toy::explode(['user' => 'ghost']));
        } catch (\Throwable) {
        }

        $bare = (new Boundary())->maskFields('password');
        $r = Replay::replay($rec->path(), 0, Toy::resolver(), $bare);
        self::assertNotNull($r->replayedError);
        self::assertStringContainsString('ToyError', $r->replayedError);
    }

    public function testAGateThatNeverAdmitsLeavesNoFile(): void
    {
        $b = Toy::plainBoundary()->enabledWhen(static fn (string $fn, array $k): bool => false);
        $rec = Recorder::open($this->tempDir(), $b);
        $out = $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));

        self::assertSame(['name' => 'Alice'], $out, 'the call must still run and return');
        self::assertNull($rec->path());
        self::assertSame([], $this->tapesIn($this->tempDir()));
    }

    public function testAGateThatThrowsRefusesRatherThanBreakingTheCall(): void
    {
        $b = Toy::plainBoundary()->enabledWhen(static function (string $fn, array $k): bool {
            throw new \RuntimeException('gate exploded');
        });
        $rec = Recorder::open($this->tempDir(), $b);
        $out = $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));

        self::assertSame(['name' => 'Alice'], $out);
        self::assertNull($rec->path());
    }

    public function testSeqIsOneBasedAndContiguous(): void
    {
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        for ($i = 0; $i < 3; $i++) {
            $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));
        }
        $tape = Recording::load($rec->path());
        self::assertSame(3, $tape->numCalls());
        foreach ([0, 1, 2] as $i) {
            self::assertSame($i + 1, $tape->call($i)->raw()['seq']);
        }
    }

    public function testTheSinkIsHandedTheWholeSessionEachTime(): void
    {
        $seen = [];
        $b = Toy::plainBoundary()->publishingTo(static function (string $n, string $t) use (&$seen): void {
            $seen[] = $t;
        });
        $rec = Recorder::open($this->tempDir(), $b);
        $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));

        self::assertGreaterThanOrEqual(2, count($seen), 'published after the header and after the call');
        $last = $seen[count($seen) - 1];
        self::assertStringContainsString('"ev":"session"', $last);
        self::assertStringContainsString('"ev":"call"', $last);
    }

    public function testASinkThatThrowsNeverBreaksTheCall(): void
    {
        $b = Toy::plainBoundary()->publishingTo(static function (string $n, string $t): void {
            throw new \RuntimeException('the bucket is on fire');
        });
        $rec = Recorder::open($this->tempDir(), $b);
        $out = $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));
        self::assertSame(['name' => 'Alice'], $out);
    }

    public function testATornFinalLineIsToleratedAndTheRestIsStillEvidence(): void
    {
        $path = $this->recordGreet();
        $before = Recording::load($path)->numCalls();
        file_put_contents($path, '{"ev":"call","seq":2,"fn":"gre', FILE_APPEND);

        self::assertSame($before, Recording::load($path)->numCalls());
    }

    /**
     * The subtlest one. A secret the code produces from somewhere other than its inputs is
     * masked on the tape and raw in the live run. Comparing those two directly reports a
     * divergence on every such value — "the code changed" when nothing changed but the masking.
     */
    public function testReplayReMasksItsOwnSideBeforeComparing(): void
    {
        $b = (new Boundary())->maskFields('apiKey');
        $tool = static fn (): array => Recorder::effect(
            'store.set',
            [['apiKey' => 'hunter2']],           // regenerated from a constant, not from kwargs
            static fn (): string => 'OK'
        ) === 'OK' ? ['done' => true] : ['done' => false];

        $rec = Recorder::open($this->tempDir(), $b);
        $rec->call('save', [], $tool);

        self::assertStringNotContainsString('hunter2', file_get_contents($rec->path()));

        $r = Replay::replay($rec->path(), 0, static fn (): callable => $tool, $b);
        self::assertNull($r->divergence, 'a re-derived secret must not read as a code change');
        self::assertTrue($r->ok(), (string) $r);
    }

    /**
     * The whole point of freezing the format.
     *
     * Compared against the runtimes shipping the current `enrol` scenario. The Python and .NET
     * fixtures still carry an older variant of it — a chained read where these use an effect,
     * and a different failure message — so their tapes render a different *story*, which is a
     * fact about those fixtures and not about the format. Every checker still accepts every
     * tape; that is the claim this repo actually makes, and `ValidateTest` is where it is made.
     */
    public function testATapeFromAnotherRuntimeReadsIdentically(): void
    {
        $dir = $this->fixturesDir();
        $mine = Recording::load($dir . '/php-sem-toy.jsonl')->call(0)->renderSpans();

        foreach (['java', 'go', 'node'] as $runtime) {
            $theirs = Recording::load($dir . "/$runtime-sem-toy.jsonl")->call(0)->renderSpans();
            self::assertSame(
                $theirs,
                $mine,
                "the $runtime tape and the php tape describe the same scenario and must render "
                . 'character for character alike'
            );
        }
    }

    public function testTheRuntimeKeyNamesPhp(): void
    {
        $tape = Recording::load($this->recordGreet());
        self::assertSame('php', $tape->runtime());
        self::assertSame(PHP_VERSION, $tape->header['php']);
    }
}
