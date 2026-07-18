<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * The source rewriter behind variable-level tracing.
 *
 * ## Why rewriting, and why PHP's own tokenizer
 *
 * Three families of approach exist, and two are refused here:
 *
 * 1. **Xdebug** gives per-line callbacks and full access to locals. It is refused for the same
 *    reason Java refused `-javaagent` and Go refused Delve: it is a compiled extension the host
 *    has to install and enable in `php.ini`, so a library that needs it to trace is a library
 *    that dictates its user's deployment. A tracer nobody can switch on is not a tracer.
 * 2. **`declare(ticks=1)` with `register_tick_function`** needs no extension, but a tick handler
 *    cannot read the locals of the frame that triggered it — PHP exposes no such API — so it can
 *    say a statement ran and nothing about what it did. That is a profiler, not a trace.
 * 3. **Chosen: rewrite the source, run the rewritten copy.** `token_get_all()` is PHP's own
 *    lexer, in core and always available, so the parse that guides the splice is the same one
 *    the engine performs. This is .NET's road (Roslyn) and Java's (`com.sun.source`) and Go's
 *    (`go/ast`) — the fourth runtime to reach variable-level tracing by rewriting rather than by
 *    debugging, which is by now the answer rather than the exception.
 *
 * ## What PHP makes easy that the others did not
 *
 * `get_defined_vars()` returns every local in scope at the point it is called. So the rewriter
 * does not track which variables exist, does not name them in the splice, and — decisively — has
 * no definite-assignment problem. Java had to approximate javac's definite-assignment rules
 * syntactically because `TraceHook.line(…, new Object[]{ x })` will not compile when `x` might be
 * unassigned; .NET had to ask Roslyn's `AnalyzeDataFlow`. Here an unassigned variable simply is
 * not in the array, and the splice is the same eleven tokens everywhere.
 *
 * ## What it splices
 *
 * A function body gains a frame, a try, and a finally; every statement that is a direct child of
 * a block inside it gains an observation; every `return` is wrapped in an identity passthrough.
 * Text is spliced at token offsets rather than re-printed, so everything the author wrote stays
 * byte for byte what it was — and the line numbers in the trace refer to the original file,
 * because the location literals are read from the ORIGINAL token stream before any edit moves
 * anything.
 */
final class Instrument
{
    private const HOOK = '\\Xag\\FlightRecorder\\TraceHook';
    private const PREFIX = '__fr';

    /** @var list<array{id: int, text: string, line: int, off: int}> */
    private array $tokens = [];

    /** @var list<array{off: int, text: string}> */
    private array $edits = [];

    private int $frames = 0;

    private function __construct(
        private readonly string $file,
        private readonly string $src,
    ) {
    }

    /** Rewrite one file's source. `$fileName` is what the trace's locations will name. */
    public static function rewrite(string $fileName, string $src): string
    {
        $it = new self($fileName, $src);
        $it->lex();
        $it->walk();
        return $it->apply();
    }

    /** Rewrite a file on disk. */
    public static function rewriteFile(string $path): string
    {
        $src = @file_get_contents($path);
        if ($src === false) {
            throw new \RuntimeException("cannot read source: $path");
        }
        return self::rewrite(basename($path), $src);
    }

    /**
     * Tokenize, carrying a line number onto every token.
     *
     * `token_get_all` reports a line only for its array tokens; a single-character token like
     * `{` or `;` arrives as a bare string with no position at all. Those are exactly the tokens
     * the frame splice hangs off, so the line is tracked here instead — otherwise every function
     * would enter and leave at line 0, and the trace would point at nothing.
     */
    private function lex(): void
    {
        $off = 0;
        $line = 1;
        foreach (token_get_all($this->src) as $t) {
            $text = is_array($t) ? $t[1] : $t;
            $line = is_array($t) ? $t[2] : $line;
            $this->tokens[] = [
                'id' => is_array($t) ? $t[0] : -1,
                'text' => $text,
                'line' => $line,
                'off' => $off,
            ];
            $off += strlen($text);
            $line += substr_count($text, "\n");
        }
    }

