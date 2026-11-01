<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * Stock declarations an app may hand its Boundary: credentials, masked by shape and by field.
 *
 * The recorder knows nothing about what any value means, and nothing here changes that: no
 * recording consults this class unless the app declares it, exactly as it declares any other
 * scrub or forbid. It ships beside the recorder because every app that records a configuration
 * file, an environment or a conversation transcript meets the same credentials.
 *
 *     $boundary = (new Boundary())
 *         ->scrubbingWith(Scrubs::credentials(...))       // mask every credential, wherever it sits
 *         ->forbidden(...Scrubs::CREDENTIAL_SHAPES);      // and refuse a line where one survived
 *
 * An app whose own fields are named like credentials and hold none (a `key` naming where a
 * client keeps its servers, a `token_env` naming a variable) says so with `$exempt`:
 *
 *     ->scrubbingWith(static fn (string $s): string => Scrubs::credentials($s, ['key', 'token_env']))
 *
 * `credentials()` masks TEXT: in any string, every credential shape (an `sk-` key, a GitHub or
 * GitLab token, an AWS access key id, a Slack token, a JWT, a Bearer or Basic credential, a
 * password in a URL, a secret in a query string), and the value of every field whose name says
 * it holds a credential: in JSON, YAML or TOML text; in such text held inside a JSON string at
 * any depth (`\"KEY\": \"value\"`, which is how a Claude Code transcript holds a file a tool
 * read); and in unquoted assignments (`KEY=value`, `export KEY=value`, `set KEY=value`,
 * `$env:KEY = value`, `--password=value`), whose value runs to whitespace or the end of the
 * line. `credentialsData()` masks what that text parses to, and the two agree: parsing masked
 * text gives the masked parse.
 *
 * A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the sha256 of its UTF-8 bytes:
 * deterministic, so a replay re-derives the same mask, and distinct, so two different keys stay
 * two different masks. Both functions are idempotent (a mask is never masked again), which a
 * Boundary's scrub requires. A value under a credential field is kept when it is empty, a
 * variable reference (`${TOKEN}`, `{env:TOKEN}`, `%TOKEN%`) or already a mask.
 *
 * ## Same bytes as every other runtime
 *
 * The reference implementation compiles its patterns ASCII-only (`\w` is `[A-Za-z0-9_]`, `\s` is
 * `[ \t\n\r\f\v]`). These patterns spell those classes out rather than writing `\w`, `\s` or
 * `\b`, because PHP builds PCRE's character tables from the current `LC_CTYPE` once an app calls
 * `setlocale()`, and a Latin-1 locale would make bytes above 0x7F word characters. No pattern
 * carries the `u` flag: matching is over bytes, and every class that admits a non-ASCII byte
 * admits every one, so a multibyte character is matched as a run of bytes exactly where the
 * reference matches it as one code point. spec/fixtures/credentials.json holds the vectors.
 */
final class Scrubs
{
    /** What a mask looks like, for asserting on or for a forbid that must NOT fire on it. */
    public const MASK = '/<secret:[0-9a-f]{8}>/';

    /**
     * Credential shapes, wherever they appear: PCRE patterns with delimiters, ready for
     * `Boundary::forbidden(...Scrubs::CREDENTIAL_SHAPES)`. Group `v` is the part masked; the
     * rest is context that says it is a credential (`Bearer `, `://user:`...`@`, `?api_key=`).
     */
    public const CREDENTIAL_SHAPES = [
        '~(?<![A-Za-z0-9_-])(?P<v>sk-(?:ant-)?[A-Za-z0-9_-]{20,})~',
        '~(?<![A-Za-z0-9_-])(?P<v>gh[pousr]_[A-Za-z0-9]{20,})~',
        '~(?<![A-Za-z0-9_-])(?P<v>github_pat_[A-Za-z0-9_]{20,})~',
        '~(?<![A-Za-z0-9_-])(?P<v>glpat-[A-Za-z0-9_-]{20,})~',
        '~(?<![A-Za-z0-9])(?P<v>AKIA[0-9A-Z]{16})(?![A-Za-z0-9])~',
        '~(?<![A-Za-z0-9_-])(?P<v>xox[abpr]-[A-Za-z0-9-]{10,})~',
        '~(?<![A-Za-z0-9_-])(?P<v>eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})~',
        // with a digit in it: "use a Bearer token" is prose, not a credential
        '~(?<![A-Za-z0-9_])(?:Bearer|Basic)[ \t\n\r\f\x0B]+(?P<v>(?=[A-Za-z._\~+/=-]*[0-9])[A-Za-z0-9._\~+/=-]{8,})~',
        // a password in a URL, unless it is a reference
        '~://[^:@/ \t\n\r\f\x0B"\'<>\\\\]+:(?P<v>(?![$%{])[^:@/ \t\n\r\f\x0B"\'<>\\\\]+)@~',
        '~[?&][A-Za-z0-9_-]*(?:key|secret|token|passw|auth|session|sig)[A-Za-z0-9_-]*=(?P<v>[^& \t\n\r\f\x0B"\'<>#\\\\]+)~',
    ];

