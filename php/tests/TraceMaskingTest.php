<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Boundary;
use Xag\FlightRecorder\Instrument;
use Xag\FlightRecorder\Serial;
use Xag\FlightRecorder\Trace;
use Xag\FlightRecorder\TraceHook;
use Xag\FlightRecorder\Tracer;
use Xag\FlightRecorder\TraceSink;

/**
 * The trace is masked as the tape is: the boundary's field rules and value scrub rewrite each
 * event's payload (`args`, `d`, `v`) before the tripwire reads the line, and never its envelope.
 *
 * And a traced frame records what the code holds, never what a handle in it could reach: a
 * closure, a reflection, an object wrapping the environment are named, not opened.
 */
final class TraceMaskingTest extends TestCase
{
    use TempDir;

    private const SUBJECT = 'Xag\\FlightRecorder\\Traced\\TracedToy';
    private const KEY = 'sk-live-0123456789abcdef';
    private const KEY_SHAPE = '/sk-live-[A-Za-z0-9]+/';
    private const ENV_NAME = 'FR_TRACE_SENTINEL';
    private const ENV_SENTINEL = 'fr-env-sentinel-7c1e9a';

    private function source(): string
    {
        return __DIR__ . '/resources/TracedToy.php';
    }

    private function signed(Boundary $boundary): \Xag\FlightRecorder\TraceRun
    {
        return Tracer::runWith([$this->source()], self::SUBJECT, 'signedHeader', $boundary, self::KEY);
    }

    private static function maskKey(): Boundary
    {
        return (new Boundary())->scrubbingWith(
            static fn (string $s): string => str_replace(self::KEY, '[KEY]', $s)
        );
    }

    public function testTheScrubMasksTheTraceAsItMasksTheTape(): void
    {
        $run = $this->signed(self::maskKey());

        self::assertSame('Bearer ' . self::KEY, $run->result, 'the code still sees the real value');
        $text = $run->trace->toJsonl();
        self::assertStringContainsString('[KEY]', $text);
        self::assertStringNotContainsString(self::KEY, $text);
    }

    public function testATraceFileHoldsOnlyTheMask(): void
    {
        $path = $this->tempDir() . '/trace.jsonl';
        $sink = new TraceSink($path, self::maskKey());
        $prior = TraceHook::setSink($sink);
        try {
            $f = TraceHook::enter('Toy.save', 'Toy.php:1', ['token' => self::KEY]);
            TraceHook::line($f, 'Toy.save', 'Toy.php:2', ['token' => self::KEY, 'h' => 'x ' . self::KEY]);
            TraceHook::line($f, 'Toy.save', 'Toy.php:3', ['token' => self::KEY, 'h' => 'x ' . self::KEY]);
            TraceHook::returned($f, 'Toy.save', 'Toy.php:3', self::KEY);
            TraceHook::raise($f, 'Toy.save', 'Toy.php:3', new \RuntimeException('bad ' . self::KEY));
        } finally {
            TraceHook::setSink($prior);
        }

        $text = (string) file_get_contents($path);
        self::assertSame(5, count(array_filter(explode("\n", $text))), 'H, C, L, R, X');
        self::assertStringContainsString('[KEY]', $text);
        self::assertStringNotContainsString(self::KEY, $text);
        self::assertStringNotContainsString(self::KEY, $sink->snapshot()->toJsonl(),
            'the in-memory buffer is masked too');
    }

    public function testTheTripwireReadsTheMaskedLine(): void
    {
        // The scrub has already taken the key off the line, so the shape has nothing to fire on.
        $run = $this->signed(self::maskKey()->forbidden(self::KEY_SHAPE));

        self::assertStringContainsString('[KEY]', $run->trace->toJsonl());
    }

    public function testARedactedFieldNameMasksTheLocalOfThatName(): void
    {
        // A trace's payload is keyed by local name as a tape's kwargs are keyed by parameter
        // name, so a field rule reaches a local exactly as it reaches an argument.
        $run = $this->signed((new Boundary())->maskFields('secret'));

        $secrets = [];
        foreach ($run->trace->events as $ev) {
            $ev = (array) $ev;
            $d = (array) ($ev['d'] ?? []);
            if (array_key_exists('secret', $d)) {
                $secrets[] = $d['secret'];
            }
        }
        self::assertSame([Serial::REDACTED], $secrets);
        self::assertSame(self::KEY, $run->trace->first('token')?->value, 'only the named local');
    }

