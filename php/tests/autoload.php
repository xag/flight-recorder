<?php

declare(strict_types=1);

/**
 * A PSR-4 autoloader for running the suite without Composer's generated one.
 *
 * The library has no runtime dependencies, so nothing but the autoloader itself needs
 * installing. This keeps `php tests/run.php` working in a checkout where `composer install` has
 * never been run — which is how the conformance sweep stays cheap to reproduce.
 */
spl_autoload_register(static function (string $class): void {
    $roots = [
        'Xag\\FlightRecorder\\Tests\\' => __DIR__ . '/',
        'Xag\\FlightRecorder\\' => __DIR__ . '/../src/',
    ];
    foreach ($roots as $prefix => $dir) {
        if (str_starts_with($class, $prefix)) {
            $rel = str_replace('\\', '/', substr($class, strlen($prefix)));
            $path = $dir . $rel . '.php';
            if (is_file($path)) {
                require $path;
                return;
            }
        }
    }
});