    /** A field whose name says it holds a credential. */
    private const SECRET_FIELD =
        '~key(?!words?|board)|secret|token|passw|auth(?!or(?!i))|credential|cookie|session|bearer~i';

    /** A variable reference names a secret without holding it. */
    private const REFERENCE =
        '~\A(?:\$\{(?:env:)?[A-Za-z0-9_]+\}|\{env:[A-Za-z0-9_]+\}|%[A-Za-z0-9_]+%|\$(?:env:)?[A-Za-z0-9_]+)\z~';

    /** A reference opening an unquoted value (`TOKEN=${MY_TOKEN}`, whose value stops at `}`). */
    private const REFERENCE_AT = '~\G(?:\$\{(?:env:)?[A-Za-z0-9_]+\}|\{env:[A-Za-z0-9_]+\}|%[A-Za-z0-9_]+%)~';

    /** A mask starting exactly at the offset searched from. */
    private const MASK_AT = '~\G<secret:[0-9a-f]{8}>~';

    /**
     * A `key: "value"` (JSON, YAML) or `key = "value"` (TOML) pair in text.
     *
     * Possessive quantifiers where the reference has greedy ones: each run here has exactly one
     * way to match (a quoted string ends at its first unescaped quote, a bare key at its first
     * non-key byte), so possession changes no match and only spares PCRE's backtracking stack on
     * a long value. `[^\n]` is the reference's `.`, spelled so that no PCRE newline convention
     * can change it.
     */
    private const PAIR = <<<'RE'
        ~(?P<k>(?<!\\)"(?:[^"\\\n]++|\\[^\n])*+"|'[^'\n]*+'|(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]++)(?P<sep>[ \t\n\r\f\x0B]*+[:=][ \t\n\r\f\x0B]*+)(?P<v>"(?:[^"\\\n]++|\\[^\n])*+"|'[^'\n]*+')~
        RE;

    /**
     * The same pair written inside a JSON string, at any depth: `\"KEY\": \"value\"` is a JSON
     * document held in a string, `\\\"KEY\\\": ...` that string held in another. All four
     * quotes carry the same run of backslashes. Key and value hold no quote, backslash or line
     * break, so the value in the text is the value in its parse.
     */
    private const ESCAPED_PAIR = <<<'RE'
        ~(?<!\\)(?P<q>\\+)"(?P<k>[^"\\\n]+)(?P=q)"(?P<sep>[ \t]*[:=][ \t]*)(?P<q2>(?P=q))"(?P<v>[^"\\\n]*)(?P=q)"~
        RE;

    /** ...and with a bare key: `KEY=\"value\"`, `password: \"value\"`, a dotenv or YAML line in a JSON string. */
    private const ESCAPED_VALUE = <<<'RE'
        ~(?<![A-Za-z0-9_.-])(?P<k>[A-Za-z0-9_.-]+)(?P<sep>[ \t]*[:=][ \t]*)(?P<q>\\+)"(?P<v>[^"\\\n]*)(?P=q)"~
        RE;

    /**
     * An unquoted assignment: `KEY=value`, `export KEY=value`, `set KEY=value`, `$env:KEY = value`,
     * a .env line, a `--password=...` flag. The value runs to whitespace or the end of the line,
     * and stops at a quote, a backslash (where a JSON escape such as `\n` begins), or `&;|<>,)}]`.
     * A value starting with `=` is a comparison (`token == x`), not an assignment.
     */
    private const ASSIGN = <<<'RE'
        ~(?<![A-Za-z0-9_.-])(?P<k>[A-Za-z0-9_.-]+)(?P<sep>[ \t]*=[ \t]*)(?P<v>(?!=)[^ \t\n\r\f\x0B"'`\\&;|<>,)}\]]+)~
        RE;

    /** The mask of one value: `<secret:` + the first 8 hex of its UTF-8 sha256 + `>`. */
    public static function secret(string $value): string
    {
        return '<secret:' . substr(hash('sha256', $value), 0, 8) . '>';
    }

    /**
     * Whether a field of this name holds a credential, unless the app exempted the name.
     *
     * @param list<string> $exempt
     */
    public static function secretField(string $name, array $exempt = []): bool
    {
        // A name holding a mask is a value an earlier pass masked, read back as the key of the
        // next pair (`token="<secret:...>": "v"`); `secret` in the mask is not the field saying
        // anything.
        return !in_array($name, $exempt, true)
            && preg_match(self::MASK, $name) !== 1
            && preg_match(self::SECRET_FIELD, $name) === 1;
    }

    /**
     * A value left as it is under a credential field: empty, a reference, or one already holding
     * a mask. The last is what makes a second pass a no-op: a reference kept on the first pass
     * (`$AKIA...`, `${...}`) can have a shape masked inside it, and what is left
     * (`$<secret:...>`) is no longer a reference but must not be masked again.
     */
    private static function keeps(string $value): bool
    {
        return $value === ''
            || preg_match(self::REFERENCE, $value) === 1
            || preg_match(self::MASK, $value) === 1;
    }

    /** The last part of a dotted key (`env.API_KEY` is a table path; its field is `API_KEY`). */
    private static function lastPart(string $key): string
    {
        $dot = strrpos($key, '.');
        return $dot === false ? $key : substr($key, $dot + 1);
    }

    /** The string a quoted token stands for: JSON-unquoted, or its quotes stripped. */
    private static function unquote(string $token): string
    {
        if ($token[0] === "'") {
            return substr($token, 1, -1);
        }
        try {
            $v = json_decode($token, false, 512, JSON_THROW_ON_ERROR);
        } catch (\JsonException) {
            return substr($token, 1, -1);
        }
        return is_string($v) ? $v : substr($token, 1, -1);
    }

    /**
     * Throws when PCRE gives up (a backtracking or JIT limit), so the failure lands where a
     * Boundary's scrub failure lands: the value masked whole, never written as it came.
     */
    private static function checked(?string $out): string
    {
        if ($out === null) {
            throw new \RuntimeException('credential scrub failed: ' . preg_last_error_msg());
        }
        return $out;
    }

    private static function shapes(string $text): string
    {
        foreach (self::CREDENTIAL_SHAPES as $shape) {
            $text = self::checked(preg_replace_callback(
                $shape,
                static function (array $m): string {
                    [$whole, $start] = $m[0];
                    [$v, $at] = $m['v'];
                    $at -= $start;
                    return substr($whole, 0, $at) . self::secret($v) . substr($whole, $at + strlen($v));
                },
                $text,
                -1,
                $count,
                PREG_OFFSET_CAPTURE
            ));
        }
        return $text;
    }

    /**
     * `$text` with every credential field's value and every credential shape masked.
     *
     * @param list<string> $exempt field names that hold no credential although named like one
     */
    public static function credentials(string $text, array $exempt = []): string
    {
        // The passes run until the text stops changing. A later pass can change how an earlier
        // one reads the text: a shape or an assignment masks the key of a pair that had
        // swallowed the next pair's opening quote, and the next round reads that pair.
        // Repeating makes the result a fixed point, so a second scrub is a no-op by construction.
        // It ends: every round that changes the text replaces at least one character outside the
        // masks with a mask, and a mask is never masked again. Text holding no credential costs
        // one round.
        while (true) {
            $next = self::round($text, $exempt);
            if ($next === $text) {
                return $text;
            }
            $text = $next;
        }
    }

    /**
     * One round of the five passes: pair, escaped pair, escaped value, assignments, shapes.
     *
     * @param list<string> $exempt
     */
    private static function round(string $text, array $exempt): string
    {
        $paired = self::chained(
            self::PAIR,
            static function (array $m) use ($exempt): ?string {
                $k = $m['k'];
                // A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
                if ($k[0] === '"' || $k[0] === "'") {
                    $name = self::unquote($k);
                } else {
                    $name = self::lastPart($k);
                }
                $value = self::unquote($m['v']);
                if (!self::secretField($name, $exempt) || self::keeps($value)) {
                    return null;
                }
                $quote = $m['v'][0];
                return $k . $m['sep'] . $quote . self::secret($value) . $quote;
            },
            $text,
            'v'
        );
        $paired = self::chained(
            self::ESCAPED_PAIR,
            static function (array $m) use ($exempt): ?string {
                if (!self::secretField($m['k'], $exempt) || self::keeps($m['v'])) {
                    return null;
                }
                $q = $m['q'];
                return $q . '"' . $m['k'] . $q . '"' . $m['sep'] . $q . '"' . self::secret($m['v']) . $q . '"';
            },
            $paired,
            'q2'
        );
        $paired = self::checked(preg_replace_callback(
            self::ESCAPED_VALUE,
            static function (array $m) use ($exempt): string {
                if (!self::secretField(self::lastPart($m['k']), $exempt) || self::keeps($m['v'])) {
                    return $m[0];
                }
                $q = $m['q'];
                return $m['k'] . $m['sep'] . $q . '"' . self::secret($m['v']) . $q . '"';
            },
            $paired
        ));
        return self::shapes(self::assignments($paired, $exempt));
    }

    /**
     * `preg_replace_callback($pattern, $mask, $text)`, except that a pair left as it is (`$mask`
     * returns null) gives its value back when that value opens a pair of its own.
     *
     * In `x = "api_key": "v1"` the pair `x = "api_key"` is not a credential, and a scan that
     * resumed after its value never saw `"api_key": "v1"`. So the scan tries one pair anchored
     * at the value's start (the `A` modifier anchors at the offset), and only if none opens
     * there does it skip the value whole: resuming inside a quoted value would read its escaped
     * quotes as the openings of new tokens. `$group` names the group where the value's token
     * begins. Searching with an offset (not on a substring) lets a lookbehind see before it.
     *
     * @param callable(array<int|string, string>): ?string $mask
     */
    private static function chained(string $pattern, callable $mask, string $text, string $group): string
    {
        $anchored = $pattern . 'A';
        $out = '';
        $pos = 0;
        $anchor = null;
        $skip = 0;
        while (true) {
            $hit = $anchor !== null
                ? preg_match($anchored, $text, $m, PREG_OFFSET_CAPTURE, $anchor)
                : preg_match($pattern, $text, $m, PREG_OFFSET_CAPTURE, $pos);
            if ($hit === false) {
                self::checked(null);
            }
            if ($hit === 0) {
                if ($anchor !== null) {
                    $anchor = null;
                    $out .= substr($text, $pos, $skip - $pos);
                    $pos = $skip;
                    continue;
                }
                return $out . substr($text, $pos);
            }
            $anchor = null;
            [$whole, $start] = $m[0];
            $replaced = $mask(array_map(static fn (array $g): string => $g[0], $m));
            if ($replaced === null) {
                $valueAt = $m[$group][1];
                $out .= substr($text, $pos, $valueAt - $pos);
                $pos = $anchor = $valueAt;
                $skip = $start + strlen($whole);
                continue;
            }
            $out .= substr($text, $pos, $start - $pos) . $replaced;
            $pos = $start + strlen($whole);
        }
    }

    /**
     * The unquoted assignments masked, scanning left to right.
     *
     * Unlike the other passes, an assignment left as it is gives its value back to the scan: the
     * search resumes right after its name, not after its value. In `a=token=x` the value of `a`
     * holds an assignment of its own, and in `AKIA...=Bearer = v` a shape masked later turns the
     * first name into a mask and frees `Bearer = v`; a scan that had swallowed it would mask it
     * only on a second pass, and a scrub must be a no-op on its own output. A value directly
     * followed by a mask is the rest of a value already masked (`$env:<secret:...>`) and is kept.
     *
     * Searching with an offset (not on a substring) lets the lookbehind see what precedes it.
     *
     * @param list<string> $exempt
     */
    private static function assignments(string $text, array $exempt): string
    {
        $out = '';
        $pos = 0;
        while (true) {
            $hit = preg_match(self::ASSIGN, $text, $m, PREG_OFFSET_CAPTURE, $pos);
            if ($hit === false) {
                self::checked(null);
            }
            if ($hit === 0) {
                return $out . substr($text, $pos);
            }
            [$key, $keyAt] = $m['k'];
            [$value, $valueAt] = $m['v'];
            $keyEnd = $keyAt + strlen($key);
            $valueEnd = $valueAt + strlen($value);
            if (!self::secretField(self::lastPart($key), $exempt)
                || self::keeps($value)
                || preg_match(self::MASK_AT, $text, $unused, 0, $valueEnd) === 1
                || preg_match(self::REFERENCE_AT, $text, $unused, 0, $valueAt) === 1
            ) {
                $out .= substr($text, $pos, $keyEnd - $pos);
                $pos = $keyEnd;
                continue;
            }
            $out .= substr($text, $pos, $valueAt - $pos) . self::secret($value);
            $pos = $valueEnd;
        }
    }

    /**
     * `credentials()` for parsed data: a string under a credential field is masked whole, any
     * other string as text. Maps (a `\stdClass` or a keyed array) and lists are walked; other
     * values are kept. A `\stdClass` comes back a `\stdClass`, so an empty map stays `{}`.
     *
     * @param list<string> $exempt
     */
    public static function credentialsData(mixed $value, array $exempt = [], ?string $key = null): mixed
    {
        if ($value instanceof \stdClass) {
            $out = new \stdClass();
            foreach (get_object_vars($value) as $k => $v) {
                $out->{(string) $k} = self::credentialsData($v, $exempt, (string) $k);
            }
            return $out;
        }
        if (is_array($value)) {
            if (array_is_list($value)) {
                return array_map(static fn ($v) => self::credentialsData($v, $exempt), $value);
            }
            $out = [];
            foreach ($value as $k => $v) {
                $out[$k] = self::credentialsData($v, $exempt, (string) $k);
            }
            return $out;
        }
        if (is_string($value)) {
            if ($key !== null && self::secretField($key, $exempt) && !self::keeps($value)) {
                return self::secret($value);
            }
            return self::credentials($value, $exempt);
        }
        return $value;
    }
}