    private static function isSkippable(int $id): bool
    {
        return $id === T_WHITESPACE || $id === T_COMMENT || $id === T_DOC_COMMENT;
    }

    /** The next token index at or after `$i` that is not whitespace or a comment. */
    private function next(int $i): int
    {
        $n = count($this->tokens);
        while ($i < $n && self::isSkippable($this->tokens[$i]['id'])) {
            $i++;
        }
        return $i;
    }

    /**
     * The walk.
     *
     * One pass, with two stacks: `$braces` records what each open brace is (a statement block, a
     * class body, a `match`, a string interpolation) and `$scopes` records the enclosing function
     * frames. A statement observation is spliced only when the innermost brace is a statement
     * block **and** some function scope is open — which is exactly "a statement inside a
     * function", and never a property declaration, a `match` arm, or an interpolation.
     */
    private function walk(): void
    {
        $n = count($this->tokens);
        /** @var list<array{kind: string, scope: bool}> $braces */
        $braces = [];
        /** @var list<array{var: string, fn: string}> $scopes */
        $scopes = [];
        $classStack = [];

        $paren = 0;
        $stmtStart = false;
        $inCaseLabel = false;
        $pendingFn = null;   // a function whose body brace we are still looking for
        $pendingClass = null;

        for ($i = 0; $i < $n; $i++) {
            $tok = $this->tokens[$i];
            $id = $tok['id'];
            $text = $tok['text'];

            if (self::isSkippable($id)) {
                continue;
            }

            // --- structural bookkeeping -------------------------------------------------
            if ($id === -1 && ($text === '(' || $text === '[')) {
                $paren++;
                $stmtStart = false;
                continue;
            }
            if ($id === -1 && ($text === ')' || $text === ']')) {
                $paren--;
                $stmtStart = false;
                continue;
            }
            if ($id === T_ATTRIBUTE) {
                $paren++;   // #[ ... ] — brackets balance through the ']' branch above
                continue;
            }

            if ($id === T_CLASS || $id === T_TRAIT || $id === T_INTERFACE || $id === T_ENUM) {
                // `new class` is anonymous; either way the next brace is a class body.
                $j = $this->next($i + 1);
                $pendingClass = ($j < $n && $this->tokens[$j]['id'] === T_STRING)
                    ? $this->tokens[$j]['text']
                    : 'class@anonymous';
                $stmtStart = false;
                continue;
            }

            if ($id === T_FUNCTION) {
                $j = $this->next($i + 1);
                if ($j < $n && $this->tokens[$j]['id'] === -1 && $this->tokens[$j]['text'] === '&') {
                    $j = $this->next($j + 1);
                }
                $name = ($j < $n && $this->tokens[$j]['id'] === T_STRING)
                    ? $this->tokens[$j]['text']
                    : '{closure}';
                $cls = $classStack === [] ? null : $classStack[count($classStack) - 1];
                $pendingFn = ['name' => ($cls === null ? $name : "$cls.$name"), 'line' => $tok['line']];
                $stmtStart = false;
                continue;
            }
            if ($id === T_FN) {
                // An arrow function is a single expression with no block to splice into. Its
                // body is observed by the enclosing frame's next statement, which is the same
                // trade every other port makes for an unbraced body.
                $stmtStart = false;
                continue;
            }

            if ($id === T_MATCH) {
                // `match` uses braces but holds arms, not statements. Splicing there is a syntax
                // error, so the block is entered as a non-statement brace and left alone.
                $this->pendingMatch = true;
                $stmtStart = false;
                continue;
            }

            if ($id === T_CURLY_OPEN || $id === T_DOLLAR_OPEN_CURLY_BRACES) {
                $braces[] = ['kind' => 'interp', 'scope' => false];
                $stmtStart = false;
                continue;
            }

            if ($id === -1 && $text === '{') {
                if ($pendingFn !== null && $paren === 0) {
                    $this->frames++;
                    $var = '$' . self::PREFIX . $this->frames;
                    $scopes[] = ['var' => $var, 'fn' => $pendingFn['name']];
                    $braces[] = ['kind' => 'stmt', 'scope' => true];
                    $this->openFrame($i, $var, $pendingFn['name'], $pendingFn['line']);
                    $pendingFn = null;
                    $stmtStart = true;
                    continue;
                }
                if ($pendingClass !== null) {
                    $classStack[] = $pendingClass;
                    $braces[] = ['kind' => 'class', 'scope' => false];
                    $pendingClass = null;
                    $stmtStart = false;
                    continue;
                }
                if ($this->pendingMatch) {
                    $braces[] = ['kind' => 'match', 'scope' => false];
                    $this->pendingMatch = false;
                    $stmtStart = false;
                    continue;
                }
                $braces[] = ['kind' => 'stmt', 'scope' => false];
                $stmtStart = true;
                continue;
            }

            if ($id === -1 && $text === '}') {
                $b = array_pop($braces);
                if ($b !== null && $b['scope']) {
                    $scope = array_pop($scopes);
                    if ($scope !== null) {
                        $this->closeFrame($i, $scope['var'], $scope['fn'], $tok['line']);
                    }
                }
                $stmtStart = true;
                continue;
            }

            if ($id === -1 && $text === ';' && $paren === 0) {
                if ($pendingFn !== null) {
                    $pendingFn = null;   // an abstract or interface method: no body to splice
                }
                if ($inCaseLabel) {
                    $inCaseLabel = false;
                }
                $stmtStart = true;
                continue;
            }

            if ($id === T_CASE || $id === T_DEFAULT) {
                $inCaseLabel = true;
                $stmtStart = false;
                continue;
            }
            if ($inCaseLabel && $id === -1 && $text === ':' && $paren === 0) {
                $inCaseLabel = false;
                $stmtStart = true;
                continue;
            }

            // --- the splice points ------------------------------------------------------
            $inStmtBlock = $braces !== [] && $braces[count($braces) - 1]['kind'] === 'stmt';
            $scope = $scopes === [] ? null : $scopes[count($scopes) - 1];

            if ($stmtStart && $paren === 0 && $inStmtBlock && $scope !== null && !$inCaseLabel) {
                if (!self::continuesPriorStatement($id, $text)) {
                    $this->observe($i, $scope['var'], $scope['fn'], $tok['line']);
                }
            }
            $stmtStart = false;

            if ($id === T_RETURN && $scope !== null) {
                $this->wrapReturn($i, $scope['var'], $scope['fn'], $tok['line']);
            }
        }
    }

