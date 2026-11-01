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

    // --- a pair inside a JSON string, and unquoted assignments ------------------------------

    private const KEYS = ['ALPACA_API_KEY_ID', 'api_token', 'Authorization', 'password', 'session', 'key',
        'token_env', 'auth', 'author', 'keywords', 'command', 'url', 'DB'];

    private const EXEMPT = ['key', 'token_env', 'auth'];

    /** @return list<string> */
    private static function values(): array
    {
        return ['PKRAW0001', "s3cr\"et\\x", 'Bearer abc123def456', self::sk(), 'ghp_' . 'abcdefghijklmnopqrstuvwxyz0123',
            'AKIA' . 'ABCDEFGHIJKLMNOP', 'postgres://u:pw1234@h/db', 'https://x.io/mcp?api_key=abc123&x=1',
            '${MY_TOKEN}', '{env:MY_TOKEN}', '', 'mcpServers', 'use a Bearer token', 'é', '<secret:0123abcd>'];
    }

    private static function json(mixed $v, bool $pretty = false): string
    {
        $flags = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR;
        return json_encode($v, $flags | ($pretty ? JSON_PRETTY_PRINT : 0));
    }

    /** A text held inside `$depth` JSON strings, as a transcript holds a tool result. */
    private static function nest(string $text, int $depth): string
    {
        for ($i = 0; $i < $depth; $i++) {
            $text = self::json($text);
        }
        return $text;
    }

    public function testAPairInsideAJsonStringMasksAsItsParseDoes(): void
    {
        $nestable = array_filter(self::values(), static fn (string $v): bool => strpbrk($v, "\"\\\n") === false);
        foreach ([[], self::EXEMPT] as $exempt) {
            foreach (self::KEYS as $key) {
                foreach ($nestable as $value) {
                    $doc = (object) ['servers' => (object) ['x' => (object) [$key => $value]]];
                    $inners = [self::json($doc, true), self::json($key) . ' = ' . self::json($value) . "\n"];
                    foreach ($inners as $inner) {
                        foreach ([1, 2, 3] as $depth) {
                            $outer = self::nest($inner, $depth);
                            $masked = Scrubs::credentials($outer, $exempt);
                            self::assertSame(
                                Scrubs::credentials(self::nest($inner, $depth - 1), $exempt),
                                json_decode($masked, false, 512, JSON_THROW_ON_ERROR),
                                $outer
                            );
                            self::assertSame($masked, Scrubs::credentials($masked, $exempt));
                        }
                    }
                }
            }
        }
    }

    public function testTheTranscriptRowThatLeakedIsMasked(): void
    {
        $keyValue = 'PKRAW' . '0001TESTVALUE';
        $traceLine = self::json(['e' => 'L', 'd' => ['env' => ['ALPACA_API_SECRET_KEY' => $keyValue, 'PATH' => '/usr/bin']]]);
        $row = self::json(['type' => 'user', 'message' => ['content' => [
            ['type' => 'tool_result', 'tool_use_id' => 't1', 'content' => $traceLine]]]]);
        foreach ([$row, self::json($row)] as $text) {
            $masked = Scrubs::credentials($text);
            self::assertStringNotContainsString($keyValue, $masked);
            self::assertStringContainsString('/usr/bin', $masked);
            self::assertSame($masked, Scrubs::credentials($masked));
        }
    }

    public function testAnAssignmentMasksItsValueWhenTheNameHoldsACredential(): void
    {
        $assignable = array_filter(
            self::values(),
            static fn (string $v): bool => $v !== '' && strpbrk($v, " \t\r\n\"'`\\&;|<>,)}]") === false && $v[0] !== '='
        );
        $forms = [
            static fn (string $k, string $v): string => "$k=$v",
            static fn (string $k, string $v): string => "export $k=$v",
            static fn (string $k, string $v): string => "set $k=$v",
            static fn (string $k, string $v): string => "\$env:$k = $v",
            static fn (string $k, string $v): string => "$k=\"$v\"",
            static fn (string $k, string $v): string => "$k='$v'",
            static fn (string $k, string $v): string => "\$env:$k = \"$v\"",
            static fn (string $k, string $v): string => "--$k=$v",
        ];
        $kept = static fn (string $v): bool => $v === ''
            || preg_match('~\A(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%|\$(?:env:)?\w+)\z~', $v) === 1
            || preg_match('~\A<secret:[0-9a-f]{8}>\z~', $v) === 1;
        $masked = 0;
        foreach (self::KEYS as $key) {
            foreach ($assignable as $value) {
                foreach ($forms as $i => $form) {
                    $text = $form($key, $value) . ' # done';
                    $holds = Scrubs::secretField($key) && !$kept($value);
                    $expect = $holds ? $form($key, Scrubs::secret($value)) : $form($key, Scrubs::credentials($value));
                    $masked += (int) $holds;
                    self::assertSame($expect . ' # done', Scrubs::credentials($text), "form $i: $text");
                }
            }
        }
        self::assertGreaterThan(0, $masked);
    }

    public function testAComparisonIsNotAnAssignment(): void
    {
        foreach (['if token == other:', 'token != x', 'password==hash(p)', 'token<=3'] as $text) {
            self::assertSame($text, Scrubs::credentials($text));
        }
    }

    public function testAVariableReferenceIsKeptUnderACredentialName(): void
    {
        foreach (['TOKEN=$MY_TOKEN', 'TOKEN=$env:MY_TOKEN', '"token": "$MY_TOKEN"'] as $text) {
            self::assertSame($text, Scrubs::credentials($text));
        }
    }

    public function testAReferenceOpeningAnUnquotedValueIsKept(): void
    {
        foreach (['TOKEN=${MY_TOKEN}', 'TOKEN={env:MY_TOKEN}', 'TOKEN=%MY_TOKEN%', 'TOKEN=$MY_TOKEN'] as $text) {
            self::assertSame($text, Scrubs::credentials($text));
        }
    }

    public function testTheScrubIsANoOpOnItsOwnOutput(): void
    {
        // Texts where a pass that did not see the whole picture left work for a second pass.
        $akia = 'AKIA' . 'ABCDEFGHIJKLMNOP';
        $texts = [
            "'$akia=Bearer  = Bearer ,",            // a shape masked later freed an assignment
            "token = \$env:$akia" . '\\"KEY',       // a kept reference had a shape masked inside
            '{"token": "$' . $akia . '"}',
            '{"token": "${' . $akia . '}"}',
            $akia . ' = "api_key": "v1"',            // a pair left as it is swallowed the next pair's key
            'token="token": "{"x"',                   // a masked value read back as a key holds "secret"
            "\$token='pw' = \"api_key\"Bearer",
            // a later pass changed how an earlier one read the text (the passes run to a fixed point)
            $akia . ':""key":"y"',
            'token=v:"auth:"y"',
            'token=-=""KEY":"1"',
            'session=d:\\"passw:\\"y\\"',
            "token=_x=\"token=\"token\": \"\t)&{",
            'token="token": "_;x=" token="token": "0123456789',
        ];
        foreach ($texts as $text) {
            $once = Scrubs::credentials($text);
            self::assertSame($once, Scrubs::credentials($once), $text);
            self::assertStringNotContainsString($akia, $once);
        }
    }

    public function testTheScrubIsAFixedPointOverDenseFragments(): void
    {
        // The fragments the differential fuzzing of the ports used. PHP's own seeded generator,
        // so the texts differ from Python's; the property is the same.
        $fragments = ['\\', '\\\\', '\\"', '\\\\\\"', '"', '""', "'", '`', '$', '$env:',
            '${X}', '%X%', '--', '==', '=', ' = ', ':', ': ', 'token', 'secretBearer', 'api_key',
            'password', 'a.b.', 'KEY', 'v1', 'AKIA' . 'ABCDEFGHIJKLMNOP', 'Bearer ', 'é', "\n", ' ', "\t", '<', '>',
            '&', '?', '://', '@', '{', '}', ')', ',', ';', '<secret:0123abcd>', '"api_key"', '${', 'a=',
            'token=', "'pw'", 'x = ', '": "', '"token": "', 'x="', 'auth:', 'passw:'];
        mt_srand(17);
        $last = count($fragments) - 1;
        for ($i = 0; $i < 20000; $i++) {
            $text = '';
            for ($n = mt_rand(1, 16); $n > 0; $n--) {
                $text .= $fragments[mt_rand(0, $last)];
            }
            $exempt = $i % 2 === 1 ? ['password'] : [];
            $once = Scrubs::credentials($text, $exempt);
            self::assertSame($once, Scrubs::credentials($once, $exempt), $text);
        }
    }

    public function testAnUnchangedAssignmentGivesItsValueBackToTheScan(): void
    {
        self::assertSame('a=token=' . Scrubs::secret('abc123'), Scrubs::credentials('a=token=abc123'));
    }

    public function testAPairLeftAsItIsGivesBackAValueThatOpensAPair(): void
    {
        self::assertSame(
            'x = "api_key": "' . Scrubs::secret('v1') . '"',
            Scrubs::credentials('x = "api_key": "v1"')
        );
        $e = '\\"';
        self::assertStringNotContainsString('v1', Scrubs::credentials("{$e}x{$e}: {$e}api_key{$e}: {$e}v1{$e}"));
    }

    public function testALongStringOfEscapedJsonScrubsInLinearTime(): void
    {
        // A transcript is one long string of escaped JSON. When every escaped quote could open a
        // quoted token running to the string's end, the pass was quadratic, and PCRE could give
        // up on its backtracking limit (which this scrub reports as a failure, masking the value).
        // 200,000 characters, not Python's 50,000: PCRE's JIT runs the quadratic scan fast enough
        // that 50,000 took 0.5 s, under the bound; at four times the length it takes seconds.
        mt_srand(3);
        $alphabet = ['"', 'a', 'b', ' ', ':'];
        $inner = '';
        for ($i = 0; $i < 200000; $i++) {
            $inner .= $alphabet[mt_rand(0, 4)];
        }
        $once = self::json($inner);
        foreach ([$once, self::json($once)] as $text) {
            $t = microtime(true);
            Scrubs::credentials($text);
            self::assertLessThan(1.0, microtime(true) - $t);
        }
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
