<?php

declare(strict_types=1);

namespace Xag\FlightRecorder\Tests;

/** A scratch directory that cleans up after itself. */
trait TempDir
{
    private ?string $tmp = null;

    protected function tempDir(): string
    {
        if ($this->tmp === null) {
            $this->tmp = sys_get_temp_dir() . DIRECTORY_SEPARATOR
                . 'fr-php-' . getmypid() . '-' . bin2hex(random_bytes(4));
            mkdir($this->tmp, 0o777, true);
        }
        return $this->tmp;
    }

    protected function fixturesDir(): string
    {
        return dirname(__DIR__, 2) . DIRECTORY_SEPARATOR . 'spec' . DIRECTORY_SEPARATOR . 'fixtures';
    }

    /** @return list<string> */
    protected function tapesIn(string $dir): array
    {
        return array_values(array_filter(
            glob($dir . DIRECTORY_SEPARATOR . '*.jsonl') ?: [],
            'is_file'
        ));
    }

    protected function tearDown(): void
    {
        if ($this->tmp !== null && is_dir($this->tmp)) {
            foreach (glob($this->tmp . DIRECTORY_SEPARATOR . '*') ?: [] as $f) {
                @unlink($f);
            }
            @rmdir($this->tmp);
        }
        $this->tmp = null;
    }
}