    private bool $pendingMatch = false;

    /**
     * Whether a token continues the statement before it rather than beginning a new one.
     *
     * `}` then `else` is one statement, not two, and an observation spliced between them would
     * detach the `else` from its `if`. The same holds for `catch`, `finally`, and the `while` of
     * a `do`.
     */
    private static function continuesPriorStatement(int $id, string $text): bool
    {
        return in_array($id, [T_ELSE, T_ELSEIF, T_CATCH, T_FINALLY, T_WHILE], true)
            || ($id === -1 && ($text === '}' || $text === ')' || $text === ',' || $text === ';'));
    }

    /** `$__frN = TraceHook::enter(...); try {` immediately inside the body brace. */
    private function openFrame(int $braceIndex, string $var, string $fn, int $line): void
    {
        $at = $this->at($line);
        $this->edits[] = [
            'off' => $this->tokens[$braceIndex]['off'] + 1,
            'text' => " $var = " . self::HOOK . "::enter('" . self::esc($fn) . "', '$at', get_defined_vars()); try {",
        ];
    }

    /** `} catch (Throwable) { raise; throw; } finally { leave; }` before the closing brace. */
    private function closeFrame(int $braceIndex, string $var, string $fn, int $line): void
    {
        $at = $this->at($line);
        $t = $var . '_t';
        $this->edits[] = [
            'off' => $this->tokens[$braceIndex]['off'],
            'text' => "} catch (\\Throwable $t) { " . self::HOOK . "::raise($var, '"
                . self::esc($fn) . "', '$at', $t); throw $t; } finally { "
                . self::HOOK . "::leave($var); } ",
        ];
    }

