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
 * password in a URL, a secret in a query string), and in JSON, YAML or TOML text, the value of
 * every field whose name says it holds a credential. `credentialsData()` masks what that text
 * parses to, and the two agree: parsing masked text gives the masked parse.
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
    private const REFERENCE = '~\A(?:\$\{(?:env:)?[A-Za-z0-9_]+\}|\{env:[A-Za-z0-9_]+\}|%[A-Za-z0-9_]+%)\z~';

    private const WHOLE_MASK = '~\A<secret:[0-9a-f]{8}>\z~';

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
        ~(?P<k>"(?:[^"\\\n]++|\\[^\n])*+"|'[^'\n]*+'|(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]++)(?P<sep>[ \t\n\r\f\x0B]*+[:=][ \t\n\r\f\x0B]*+)(?P<v>"(?:[^"\\\n]++|\\[^\n])*+"|'[^'\n]*+')~
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
        return !in_array($name, $exempt, true) && preg_match(self::SECRET_FIELD, $name) === 1;
    }

    /** A value left as it is under a credential field: empty, a reference, already a mask. */
    private static function keeps(string $value): bool
    {
        return $value === ''
            || preg_match(self::REFERENCE, $value) === 1
            || preg_match(self::WHOLE_MASK, $value) === 1;
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
        $paired = self::checked(preg_replace_callback(
            self::PAIR,
            static function (array $m) use ($exempt): string {
                $k = $m['k'];
                // A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
                if ($k[0] === '"' || $k[0] === "'") {
                    $name = self::unquote($k);
                } else {
                    $dot = strrpos($k, '.');
                    $name = $dot === false ? $k : substr($k, $dot + 1);
                }
                $value = self::unquote($m['v']);
                if (!self::secretField($name, $exempt) || self::keeps($value)) {
                    return $m[0];
                }
                $quote = $m['v'][0];
                return $k . $m['sep'] . $quote . self::secret($value) . $quote;
            },
            $text
        ));
        return self::shapes($paired);
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
