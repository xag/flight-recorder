<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Variable-level tracing: rewrite the named sources, load the rewritten copy, run an entry
 * point with the tracer armed.
 *
 *     $run = Tracer::run(['src/Tools.php'], App\Tools::class, 'studyStatus', $user);
 *     $run->trace->last('level');      // what it actually was, not what you infer it was
 *     echo $run->trace->render('deck');
 *
 * ## Why the rewritten copy is still the same execution
 *
 * The code under replay reaches the world *only* through the boundary, and the boundary lives
 * in this package, which the rewritten sources reference and therefore **share**. Same statics,
 * same hook, same answers off the tape. Nothing about the instrumented copy is a simulation.
 *
 * ## The one constraint
 *
 * A rewritten class is loaded under its own name, so the original must not have been loaded
 * first — PHP has no class-loader isolation to hide a second definition behind. Keep traced
 * sources out of the autoload path (the suite keeps its subject under `tests/resources/`, as
 * Java keeps its own outside the compiled test tree), or trace in a process that has not yet
 * touched them. `run()` says so plainly rather than failing with a redeclaration fatal, which
 * is not a recoverable error and would take the whole process down.
 */
final class Tracer
{
    /** @var array<string, true> classes this process loaded in instrumented form */
    private static array $traced = [];

    /** The instrumented form of one file, for reading. Loads nothing. */
    public static function preview(string $path): string
    {
        return Instrument::rewriteFile($path);
    }

    /**
     * Rewrite, load, and run — returning both the value and the trace.
     *
     * @param list<string> $sourcePaths
     */
    public static function run(
        array $sourcePaths,
        string $class,
        string $method,
        mixed ...$args,
    ): TraceRun {
        return self::runWith($sourcePaths, $class, $method, null, ...$args);
    }

    /**
     * As `run()`, with a boundary whose `forbid` patterns guard the trace.
     *
     * @param list<string> $sourcePaths
     */
    public static function runWith(
        array $sourcePaths,
        string $class,
        string $method,
        ?Boundary $boundary,
        mixed ...$args,
    ): TraceRun {
        if (class_exists($class, false) && !isset(self::$traced[$class])) {
            throw new \RuntimeException(
                "$class is already loaded from its original source, so the instrumented copy "
                . 'cannot take its name. Keep a traced source out of the autoload path, or trace '
                . 'it from a process that has not yet referenced it.'
            );
        }

        // A second run of an already-instrumented class re-uses the copy in place. The
        // alternative — refusing — would mean one traced function per process, and a suite with
        // ten tracing tests would need ten processes to say anything.
        if (!isset(self::$traced[$class])) {
            self::load($sourcePaths);
            self::$traced[$class] = true;
        }

        if (!class_exists($class, false)) {
            throw new \RuntimeException(
                "the instrumented sources declared no class $class — check the class name against "
                . 'the files passed in'
            );
        }

        $sink = new TraceSink(null, $boundary);
        $prior = TraceHook::setSink($sink);
        TraceHook::reset();
        try {
            $result = $class::$method(...$args);
        } finally {
            TraceHook::setSink($prior);
        }

        $refused = $sink->refused();
        if ($refused !== null) {
            throw new ForbiddenValue($refused, 'a traced value');
        }
        return new TraceRun($result, $sink->snapshot());
    }

    /**
     * Rewrite each source into a temp file and include it.
     *
     * A temp *file* rather than `eval()`: an included file keeps a real path and real line
     * numbers, so a fatal inside instrumented code names something a reader can open. The
     * splice's own location literals still refer to the original file, which is what the trace
     * reports.
     *
     * @param list<string> $sourcePaths
     */
    private static function load(array $sourcePaths): void
    {
        $dir = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'flight-recorder-traced-' . getmypid();
        if (!is_dir($dir) && !@mkdir($dir, 0o777, true) && !is_dir($dir)) {
            throw new \RuntimeException("cannot create a directory for instrumented sources: $dir");
        }
        foreach ($sourcePaths as $path) {
            $code = Instrument::rewriteFile($path);
            $out = $dir . DIRECTORY_SEPARATOR . basename($path);
            file_put_contents($out, $code);
            require_once $out;
        }
    }
}
