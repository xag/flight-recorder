<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Boundary;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Scrubs;
use Xag\FlightRecorder\Serial;
use Xag\FlightRecorder\Tracer;

/**
 * The stock credential scrub, checked byte for byte against the vectors the Python
 * implementation wrote (spec/fixtures/credentials.json), then used as what it is for: a
 * Boundary's scrub and forbid, on a recorded call and on a trace.
 */
final class ScrubsTest extends TestCase
{
    use TempDir;

    private const SUBJECT = 'Xag\\FlightRecorder\\Traced\\TracedToy';

    /** Built by concatenation, so this file is not one a scanner reads as carrying a key. */
    private static function proseKey(): string
    {
        return 'sk-' . 'proj-0123456789abcdefghijklmnop';
    }

    private static function sk(): string
    {
        return 'sk-ant-' . 'api03-abcdefghijklmnopqrstu';
    }

    /** @return array{mask: string, cases: list<\stdClass>} */
    private static function vectors(): array
    {
        $path = __DIR__ . '/../../spec/fixtures/credentials.json';
        $doc = json_decode((string) file_get_contents($path), false, 512, JSON_THROW_ON_ERROR);
        return ['mask' => $doc->mask, 'cases' => $doc->cases];
    }

    public function testEveryVectorMasksToTheSameBytesAsPython(): void
    {
        $v = self::vectors();
        self::assertSame(Scrubs::MASK, '/' . $v['mask'] . '/');
        $texts = 0;
        $data = 0;
        foreach ($v['cases'] as $i => $case) {
            if (property_exists($case, 'text')) {
                self::assertSame($case->masked, Scrubs::credentials($case->text, $case->exempt), "case $i");
                $texts++;
            } else {
                self::assertEquals($case->masked_data, Scrubs::credentialsData($case->data, $case->exempt), "case $i");
                self::assertSame(
                    json_encode($case->masked_data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES),
                    json_encode(Scrubs::credentialsData($case->data, $case->exempt), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES),
                    "case $i"
                );
                $data++;
            }
        }
        self::assertGreaterThan(0, $texts);
        self::assertGreaterThan(0, $data);
    }

    public function testAMaskIsNeverMaskedAgain(): void
    {
        foreach (self::vectors()['cases'] as $i => $case) {
            if (property_exists($case, 'text')) {
                $once = Scrubs::credentials($case->text, $case->exempt);
                self::assertSame($once, Scrubs::credentials($once, $case->exempt), "case $i");
            } else {
                $once = Scrubs::credentialsData($case->data, $case->exempt);
                self::assertEquals($once, Scrubs::credentialsData($once, $case->exempt), "case $i");
            }
        }
    }

    public function testTheShapesAsForbidPassAScrubbedText(): void
    {
        // Whatever `credentials` leaves, no shape fires on, so declaring both never refuses a
        // recording the scrub already cleaned.
        $shapes = (new Boundary())->forbidden(...Scrubs::CREDENTIAL_SHAPES)->forbid;
        self::assertSame(Scrubs::CREDENTIAL_SHAPES, $shapes);
        foreach (self::vectors()['cases'] as $i => $case) {
            if (property_exists($case, 'text')) {
                self::assertNull(Serial::forbiddenHit(Scrubs::credentials($case->text), $shapes), "case $i");
                self::assertNull(Serial::forbiddenHit($case->masked, $shapes), "case $i");
            }
        }
        self::assertNotNull(Serial::forbiddenHit(json_encode(['note' => self::sk()]), $shapes));
    }

    public function testASecretIsTheFirstEightHexOfTheUtf8Sha256(): void
    {
        // Values computed by Python's hashlib over the UTF-8 bytes.
        self::assertSame('<secret:4a99557e>', Scrubs::secret('é'));
        self::assertSame('<secret:7b62ee12>', Scrubs::secret('PKRAW0001'));
    }

    public function testAnExemptedFieldIsKeptAndOnlyThatOne(): void
    {
        $exempt = ['key', 'token_env', 'auth'];
        self::assertSame(['key' => 'mcpServers'], Scrubs::credentialsData(['key' => 'mcpServers'], $exempt));
        self::assertNotSame(['key' => 'mcpServers'], Scrubs::credentialsData(['key' => 'mcpServers']));
        // an exemption keeps the field, not a credential shape inside it
        self::assertStringNotContainsString(
            self::sk(),
            (string) json_encode(Scrubs::credentialsData(['key' => self::sk()], $exempt))
        );
    }

    public function testAnEmptyMapStaysAMap(): void
    {
        $in = json_decode('{"a": {}, "b": [], "token": "abc", "n": 1.5, "t": true, "z": null}');
        self::assertSame(
            '{"a":{},"b":[],"token":"' . Scrubs::secret('abc') . '","n":1.5,"t":true,"z":null}',
            json_encode(Scrubs::credentialsData($in))
        );
    }

    // --- as a Boundary declares it: tape and trace ------------------------------------------

    private static function boundary(): Boundary
    {
        return (new Boundary())
            ->scrubbingWith(Scrubs::credentials(...))
            ->forbidden(...Scrubs::CREDENTIAL_SHAPES);
    }

    public function testABoundaryDeclaringTheScrubWritesNoCredentialOnTheTape(): void
    {
        $rec = Recorder::open($this->tempDir(), self::boundary());
        $key = self::proseKey();
        $out = $rec->call(
            'ask',
            ['prompt' => "use $key for this", 'headers' => ['Authorization' => "Bearer $key"]],
            static fn (): string => "echo $key"
        );

        self::assertSame("echo $key", $out, 'the code still sees the real value');
        $text = (string) file_get_contents((string) $rec->path());
        self::assertStringNotContainsString($key, $text);
        self::assertMatchesRegularExpression(Scrubs::MASK, $text);
    }

    public function testABoundaryDeclaringTheScrubWritesNoCredentialInTheTrace(): void
    {
        $key = self::proseKey();
        $run = Tracer::runWith([__DIR__ . '/resources/TracedToy.php'], self::SUBJECT, 'signedHeader', self::boundary(), $key);

        self::assertSame("Bearer $key", $run->result);
        $text = $run->trace->toJsonl();
        self::assertStringNotContainsString($key, $text);
        self::assertStringContainsString(Scrubs::secret($key), $text);
    }
}
