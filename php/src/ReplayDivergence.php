<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The code asked the world a different question than the recording holds. **The code changed.**
 *
 * This is the finding, not a fault in the library: the first divergence is precisely where
 * behaviour changed between the recording and now.
 */
final class ReplayDivergence extends \RuntimeException
{
}
