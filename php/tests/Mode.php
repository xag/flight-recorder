<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

/** A backed enum, so the codec's enum handling has something to encode. */
enum Mode: string
{
    case Read = 'read';
    case Write = 'write';
}