    /** An observation before one statement. */
    private function observe(int $index, string $var, string $fn, int $line): void
    {
        $at = $this->at($line);
        $this->edits[] = [
            'off' => $this->tokens[$index]['off'],
            'text' => self::HOOK . "::line($var, '" . self::esc($fn) . "', '$at', get_defined_vars()); ",
        ];
    }

    /**
     * Wrap a return value in the identity passthrough.
     *
     * `return $x;` becomes `return TraceHook::returned($f, …, $x);` — never a temporary, so the
     * rewrite cannot change a type, a reference, or an evaluation order. A bare `return;` gets a
     * preceding statement instead, since there is no value to pass through.
     */
    private function wrapReturn(int $index, string $var, string $fn, int $line): void
    {
        $at = $this->at($line);
        $j = $this->next($index + 1);
        $n = count($this->tokens);
        if ($j >= $n) {
            return;
        }
        if ($this->tokens[$j]['id'] === -1 && $this->tokens[$j]['text'] === ';') {
            $this->edits[] = [
                'off' => $this->tokens[$index]['off'],
                'text' => self::HOOK . "::returned($var, '" . self::esc($fn) . "', '$at', null); ",
            ];
            return;
        }
        $end = $this->statementEnd($j);
        if ($end === null) {
            return;
        }
        $this->edits[] = [
            'off' => $this->tokens[$index]['off'] + strlen($this->tokens[$index]['text']),
            'text' => ' ' . self::HOOK . "::returned($var, '" . self::esc($fn) . "', '$at', (",
        ];
        $this->edits[] = ['off' => $this->tokens[$end]['off'], 'text' => '))'];
    }

    /** The index of the `;` ending the statement that starts at `$i`, at nesting depth zero. */
    private function statementEnd(int $i): ?int
    {
        $depth = 0;
        for ($k = $i, $n = count($this->tokens); $k < $n; $k++) {
            $t = $this->tokens[$k];
            if ($t['id'] === T_CURLY_OPEN || $t['id'] === T_DOLLAR_OPEN_CURLY_BRACES) {
                $depth++;
                continue;
            }
            if ($t['id'] !== -1) {
                continue;
            }
            if (in_array($t['text'], ['(', '[', '{'], true)) {
                $depth++;
            } elseif (in_array($t['text'], [')', ']', '}'], true)) {
                $depth--;
            } elseif ($t['text'] === ';' && $depth === 0) {
                return $k;
            }
        }
        return null;
    }

    /**
     * A location literal, read from the ORIGINAL line numbering.
     *
     * Instrumenting moves every line; reporting an instrumented line number would point the
     * reader at a file that exists nowhere on their disk.
     */
    private function at(int $line): string
    {
        return self::esc($this->file) . ':' . $line;
    }

    private static function esc(string $s): string
    {
        return str_replace(['\\', "'"], ['\\\\', "\\'"], $s);
    }

    /** Edits applied back to front, so every offset stays valid as they land. */
    private function apply(): string
    {
        $edits = $this->edits;
        usort($edits, static fn (array $a, array $b): int => $b['off'] <=> $a['off']);
        $out = $this->src;
        foreach ($edits as $e) {
            $out = substr($out, 0, $e['off']) . $e['text'] . substr($out, $e['off']);
        }
        return $out;
    }
}
