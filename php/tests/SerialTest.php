<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Boundary;
use Xag\FlightRecorder\Json;
use Xag\FlightRecorder\Serial;
use Xag\FlightRecorder\Snapshot;

/** The boundary value codec and both redaction layers. */
final class SerialTest extends TestCase
{
    // --- the codec -----------------------------------------------------------------------

    public function testADatetimeIsItsOwnMarker(): void
    {
        $d = new \DateTimeImmutable('2026-07-18T10:30:00+02:00');
        self::assertSame(['__dt__' => '2026-07-18T10:30:00+02:00'], Serial::toJsonable($d));
        self::assertInstanceOf(\DateTimeImmutable::class, Serial::fromJsonable(Serial::toJsonable($d)));
    }

    /** PHP has one nothing; the marker exists for the runtime that has two. */
    public function testUndefinedRevivesToNullEvenThoughPhpNeverEmitsIt(): void
    {
        self::assertNull(Serial::fromJsonable(['__undef__' => true]));
        self::assertNotSame('__undef__', array_key_first((array) Serial::toJsonable(null)) ?? '');
        self::assertNull(Serial::toJsonable(null));
    }

    /** PHP has no date-only type, so `__date__` is revived and never emitted. */
    public function testADateRevivesEvenThoughPhpNeverEmitsIt(): void
    {
        self::assertInstanceOf(\DateTimeImmutable::class, Serial::fromJsonable(['__date__' => '2026-07-18']));
    }

    public function testNanAndInfinityDegradeRatherThanBreakingTheLine(): void
    {
        foreach ([NAN, INF, -INF] as $v) {
            $enc = Serial::toJsonable($v);
            self::assertArrayHasKey('__opaque__', (array) $enc);
        }
        // and the line still encodes
        self::assertIsString(Json::encode(Serial::toJsonable(['x' => NAN])));
    }

    public function testAnOpaqueMarkerIsCappedAndCarriesNoIdentity(): void
    {
        $obj = new \stdClass();
        $obj->a = 1;
        // stdClass is structure; an arbitrary class instance is not.
        $enc = Serial::toJsonable(new ToyError('boom', 1));
        self::assertArrayHasKey('__opaque__', $enc);
        self::assertLessThanOrEqual(200, strlen($enc['__opaque__']));
        self::assertDoesNotMatchRegularExpression('/0x[0-9a-f]+/i', $enc['__opaque__']);
        self::assertStringNotContainsString('#', $enc['__opaque__']);
    }

    public function testDeeplyNestedValuesDegradeRatherThanRecursingForever(): void
    {
        $v = 'leaf';
        for ($i = 0; $i < 40; $i++) {
            $v = ['down' => $v];
        }
        $enc = Serial::toJsonable($v);          // must terminate, must not throw
        self::assertIsString(Json::encode($enc));
    }

    public function testAnEmptyStdClassSurvivesAsAnObjectAndAnEmptyArrayAsAList(): void
    {
        // The one PHP-shaped decision, asserted rather than assumed.
        self::assertSame('{}', Json::encode(Serial::toJsonable(new \stdClass())));
        self::assertSame('[]', Json::encode(Serial::toJsonable([])));
    }

    /**
     * An object's public surface is data; its internals are its own business.
     *
     * This is what lets replay hand a declared type back to code that asked for one — without
     * it, an object round-trips as an opaque marker and the replayed code dies on a TypeError
     * the recorder itself caused.
     */
    public function testAnObjectsPublicSurfaceIsRecordedAndItsInternalsAreNot(): void
    {
        $enc = Serial::toJsonable(new class {
            public string $name = 'Alice';
            public int $x = 3;
            protected string $secret = 'internal';
            private string $alsoSecret = 'internal';
        });

        self::assertSame(['name' => 'Alice', 'x' => 3], $enc);
    }

    public function testAnEnumRecordsAsItsValue(): void
    {
        self::assertSame('read', Serial::toJsonable(Mode::Read));
        self::assertSame('write', Serial::toJsonable(Mode::Write));
    }

    public function testASnapshotRecordsOnlyIdentityExistenceAndData(): void
    {
        $s = new Snapshot('u1', true, ['name' => 'Alice']);
        self::assertSame(
            ['id' => 'u1', 'exists' => true, 'data' => ['name' => 'Alice']],
            Serial::snapshotJsonable($s)
        );
        // A snapshot that does not exist has no data to record.
        self::assertNull(Serial::snapshotJsonable(Snapshot::missing('gone'))['data']);
    }

    // --- redaction -----------------------------------------------------------------------

    public function testLayerOneMasksByFieldNameWhereverItSits(): void
    {
        $v = ['a' => ['b' => ['password' => 's3cret', 'keep' => 'yes']]];
        $out = Serial::redactJsonable($v, ['password' => null]);
        self::assertSame(Serial::REDACTED, $out['a']['b']['password']);
        self::assertSame('yes', $out['a']['b']['keep']);
    }

    public function testLayerTwoSweepsEveryLeafStringIncludingPositionalArgsAndProse(): void
    {
        $scrub = static fn (string $s): string => (string) preg_replace('/sk-live-[A-Za-z0-9]+/', '[KEY]', $s);
        $v = [
            'args' => ['sk-live-abc123'],                                  // positional, unnamed
            'body' => 'we emailed sk-live-abc123 to the user, sorry',      // mid-sentence
            'cache' => ['session:sk-live-abc123' => 'x'],                  // baked into a key
        ];
        $out = Serial::redactJsonable($v, [], $scrub);
        self::assertSame('[KEY]', $out['args'][0]);
        self::assertStringNotContainsString('sk-live-abc123', $out['body']);
        // Object KEYS are deliberately NOT swept, so tapes stay comparable across runtimes.
        self::assertArrayHasKey('session:sk-live-abc123', $out['cache']);
    }

