<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Spec\Validate;

/**
 * The conformance checker.
 *
 * `spec/fixtures/` holds tapes produced by the other implementations. Every one of them must
 * validate here, unread and unadjusted — that is what makes "the tape is one format" a fact
 * rather than an intention. If a fixture fails, this checker is wrong: the fixtures are the
 * evidence and this file is the claim.
 */
final class ValidateTest extends TestCase
{
    use TempDir;

    private const HEADER = '{"ev":"session","version":1,"php":"8.3.0","constants":{},'
        . '"started":"2026-07-18T10:00:00+00:00"}';

    /** The load-bearing cross-runtime test. */
    public function testEveryFixtureIsConformant(): void
    {
        $tapes = $this->tapesIn($this->fixturesDir());
        self::assertNotEmpty($tapes, 'no fixtures found — the sweep would pass vacuously');

        foreach ($tapes as $t) {
            self::assertSame(
                [],
                Validate::file($t),
                basename($t) . ' is not conformant: ' . implode('; ', Validate::file($t))
            );
        }
    }

    public function testTapesFromEveryRuntimeArePresent(): void
    {
        $names = array_map('basename', $this->tapesIn($this->fixturesDir()));
        foreach (['python', 'node', 'dotnet', 'go', 'java', 'php'] as $runtime) {
            self::assertContains(
                "$runtime-toy.jsonl",
                $names,
                "the sweep is missing $runtime's contribution, so it proves less than it claims"
            );
        }
    }

    public function testFloatWhereAnIntIsRequired(): void
    {
        $call = '{"ev":"call","seq":1.0,"fn":"f","kwargs":{},"events":[],"result":null,'
            . '"error":null,"ts":"2026-07-18T10:00:00+00:00","ms":1}';
        self::assertNotEmpty(Validate::tape(self::HEADER . "\n" . $call));

        $sem = $this->callWith('[{"k":"sem","name":"a","phase":"point","sid":1.0}]');
        self::assertNotEmpty(Validate::tape(self::HEADER . "\n" . $sem));
    }

    public function testSessionNamesExactlyOneRuntime(): void
    {
        $two = '{"ev":"session","version":1,"php":"8.3","go":"1.22","constants":{},'
            . '"started":"2026-07-18T10:00:00+00:00"}';
        self::assertNotEmpty(Validate::tape($two));

        $none = '{"ev":"session","version":1,"constants":{},"started":"2026-07-18T10:00:00+00:00"}';
        self::assertNotEmpty(Validate::tape($none));

        self::assertSame([], Validate::tape(self::HEADER));
    }

    /** The asymmetry is deliberate: metadata must be aware, an app-visible value need not be. */
    public function testAwarenessIsRequiredOfMetadataOnly(): void
    {
        $naiveTs = '{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[],"result":null,'
            . '"error":null,"ts":"2026-07-18T10:00:00","ms":1}';
        self::assertNotEmpty(Validate::tape(self::HEADER . "\n" . $naiveTs));

        $naiveNow = $this->callWith('[{"k":"now","v":"2026-07-18T10:00:00"}]');
        self::assertSame([], Validate::tape(self::HEADER . "\n" . $naiveNow));
    }

    public function testCallMustCarryError(): void
    {
        $noError = '{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[],"result":null,'
            . '"ts":"2026-07-18T10:00:00+00:00","ms":1}';
        self::assertNotEmpty(Validate::tape(self::HEADER . "\n" . $noError));
    }

