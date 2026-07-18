<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

use Xag\FlightRecorder\Boundary;
use Xag\FlightRecorder\Recorder;
use Xag\FlightRecorder\ReplayedEffectError;
use Xag\FlightRecorder\Snapshot;

/**
 * The shared scenario.
 *
 * All six runtimes ship this same shape, so the six fixtures tell the same story. That is what
 * makes the fixture sweep meaningful: a reader that can recover one runtime's account must recover
 * every runtime's. It is CHECKED, not asserted — `RecordReplayTest` renders the other five
 * runtimes' tapes and compares them character for character, so a scenario that drifts to suit one
 * local test fails a build that is not its own.
 */
final class Toy
{
    // --- the outside world ---------------------------------------------------------------

    public static function storeGet(string $key): array
    {
        return Recorder::effect('store.get', [$key], static fn (): array => ['name' => 'Alice', 'x' => 3]);
    }

    public static function storeSet(string $key, mixed $value): string
    {
        return Recorder::effect('store.set', [$key, $value], static fn (): string => 'OK');
    }

    public static function storeBoom(string $key): mixed
    {
        return Recorder::effect('store.boom', [$key], static function () use ($key): mixed {
            throw new ToyError("no such key: $key", 42);
        });
    }

    // --- the tools -----------------------------------------------------------------------

    /** The rich basic scenario: an effect, a chained read, all four random shapes, both clocks, a write. */
    public static function greet(array $kwargs): array
    {
        $user = (string) ($kwargs['user'] ?? '');
        $row = self::storeGet($user);

        Recorder::query('stream', 'collection("users").where("x", ">", 0)', static fn (): array => [
            new Snapshot('0', true, ['name' => 'alpha', 'x' => 1]),
            new Snapshot('1', true, ['name' => 'beta', 'x' => 2]),
        ]);

        Recorder::sampleIndices(3, 2);
        Recorder::randBytes(4);
        Recorder::randFloat();
        Recorder::randInt(100);
        $at = Recorder::now();
        Recorder::perf();

        Recorder::exec('set', "store.set(greeted:$user)", [['at' => $at]], static function (): void {
        });

        return ['name' => $row['name']];
    }

    /** A raising effect produces both an `fx.err` and a non-null `call.error`. */
    public static function explode(array $kwargs): mixed
    {
        return self::storeBoom((string) ($kwargs['user'] ?? ''));
    }

    /** The universal `enrol` scenario, identical across all runtimes. */
    public static function enrol(array $kwargs): array
    {
        $user = (string) ($kwargs['user'] ?? '');
        // Outside the span on purpose: it belongs to the call, not to the act.
        $started = Recorder::now();

        return Recorder::span(
            'enrol',
            ['user' => $user, 'started' => $started, 'password' => $kwargs['password'] ?? null],
            static function () use ($user): array {
                // A chained read, not an effect: the canonical scenario puts a `db` event
                // inside a span, which is the one enclosure a reader most wants to see and
                // the one an `fx`-only span never demonstrates.
                $snap = Recorder::span('load_corpus', [], static fn (): Snapshot => Recorder::queryOne(
                    'get',
                    "collection(\"users\").document(\"$user\")",
                    static fn (): Snapshot => new Snapshot($user, true, ['name' => 'Alice', 'x' => 3])
                ));
                Recorder::note('corpus_read', ['found' => $snap->exists]);

                try {
                    Recorder::span('register', ['password' => 'hunter2'], static function () use ($user): void {
                        Recorder::effect(
                            'store.set',
                            ["user:$user", ['password' => 'hunter2']],
                            static fn (): string => 'OK'
                        );
                        self::storeBoom($user);
                    });
                } catch (ToyError | ReplayedEffectError $e) {
                    // Two arms: the real type when recording (and when a reviver is declared),
                    // the stand-in when replaying a tape whose boundary declares none.
                    Recorder::note('registration_failed', ['why' => $e->getMessage()]);
                }

                return ['user' => $user, 'name' => $snap->get('name')];
            }
        );
    }

    // --- the boundaries --------------------------------------------------------------------

    public static function plainBoundary(): Boundary
    {
        return (new Boundary())
            ->constant('toy.LIMIT', 3)
            ->maskFields('password')
            ->reviving('ToyError', static fn (array $a): ToyError => ToyError::fromArgs($a));
    }

    public static function semBoundary(): Boundary
    {
        return (new Boundary())
            ->maskFields('password')
            ->reviving('ToyError', static fn (array $a): ToyError => ToyError::fromArgs($a));
    }

    /** Maps a recorded tool name back to the code that implements it. */
    public static function resolver(): callable
    {
        return static function (string $fn, array $kwargs): ?callable {
            return match ($fn) {
                'greet' => static fn (): array => self::greet($kwargs),
                'explode' => static fn (): mixed => self::explode($kwargs),
                'enrol' => static fn (): array => self::enrol($kwargs),
                default => null,
            };
        };
    }
}
