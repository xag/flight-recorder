<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use PHPUnit\Framework\TestCase;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\Recording;
use Xag\FlightRecorder\Replay;

/** One row of the fake store, so replay has a declared type to fit an answer back into. */
final class Row
{
    public function __construct(public readonly string $name, public readonly int $x)
    {
    }
}

/** The real client. It counts what it was actually asked to do. */
final class RealStore
{
    public int $reads = 0;
    public int $writes = 0;

    public function read(string $key): Row
    {
        $this->reads++;
        return new Row('Alice', 3);
    }

    public function write(string $key, string $value): string
    {
        $this->writes++;
        return 'OK';
    }

    public function untouched(string $key): string
    {
        return "not recorded: $key";
    }
}

/** Wrapping a client object: the boundary is the object, as it is in Node, .NET, Go and Java. */
final class WrapTest extends TestCase
{
    use TempDir;

    public function testAWrappedCallIsForwardedToTheRealThingAndWrittenDown(): void
    {
        $real = new RealStore();
        $kv = Recorder::wrapAs('kv', $real, 'read', 'write');

        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('load', ['key' => 'alice'], static fn (): Row => $kv->read('alice'));

        self::assertSame(1, $real->reads, 'the real object must actually have been called');

        $cv = Recording::load($rec->path())->call(0);
        // Prefix-qualified, so two clients never collide on the tape.
        self::assertSame('kv.read', $cv->event('fx')['fn']);
    }

    public function testAMethodNotNamedIsInvisibleToTheRecorder(): void
    {
        $real = new RealStore();
        $kv = Recorder::wrapAs('kv', $real, 'read');

        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $out = null;
        $rec->call('other', [], static function () use ($kv, &$out): string {
            $out = $kv->untouched('x');
            return 'done';
        });

        self::assertSame('not recorded: x', $out);
        self::assertNull(Recording::load($rec->path())->call(0)->event('fx'));
    }

    /** Replay must not reach the real world. */
    public function testReplayHandsBackTheDeclaredTypeNotARawArray(): void
    {
        $real = new RealStore();
        $kv = Recorder::wrapAs('kv', $real, 'read', 'write');
        $tool = static fn (): Row => $kv->read('alice');

        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('load', [], $tool);
        self::assertSame(1, $real->reads);

        // A holder, not a by-reference capture: an arrow function captures by value, so `&$x`
        // inside one binds to the arrow's copy and the assignment never reaches this scope.
        $captured = new \ArrayObject();
        $body = static function () use ($tool, $captured): Row {
            $row = $tool();
            $captured['row'] = $row;
            return $row;
        };

        $r = Replay::replay(
            $rec->path(),
            0,
            static fn (): callable => $body,
            Toy::plainBoundary()
        );

        self::assertNull($r->divergence, (string) $r);
        self::assertSame(1, $real->reads, 'replay must not reach the real world');
        self::assertInstanceOf(Row::class, $captured['row'], 'the declared type must survive the tape');
        self::assertSame('Alice', $captured['row']->name);
        self::assertSame(3, $captured['row']->x);
    }

    /** Replaying a run must not charge the card twice. */
    public function testAWriteIsRecordedAndNotReExecutedOnReplay(): void
    {
        $real = new RealStore();
        $kv = Recorder::wrapAs('kv', $real, 'read', 'write');
        $tool = static fn (): string => $kv->write('k', 'v');

        $rec = Recorder::open($this->tempDir(), Toy::plainBoundary());
        $rec->call('save', [], $tool);
        self::assertSame(1, $real->writes);

        $r = Replay::replay($rec->path(), 0, static fn (): callable => $tool, Toy::plainBoundary());
        self::assertTrue($r->ok(), (string) $r);
        self::assertSame(1, $real->writes, 'the write must not have run again');
    }

    public function testUnwrapReachesTheRealObjectForCodeThatNeedsTheDeclaredType(): void
    {
        $real = new RealStore();
        $kv = Recorder::wrapAs('kv', $real, 'read');
        self::assertSame($real, $kv->unwrap());
    }
}