    public function testSpansNestAndClose(): void
    {
        $straddle = $this->callWith(
            '[{"k":"sem","name":"a","phase":"begin","sid":1},'
            . '{"k":"sem","name":"b","phase":"begin","sid":2},'
            . '{"k":"sem","name":"a","phase":"end","sid":1},'
            . '{"k":"sem","name":"b","phase":"end","sid":2}]'
        );
        self::assertStringContainsString(
            'not well-nested',
            implode(' ', Validate::tape(self::HEADER . "\n" . $straddle))
        );

        $unclosed = $this->callWith('[{"k":"sem","name":"a","phase":"begin","sid":1}]');
        self::assertStringContainsString(
            'never closed',
            implode(' ', Validate::tape(self::HEADER . "\n" . $unclosed))
        );

        $reused = $this->callWith(
            '[{"k":"sem","name":"a","phase":"point","sid":1},'
            . '{"k":"sem","name":"b","phase":"point","sid":1}]'
        );
        self::assertStringContainsString(
            'is reused',
            implode(' ', Validate::tape(self::HEADER . "\n" . $reused))
        );
    }

    public function testRandShapes(): void
    {
        $bad = [
            '[{"k":"rand","m":"bytes","n":2,"hex":"AB12"}]',            // uppercase
            '[{"k":"rand","m":"bytes","n":3,"hex":"ab12"}]',            // length mismatch
            '[{"k":"rand","m":"float","v":1.0}]',                       // must be < 1
            '[{"k":"rand","m":"sample","n":2,"kk":1,"idx":[5]}]',       // out of range
            '[{"k":"rand","m":"gaussian","v":1}]',                      // unknown m
        ];
        foreach ($bad as $events) {
            self::assertNotEmpty(
                Validate::tape(self::HEADER . "\n" . $this->callWith($events)),
                "should have been rejected: $events"
            );
        }
        self::assertSame(
            [],
            Validate::tape(self::HEADER . "\n" . $this->callWith('[{"k":"rand","m":"bytes","n":2,"hex":"ab12"}]'))
        );
    }

    public function testUnknownEvAndUnknownKindAreIgnored(): void
    {
        $inflight = '{"ev":"inflight","whatever":true}';
        self::assertSame([], Validate::tape(self::HEADER . "\n" . $inflight));

        $futureKind = $this->callWith('[{"k":"future","anything":1}]');
        self::assertSame([], Validate::tape(self::HEADER . "\n" . $futureKind));
    }

    public function testOnlyTheFinalLineMayBeTorn(): void
    {
        $good = $this->callWith('[]');
        self::assertSame([], Validate::tape(self::HEADER . "\n" . $good . "\n" . '{"ev":"call","se'));

        $tornMiddle = self::HEADER . "\n" . '{"ev":"call","se' . "\n" . $good;
        self::assertNotEmpty(Validate::tape($tornMiddle));
    }

    public function testSeqIsOneBasedAndContiguous(): void
    {
        $two = '{"ev":"call","seq":2,"fn":"f","kwargs":{},"events":[],"result":null,'
            . '"error":null,"ts":"2026-07-18T10:00:00+00:00","ms":1}';
        self::assertStringContainsString(
            '1-based and monotonic',
            implode(' ', Validate::tape(self::HEADER . "\n" . $two))
        );
    }

    public function testValueModel(): void
    {
        $long = str_repeat('x', 201);
        $tooLong = $this->callWith('[]', '{"o":{"__opaque__":"' . $long . '"}}');
        self::assertNotEmpty(Validate::tape(self::HEADER . "\n" . $tooLong));

        $badUndef = $this->callWith('[]', '{"u":{"__undef__":1}}');
        self::assertNotEmpty(Validate::tape(self::HEADER . "\n" . $badUndef));

        // A reserved trace marker is legal and uninterpreted.
        $reserved = $this->callWith('[]', '{"s":{"__snap__":"anything at all"}}');
        self::assertSame([], Validate::tape(self::HEADER . "\n" . $reserved));
    }

    public function testEmptyTape(): void
    {
        self::assertSame(['empty tape: the session header is mandatory'], Validate::tape(''));
    }

    private function callWith(string $eventsJson, string $kwargsJson = '{}'): string
    {
        return '{"ev":"call","seq":1,"fn":"f","kwargs":' . $kwargsJson . ',"events":' . $eventsJson
            . ',"result":null,"error":null,"ts":"2026-07-18T10:00:00+00:00","ms":1}';
    }
}
