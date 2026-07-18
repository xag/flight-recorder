<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Boundary;
use Xag\FlightRecorder\ForbiddenValue;
use Xag\FlightRecorder\Mutate;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Recording;
use Xag\FlightRecorder\TraceHook;
use Xag\FlightRecorder\TraceSink;

/**
 * The tripwire.
 *
 * There are four write paths — the tape's lines, the session header, an event before it
 * buffers, and a re-saved edited tape — plus the trace. A guard on three of them is a guard on
 * none: the one left unwatched is where the secret goes.
 */
final class ForbidTest extends TestCase
{
    use TempDir;

    private const SECRET = 'sk-live-0123456789abcdef';
    private const PATTERN = '/sk-live-[A-Za-z0-9]+/';

    private function guarded(): Boundary
    {
        return (new Boundary())->forbidden(self::PATTERN);
    }

    public function testAForbiddenValueInACallRecordIsRefusedAndNothingIsWritten(): void
    {
        $rec = Recorder::open($this->tempDir(), $this->guarded());

        try {
            $rec->call('save', ['token' => self::SECRET], static fn (): string => 'ok');
            self::fail('the recorder should have refused to write this line');
        } catch (ForbiddenValue $e) {
            self::assertStringContainsString(self::PATTERN, $e->getMessage());
            self::assertStringNotContainsString(
                self::SECRET,
                $e->getMessage(),
                'the guard must never quote what it caught'
            );
        }
    }

    public function testAForbiddenValueInTheHeaderLeavesNoSessionFileAtAll(): void
    {
        $b = $this->guarded()->constant('app.TOKEN', self::SECRET);
        $rec = Recorder::open($this->tempDir(), $b);

        try {
            $rec->call('anything', [], static fn (): string => 'ok');
        } catch (ForbiddenValue) {
        }
        self::assertSame([], $this->tapesIn($this->tempDir()));
    }

    /** "In memory" is a statement about latency, not about confinement. */
    public function testAnInFlightEventIsGuardedBeforeItEntersTheBuffer(): void
    {
        $rec = Recorder::open($this->tempDir(), $this->guarded());

        $this->expectException(ForbiddenValue::class);
        $rec->call('save', [], static fn (): string => Recorder::effect(
            'store.set',
            [self::SECRET],
            static fn (): string => 'OK'
        ));
    }

    public function testAMutationThatPutsASecretBackIsRefusedOnSave(): void
    {
        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));

        $tape = Recording::load($rec->path());
        Mutate::on($tape->call(0))->effect('store.get')->setResult(['token' => self::SECRET]);

        $out = $this->tempDir() . '/edited.jsonl';
        try {
            $tape->forbidding(self::PATTERN)->save($out);
            self::fail('the re-write path must be guarded too');
        } catch (ForbiddenValue $e) {
            self::assertStringNotContainsString(self::SECRET, $e->getMessage());
        }
        self::assertFileDoesNotExist($out, 'a refusal must leave no half-written file behind');
    }

    /** The rules are the boundary's, not the artifact's. */
    public function testATapeDoesNotCarryItsOwnForbidPatterns(): void
    {
        $rec = Recorder::open($this->tempDir(), $this->guarded());
        $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));

        self::assertStringNotContainsString(self::PATTERN, file_get_contents($rec->path()));
    }

    public function testTheTraceIsGuardedToo(): void
    {
        $sink = new TraceSink(null, $this->guarded());
        $prior = TraceHook::setSink($sink);
        try {
            TraceHook::enter('Toy.save', 'Toy.php:1', ['token' => self::SECRET]);
        } finally {
            TraceHook::setSink($prior);
        }

        self::assertSame(self::PATTERN, $sink->refused());
        self::assertSame(0, $sink->count());
        self::assertTrue($sink->snapshot()->isEmpty());
    }

    public function testARefusalWritesASidecarBesideTheTraceAndDestroysIt(): void
    {
        $path = $this->tempDir() . '/trace.jsonl';
        $sink = new TraceSink($path, $this->guarded());
        self::assertFileExists($path);

        $prior = TraceHook::setSink($sink);
        try {
            TraceHook::enter('Toy.save', 'Toy.php:1', ['token' => self::SECRET]);
        } finally {
            TraceHook::setSink($prior);
        }

        self::assertFileDoesNotExist($path, 'the trace must be destroyed, not merely stopped');
        $sidecar = TraceHook::refusalPath($path);
        self::assertFileExists($sidecar);
        self::assertSame(self::PATTERN, file_get_contents($sidecar));
    }

    public function testTracingStaysOffAfterAHit(): void
    {
        $sink = new TraceSink(null, $this->guarded());
        $prior = TraceHook::setSink($sink);
        try {
            TraceHook::enter('Toy.save', 'Toy.php:1', ['token' => self::SECRET]);
            TraceHook::enter('Toy.other', 'Toy.php:2', ['harmless' => 1]);
        } finally {
            TraceHook::setSink($prior);
        }
        self::assertSame(0, $sink->count());
    }

    public function testABadPatternFailsAtDeclarationTimeNotWhenItWouldHaveFired(): void
    {
        $this->expectException(\InvalidArgumentException::class);
        (new Boundary())->forbidden('/([unclosed/');
    }
}
