<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A recording decorator around a client object.
 *
 * **This is not a mock.** Under record it calls the real object and writes down what came back;
 * under replay it serves the recorded answer without calling anything. Nothing here knows what
 * any method does — only which names are worth writing down.
 *
 * PHP reaches this more cheaply than any other runtime the library targets: `__call` catches
 * every undefined method at run time, so a wrapper needs no interface to implement (Java's
 * `reflect.Proxy` and .NET's `DispatchProxy` both do), no subclass, and no code generation. The
 * cost is that a wrapped object does not satisfy a type declaration for the class it wraps —
 * see `unwrap()`.
 */
final class Wrapped
{
    /**
     * @param list<string> $methods the method names to record; everything else passes through
     */
    public function __construct(
        private readonly string $prefix,
        private readonly object $target,
        private readonly array $methods,
    ) {
    }

    /** The real object, for the code paths that need the declared type rather than the wrapper. */
    public function unwrap(): object
    {
        return $this->target;
    }

    public function __call(string $name, array $args): mixed
    {
        if (!in_array($name, $this->methods, true)) {
            // Not named: forwarded untouched and unrecorded. A boundary records what it was
            // told crosses it, and nothing else.
            return $this->target->$name(...$args);
        }
        $answer = Recorder::effect(
            $this->prefix . '.' . $name,
            array_values($args),
            fn (): mixed => $this->target->$name(...$args)
        );
        return $this->coerce($answer, $name);
    }

    public function __get(string $name): mixed
    {
        return $this->target->$name;
    }

    public function __set(string $name, mixed $value): void
    {
        $this->target->$name = $value;
    }

    public function __isset(string $name): bool
    {
        return isset($this->target->$name);
    }

    /**
     * Fit a revived answer to the method's declared return type.
     *
     * A tape stores structure, not types: an object comes back off it as an array. Under record
     * that never shows, because the real object returned the real thing. Under replay the array
     * IS what flows — so without this step, code that declared a return type dies on a
     * TypeError the recorder itself caused, and the recorder has broken the very thing it
     * exists not to disturb.
     *
     * Best-effort by design: a poorer replay is a finding, a replay that dies inside the codec
     * is a distraction.
     */
    private function coerce(mixed $value, string $method): mixed
    {
        if (Recorder::$feed === null) {
            return $value; // recording: the real object already returned the real type
        }
        try {
            $type = (new \ReflectionMethod($this->target, $method))->getReturnType();
            if (!$type instanceof \ReflectionNamedType || $type->isBuiltin()) {
                return $value;
            }
            $class = $type->getName();
            if ($value instanceof $class) {
                return $value;
            }
            if ($class === Snapshot::class && is_array($value)) {
                return Snapshot::fromArray($value);
            }
            if (is_array($value) && class_exists($class)) {
                return self::hydrate($class, $value);
            }
        } catch (\Throwable) {
            // fall through: the value as it stands is more useful than an exception
        }
        return $value;
    }

    /** Build `$class` from an array, by constructor promotion where the names line up. */
    private static function hydrate(string $class, array $data): mixed
    {
        $ctor = (new \ReflectionClass($class))->getConstructor();
        if ($ctor === null) {
            return $data;
        }
        $argv = [];
        foreach ($ctor->getParameters() as $p) {
            if (array_key_exists($p->getName(), $data)) {
                $argv[] = $data[$p->getName()];
            } elseif ($p->isDefaultValueAvailable()) {
                $argv[] = $p->getDefaultValue();
            } else {
                return $data; // the shape does not fit; the array is the honest answer
            }
        }
        return new $class(...$argv);
    }
}