    public function testAFieldRulesOwnOutputAlsoMeetsTheSweep(): void
    {
        $scrub = static fn (string $s): string => str_replace('secret', '[X]', $s);
        $rule = static fn (mixed $v): string => 'still-a-secret';
        $out = Serial::redactJsonable(['token' => 'abc'], ['token' => $rule], $scrub);
        self::assertStringNotContainsString('secret', $out['token']);
    }

    public function testARuleThatThrowsDegradesToRedactedRatherThanLeaking(): void
    {
        $boom = static function (mixed $v): mixed {
            throw new \RuntimeException('nope');
        };
        $out = Serial::redactJsonable(['token' => 'abc'], ['token' => $boom]);
        self::assertSame(Serial::REDACTED, $out['token']);

        $badScrub = static function (string $s): string {
            throw new \RuntimeException('nope');
        };
        self::assertSame(Serial::REDACTED, Serial::redactJsonable('abc', [], $badScrub));
    }

    public function testAMaskThatMatchesItsOwnPatternIsRefusedAtDeclarationTime(): void
    {
        $this->expectExceptionMessageMatches('/idempotent/');
        (new Boundary())->scrubbing('/[A-Z]+/', 'REDACTED');
    }

    public function testScrubbingIsIdempotent(): void
    {
        $b = (new Boundary())->scrubbing('/sk-live-[A-Za-z0-9]+/');
        $once = ($b->scrub)('key sk-live-abc');
        self::assertSame($once, ($b->scrub)($once));
    }

    public function testScrubbingStacksSoEachSecretShapeGetsItsOwnLine(): void
    {
        $b = (new Boundary())
            ->scrubbing('/sk-live-[A-Za-z0-9]+/', '[KEY]')
            ->scrubbing('/\d{16}/', '[CARD]');
        $out = ($b->scrub)('sk-live-abc paid with 1234567812345678');
        self::assertSame('[KEY] paid with [CARD]', $out);
    }

    public function testForbiddenHitReturnsThePatternNeverTheMatch(): void
    {
        $hit = Serial::forbiddenHit('token sk-live-abcdef here', ['/sk-live-[a-z]+/']);
        self::assertSame('/sk-live-[a-z]+/', $hit);
        self::assertNull(Serial::forbiddenHit('nothing to see', ['/sk-live-[a-z]+/']));
    }

    // --- JSON ----------------------------------------------------------------------------

    public function testIntegersAndFloatsAreDistinguishedOnTheWayIn(): void
    {
        self::assertIsInt(Json::decode('1'));
        self::assertIsFloat(Json::decode('1.0'));
        self::assertSame('1.0', Json::encode(1.0));
        self::assertSame('1', Json::encode(1));
    }

    public function testThisPhpRoundTripsFloatsExactly(): void
    {
        self::assertTrue(
            Json::roundTripsFloats(),
            'serialize_precision is set such that doubles no longer round-trip; tapes written '
            . 'here would stop comparing equal to the ones other runtimes write'
        );
    }

    public function testCanonicalFormMakesThirtyEqualThirtyPointZeroAndIgnoresKeyOrder(): void
    {
        self::assertTrue(Json::equal(30, 30.0));
        self::assertTrue(Json::equal(['a' => 1, 'b' => 2], ['b' => 2, 'a' => 1]));
        self::assertFalse(Json::equal(['a' => 1], ['a' => 2]));
    }

    public function testARoundTripSurvivesEscapesAndUnicode(): void
    {
        $s = "line\nbreak \"quoted\" \\ backslash — é 日本";
        self::assertSame($s, Json::decode(Json::encode($s)));
    }

    // --- traced values -------------------------------------------------------------------

    public function testALongStringKeepsItsTrueLengthAfterTruncation(): void
    {
        $long = str_repeat('x', Serial::TRACE_MAX_CHARS + 50);
        $enc = Serial::traceJsonable($long);
        self::assertSame(Serial::TRACE_MAX_CHARS + 50, $enc['__str__']['len']);
        self::assertSame(Serial::TRACE_MAX_CHARS + 50, Serial::lengthOf($enc));
        self::assertCount(Serial::TRACE_MAX_CHARS + 50, Serial::fromTraceJsonable($enc));
    }

    public function testALongSequenceKeepsItsTrueLengthAfterTruncation(): void
    {
        $big = range(1, Serial::TRACE_MAX_ITEMS + 25);
        $enc = Serial::traceJsonable($big);
        self::assertSame(Serial::TRACE_MAX_ITEMS + 25, $enc['__seq__']['len']);
        $revived = Serial::fromTraceJsonable($enc);
        self::assertCount(Serial::TRACE_MAX_ITEMS + 25, $revived);
        self::assertSame(Serial::TRACE_MAX_ITEMS, $revived->traced());
    }

    /** A user array shaped like a marker must revive as itself, not as recorder metadata. */
    public function testAUserArrayThatLooksLikeAMarkerIsEscaped(): void
    {
        $enc = Serial::traceJsonable(['__dt__' => 'not really a date']);
        self::assertArrayHasKey('__esc__', $enc);
        self::assertSame(['__dt__' => 'not really a date'], Serial::fromTraceJsonable($enc));
    }
}
