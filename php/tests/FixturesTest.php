<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Spec\Validate;

/**
 * PHP's contribution to the cross-runtime fixture sweep.
 *
 * The two tapes this writes are read by every other runtime's conformance suite. That is the
 * whole point of the arrangement: a fork of the format fails a build that is not this one.
 */
final class FixturesTest extends TestCase
{
    use TempDir;

    /** Always runs, into a temp dir, so the committed fixtures are never touched by `phpunit`. */
    public function testScenariosConform(): void
    {
        $toy = $this->recordToy($this->tempDir());
        $sem = $this->recordSemToy($this->tempDir());

        self::assertSame([], Validate::file($toy), 'the toy tape is not conformant');
        self::assertSame([], Validate::file($sem), 'the sem tape is not conformant');

        $semText = file_get_contents($sem);
        // If any of these stopped appearing the fixture would still be conformant and would no
        // longer be evidence.
        foreach (['"phase":"begin"', '"phase":"end"', '"phase":"point"',
                  '"outcome":"ok"', '"outcome":"error"', '__dt__', '[REDACTED]'] as $needle) {
            self::assertStringContainsString($needle, $semText, "the sem fixture no longer shows $needle");
        }
        self::assertStringNotContainsString('hunter2', $semText, 'the sem fixture leaks the secret');
    }

    public function testToyCarriesEveryRandomShape(): void
    {
        $text = file_get_contents($this->recordToy($this->tempDir()));
        foreach (['"m":"sample"', '"m":"bytes"', '"m":"float"', '"m":"int"'] as $shape) {
            self::assertStringContainsString($shape, $text, "the toy fixture no longer draws $shape");
        }
    }

    /**
     * Rewrite the committed fixtures. Env-gated on purpose.
     *
     * These are bytes under version control, and a test that rewrote them on every run would
     * turn "the fixtures changed" into noise nobody reads.
     */
    public function testRegenerateFixtures(): void
    {
        if (getenv('FR_REGEN_FIXTURES') === false) {
            self::markTestSkipped('set FR_REGEN_FIXTURES=1 to rewrite spec/fixtures/php-*.jsonl');
        }
        $out = $this->fixturesDir();
        $this->publish($this->recordToy($this->tempDir()), $out . DIRECTORY_SEPARATOR . 'php-toy.jsonl');
        $this->publish($this->recordSemToy($this->tempDir()), $out . DIRECTORY_SEPARATOR . 'php-sem-toy.jsonl');
        self::assertTrue(true);
    }

    /** A bad fixture is worse than no fixture, because every other runtime's suite trusts it. */
    private function publish(string $from, string $to): void
    {
        $violations = Validate::file($from);
        self::assertSame([], $violations, 'refusing to publish a non-conformant fixture');
        copy($from, $to);
    }

    private function recordToy(string $dir): string
    {
        $rec = Recorder::open($dir, Toy::plainBoundary());
        $rec->call('greet', ['user' => 'alice'], static fn (): array => Toy::greet(['user' => 'alice']));
        try {
            $rec->call('explode', ['user' => 'ghost'], static fn (): mixed => Toy::explode(['user' => 'ghost']));
        } catch (\Throwable) {
            // recorded, and re-raised exactly as it was: that is the point of the scenario
        }
        return $rec->path();
    }

    private function recordSemToy(string $dir): string
    {
        $kwargs = ['user' => 'alice', 'password' => 'hunter2'];
        $rec = Recorder::open($dir, Toy::semBoundary());
        $rec->call('enrol', $kwargs, static fn (): array => Toy::enrol($kwargs));
        return $rec->path();
    }
}
