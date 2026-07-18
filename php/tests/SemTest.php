<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\CallView;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Recording;
use Xag\FlightRecorder\Replay;
use Xag\FlightRecorder\ReplayedEffectError;
use Xag\FlightRecorder\Serial;

/** Semantic spans: the app's own testimony, written next to the evidence. */
final class SemTest extends TestCase
{
    use TempDir;

    private const KWARGS = ['user' => 'alice', 'password' => 'hunter2'];

    private function record(): CallView
    {
        $rec = Recorder::open($this->tempDir(), Toy::semBoundary());
        $rec->call('enrol', self::KWARGS, static fn (): array => Toy::enrol(self::KWARGS));
        return Recording::load($rec->path())->call(0);
    }

    public function testTheSpanTreeIsRecoveredFromOrderAlone(): void
    {
        $root = $this->record()->spans();

        self::assertSame('enrol', $root->name);
        self::assertSame('call', $root->phase);
        self::assertCount(1, $root->children);

        $enrol = $root->children[0];
        self::assertSame('enrol', $enrol->name);
        self::assertSame('ok', $enrol->outcome);
        self::assertSame(
            ['load_corpus', 'corpus_read', 'register', 'registration_failed'],
            array_map(static fn ($c): string => $c->name, $enrol->children)
        );
    }

    public function testASpanWhoseBodyThrewEndsWithOutcomeError(): void
    {
        $enrol = $this->record()->spans()->children[0];
        $register = $enrol->children[2];
        self::assertSame('register', $register->name);
        self::assertSame('error', $register->outcome);
    }

    public function testRawEventsHangUnderTheSpanThatEnclosedThem(): void
    {
        $enrol = $this->record()->spans()->children[0];

        $loadCorpus = $enrol->children[0];
        self::assertCount(1, $loadCorpus->events);
        self::assertSame('store.get', $loadCorpus->events[0]['fn']);

        self::assertCount(2, $enrol->children[2]->events);
    }

    /** It belongs to the call, not to the act. */
    public function testTheClockReadOutsideTheSpanBelongsToTheCall(): void
    {
        $root = $this->record()->spans();
        self::assertCount(1, $root->events);
        self::assertSame('now', $root->events[0]['k']);
    }

    public function testRedactionReachesIntoSpanData(): void
    {
        $cv = $this->record();
        $sems = array_values(array_filter($cv->events(), static fn ($e): bool => $e['k'] === 'sem'));
        self::assertSame(Serial::REDACTED, $sems[0]['data']['password']);
    }

    /** The absence of detail is not itself a detail. */
    public function testEmptyDataIsOmittedNotWrittenAsAnEmptyObject(): void
    {
        $cv = $this->record();
        $sems = array_values(array_filter($cv->events(), static fn ($e): bool => $e['k'] === 'sem'));
        self::assertSame('load_corpus', $sems[1]['name']);
        self::assertArrayNotHasKey('data', $sems[1]);
    }

    public function testSidsAreUniqueWithinACallAndAnEndRepeatsItsBegin(): void
    {
        $cv = $this->record();
        $sems = array_values(array_filter($cv->events(), static fn ($e): bool => $e['k'] === 'sem'));

        self::assertSame(1, $sems[0]['sid']);
        $last = $sems[count($sems) - 1];
        self::assertSame('end', $last['phase']);
        self::assertSame(1, $last['sid']);

        $points = array_filter($sems, static fn ($e): bool => $e['phase'] !== 'end');
        $sids = array_map(static fn ($e): int => $e['sid'], $points);
        self::assertSame(count($sids), count(array_unique($sids)));
    }

    /** Testimony is not evidence. But it IS consumed. */
    public function testSemsAreNeverFedBackToTheReplayedCode(): void
    {
        $b = Toy::semBoundary();
        $rec = Recorder::open($this->tempDir(), $b);
        $rec->call('enrol', self::KWARGS, static fn (): array => Toy::enrol(self::KWARGS));

        $r = Replay::replay($rec->path(), 0, Toy::resolver(), $b);
        self::assertTrue($r->ok(), (string) $r);
        self::assertSame($r->eventsTotal, $r->eventsConsumed);
        self::assertNull($r->semDivergence);
    }

    public function testAChangedAccountIsReportedButDoesNotGate(): void
    {
        $b = Toy::semBoundary();
        $rec = Recorder::open($this->tempDir(), $b);
        $rec->call('enrol', self::KWARGS, static fn (): array => Toy::enrol(self::KWARGS));

        // The same execution, telling a differently-named story.
        $renamed = static function (string $fn, array $kwargs): callable {
            return static function () use ($kwargs): array {
                $user = (string) $kwargs['user'];
                $started = Recorder::now();
                return Recorder::span('signup', ['user' => $user, 'started' => $started], static function () use ($user): array {
                    $row = Recorder::span('load_corpus', [], static fn (): array => Toy::storeGet($user));
                    Recorder::note('corpus_read', ['found' => true]);
                    try {
                        Recorder::span('register', ['password' => 'hunter2'], static function () use ($user): void {
                            Recorder::effect('store.set', ["user:$user", ['password' => 'hunter2']], static fn (): string => 'OK');
                            Toy::storeBoom($user);
                        });
                    } catch (ToyError | ReplayedEffectError $e) {
                        Recorder::note('registration_failed', ['why' => $e->getMessage()]);
                    }
                    return ['user' => $user, 'name' => $row['name']];
                });
            };
        };

        $r = Replay::replay($rec->path(), 0, $renamed, $b);
        self::assertNotNull($r->semDivergence);
        self::assertStringContainsString('account of what it was doing', $r->semDivergence);
        self::assertTrue($r->ok(), 'a changed account is a third signal, and it does not gate');
    }

    public function testTheRenderedTreeReadsTopDown(): void
    {
        $render = $this->record()->renderSpans();
        $lines = explode("\n", $render);

        self::assertStringStartsWith('enrol  ok', $lines[0]);
        self::assertStringContainsString('load_corpus  ok  (1 fx)', $render);
        self::assertStringContainsString('register  ERROR  (2 fx)', $render);
        self::assertStringContainsString('- corpus_read  found=true', $render);
    }
}
