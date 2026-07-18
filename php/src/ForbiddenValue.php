<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The recorder was about to write a secret. Nothing was written.
 *
 * This is the one recorder failure that is never swallowed. Everywhere else the recorder is
 * built never to break the app it instruments; here it must, because a warning in a production
 * log is not how anyone should discover a credential was about to be written to disk.
 *
 * Carries the PATTERN, never the match. An error message carrying the secret would defeat the
 * guard's whole purpose — it would move the credential from a tape nobody reads into a log
 * everybody does.
 */
final class ForbiddenValue extends \RuntimeException
{
    public function __construct(
        public readonly string $pattern,
        public readonly string $what = 'the tape',
    ) {
        parent::__construct(
            "$what matches a forbidden pattern (\"$pattern\") after redaction — nothing was "
            . 'written; name the field in Boundary::redacting(), or widen a rule that stopped '
            . 'matching, and record again'
        );
    }
}
