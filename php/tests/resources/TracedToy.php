<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Traced;

/**
 * The subject of the tracing tests.
 *
 * Deliberately NOT autoloadable: the namespace `Xag\FlightRecorder\Traced` is mapped by no PSR-4
 * rule in composer.json, so nothing can load the original ahead of the instrumented copy — which
 * would win the class name and leave the copy unable to declare itself. Java keeps its TracedToy
 * out of the compiled test tree for exactly this reason; putting it in an unmapped namespace is
 * the same move, and it survives a case-insensitive filesystem, which a lowercase directory name
 * alone would not.
 */
final class TracedToy
{
    /**
     * A deliberate bug, of the only kind variable-level tracing exists for: one whose output is
     * entirely self-consistent.
     *
     * The percentage is computed over the questions ANSWERED rather than the questions ASKED, so
     * a candidate who skipped half the paper scores as though they had not. Every plausible
     * claim about the result holds — it is an integer, it is between 0 and 100, it rises when
     * more answers are right. The claim that catches it is about `$answered`.
     *
     * @param list<int> $answers -1 means unanswered
     */
    public static function gradePercent(array $answers): int
    {
        $answered = 0;
        $correct = 0;
        foreach ($answers as $a) {
            if ($a < 0) {
                continue;
            }
            $answered++;
            if ($a === 1) {
                $correct++;
            }
        }
        $pct = intdiv($correct * 100, $answered);
        return $pct;
    }

    /** Holds a credential in two locals, so a trace can be checked to carry its mask and not it. */
    public static function signedHeader(string $token): string
    {
        $secret = $token;
        $header = 'Bearer ' . $secret;
        return $header;
    }

    /**
     * Holds the closest PHP comes to Python's `os` module in a frame: handles onto process-wide
     * state that are not themselves data. None of them may be opened by the encoder.
     */
    public static function holdsProcessHandles(): int
    {
        $reader = static fn (): array => getenv();
        $wrapped = new \ArrayObject(getenv());
        $iterator = new \ArrayIterator($_SERVER);
        $reflected = new \ReflectionFunction('getenv');
        $named = 'getenv';
        return count($wrapped) > 0 ? 1 : 0;
    }

    /** A function that throws, so the trace can be checked to carry the state up to the throw. */
    public static function divide(int $a, int $b): int
    {
        $scaled = $a * 10;
        $out = intdiv($scaled, $b);
        return $out;
    }
}
