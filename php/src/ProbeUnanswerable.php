<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A mutation sent execution down a path this recording cannot answer.
 *
 * **Nothing is wrong with the code**; the tape is incompletely edited. Deliberately not a
 * divergence: a divergence means the tape has an answer and it is for a different question,
 * whereas this means the tape ran out. Reporting these as the same thing is how a probe
 * session turns into a wild goose chase.
 */
final class ProbeUnanswerable extends \RuntimeException
{
}