    public function testTheTraceEnvelopeIsNotRedacted(): void
    {
        // As on the tape, rules rewrite payloads only: a rule named like an envelope key cannot
        // take the structure of the trace apart.
        $run = $this->signed((new Boundary())->maskFields('e', 'fn', 'at')->scrubbingWith(
            static fn (string $s): string => '[ALL]'
        ));

        $kinds = [];
        foreach ($run->trace->events as $ev) {
            $ev = (array) $ev;
            $kinds[$ev['e']] = true;
            self::assertSame('TracedToy.signedHeader', $ev['fn']);
            self::assertMatchesRegularExpression('/^TracedToy\.php:\d+$/', $ev['at']);
        }
        self::assertSame(['C', 'L', 'R'], array_values(array_intersect(['C', 'L', 'R'], array_keys($kinds))));
    }

    // --- a handle in a frame is named, never opened -----------------------------------------
    //
    // Python's trace encoder once walked a local holding the `os` module into its attributes,
    // and `os.sys.modules["nt"].environ` is the whole environment. These pin that PHP's encoder
    // has no such road: it opens arrays and stdClass only, and names everything else.

    private function withSentinel(callable $body): void
    {
        $priorEnv = $_ENV[self::ENV_NAME] ?? null;
        $priorServer = $_SERVER[self::ENV_NAME] ?? null;
        putenv(self::ENV_NAME . '=' . self::ENV_SENTINEL);
        $_ENV[self::ENV_NAME] = self::ENV_SENTINEL;
        $_SERVER[self::ENV_NAME] = self::ENV_SENTINEL;
        try {
            self::assertSame(self::ENV_SENTINEL, getenv(self::ENV_NAME), 'the sentinel is set');
            $body();
        } finally {
            putenv(self::ENV_NAME);
            if ($priorEnv === null) {
                unset($_ENV[self::ENV_NAME]);
            } else {
                $_ENV[self::ENV_NAME] = $priorEnv;
            }
            if ($priorServer === null) {
                unset($_SERVER[self::ENV_NAME]);
            } else {
                $_SERVER[self::ENV_NAME] = $priorServer;
            }
        }
    }

    public function testATracedFrameHoldingProcessHandlesWritesNoEnvironment(): void
    {
        $this->withSentinel(function (): void {
            $path = $this->tempDir() . '/handles.jsonl';
            $run = Tracer::run([$this->source()], self::SUBJECT, 'holdsProcessHandles');
            file_put_contents($path, $run->trace->toJsonl());

            $text = (string) file_get_contents($path);
            self::assertStringNotContainsString(self::ENV_SENTINEL, $text);
            self::assertStringContainsString('"wrapped":{"__opaque__":"<ArrayObject>"}', $text);
            self::assertStringContainsString('"reader":{"__opaque__":"<Closure>"}', $text);
            self::assertSame('getenv', $run->trace->last('named')?->value);
        });
    }

    public function testWhatIsNotDataIsNeverWalked(): void
    {
        $this->withSentinel(function (): void {
            $public = new class {
                public array $env;

                public function __construct()
                {
                    $this->env = getenv();
                }
            };
            $values = [
                static fn (): array => getenv(),
                new \ArrayObject(getenv()),
                new \ArrayIterator($_SERVER),
                new \ReflectionFunction('getenv'),
                new \ReflectionClass(Boundary::class),
                $public,
                [$public, new \ArrayObject($_ENV)],
            ];
            foreach ($values as $i => $value) {
                $encoded = json_encode(Serial::traceJsonable($value), JSON_UNESCAPED_SLASHES);
                self::assertIsString($encoded);
                self::assertStringNotContainsString(self::ENV_SENTINEL, $encoded, "value #$i");
            }
        });
    }

    public function testFileScopeCodeIsNeverInstrumented(): void
    {
        // At file scope `get_defined_vars()` IS the global symbol table — $_SERVER, $_ENV,
        // $GLOBALS — so a splice there would trace the environment. The rewriter splices only
        // inside a function body, where it is the function's own locals.
        $src = "<?php\n\$env = getenv();\nif (true) { \$x = \$_SERVER; }\n"
            . "function f(): int { \$y = 1; return \$y; }\n\$z = f();\n";
        $out = Instrument::rewrite('top.php', $src);

        [$before, $rest] = explode('function f()', $out, 2);
        [$body, $after] = explode("\n", $rest, 2);
        self::assertStringNotContainsString('TraceHook', $before);
        self::assertStringNotContainsString('TraceHook', $after);
        self::assertStringContainsString('TraceHook::enter', $body);
    }

    public function testTraceLinesRoundTripAfterMasking(): void
    {
        // Masking rewrites values, not the shape of an event: a masked trace still parses.
        $run = $this->signed(self::maskKey());
        $back = Trace::parse($run->trace->toJsonl());
        self::assertSame('[KEY]', $back->first('token')?->value);
    }
}
