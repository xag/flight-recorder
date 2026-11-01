// Package scrubs holds stock declarations an app may hand its Boundary: credentials, masked by
// shape and by field.
//
// The recorder knows nothing about what any value means, and nothing here changes that: no
// recording consults this package unless the app declares it, exactly as it declares any other
// Scrub or Forbid.
//
//	boundary := flightrecorder.Boundary{
//		Scrub:  scrubs.Credentials,      // mask every credential, wherever it sits
//		Forbid: scrubs.CredentialShapes, // and refuse a line where one survived
//	}
//
// An app whose own fields are named like credentials and hold none (a `key` naming where a client
// keeps its servers, a `token_env` naming a variable) says so with an exemption:
//
//	Scrub: scrubs.CredentialsExcept("key", "token_env"),
//
// Credentials masks TEXT: in any string, every credential shape (an `sk-` key, a GitHub or GitLab
// token, an AWS access key id, a Slack token, a JWT, a Bearer or Basic credential, a password in a
// URL, a secret in a query string), and in JSON, YAML or TOML text, the value of every field whose
// name says it holds a credential. CredentialsData masks what that text parses to, and the two
// agree: parsing masked text gives the masked parse.
//
// A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the value's sha256: deterministic, so a
// replay re-derives the same mask, and distinct, so two different keys stay two different masks.
// Both functions are idempotent (a mask is never masked again), which Boundary.Scrub requires. A
// value under a credential field is kept when it is empty, a variable reference (`${TOKEN}`,
// `{env:TOKEN}`, `%TOKEN%`) or already a mask.
//
// This is the Go port of flight_recorder/scrubs.py, and it masks every text to the same bytes
// (spec/fixtures/credentials.json). The Python patterns are ASCII regular expressions with
// lookarounds, and RE2 has no lookaround, so each lookaround is a check written out by hand around
// an RE2 match, and the substitution loop reproduces Python's: leftmost match first, no overlap,
// and a candidate a lookaround rejects is abandoned for the next start position, which may lie
// inside it.
package scrubs

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"regexp"
	"strings"
)

// Mask is the shape of a masked value.
var Mask = regexp.MustCompile(`<secret:[0-9a-f]{8}>`)

var (
	maskWhole = regexp.MustCompile(`^<secret:[0-9a-f]{8}>\z`)
	// A variable reference names a secret without holding it.
	reference = regexp.MustCompile(`^(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%)\z`)
)

// pyspace is Python's ASCII \s, which has \v where RE2's \s does not.
const pyspace = `\t\n\v\f\r `

// CredentialShapes are the credential shapes as RE2 patterns for Boundary.Forbid. Each fires on a
// text exactly when the scrub's shape of the same index would mask something in it: a lookbehind
// becomes a consumed character or the start of the text, and a lookahead is spelled out. So the
// pair never refuses a line the scrub already cleaned, and refuses every line where a shape
// survived.
var CredentialShapes = []string{
	`(?:^|[^\w-])sk-(?:ant-)?[A-Za-z0-9_-]{20,}`,
	`(?:^|[^\w-])gh[pousr]_[A-Za-z0-9]{20,}`,
	`(?:^|[^\w-])github_pat_[A-Za-z0-9_]{20,}`,
	`(?:^|[^\w-])glpat-[A-Za-z0-9_-]{20,}`,
	`(?:^|[^A-Za-z0-9])AKIA[0-9A-Z]{16}(?:$|[^A-Za-z0-9])`,
	`(?:^|[^\w-])xox[abpr]-[A-Za-z0-9-]{10,}`,
	`(?:^|[^\w-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`,
	// with a digit in it, at least 8 long: "use a Bearer token" is prose, not a credential
	`\b(?:Bearer|Basic)[` + pyspace + `]+(?:` + bearerValue + `)`,
	// a password in a URL, unless it is a reference
	`://[^:@/` + pyspace + `"'<>\\]+:[^:@/` + pyspace + `"'<>$%{\\][^:@/` + pyspace + `"'<>\\]*@`,
	`[?&][\w-]*(?:key|secret|token|passw|auth|session|sig)[\w-]*=[^&` + pyspace + `"'<>#\\]+`,
}

// bearerValue is [A-Za-z0-9._~+/=-]{8,} holding a digit, as RE2 can say it: the first digit at
// index k, with enough after it to make 8.
var bearerValue = func() string {
	const n, a = `[A-Za-z._~+/=-]`, `[A-Za-z0-9._~+/=-]`
	var alts []string
	for k := 0; k < 7; k++ {
		alts = append(alts, strings.Repeat(n, k)+`[0-9]`+a+`{`+itoa(7-k)+`}`)
	}
	return strings.Join(append(alts, n+`{7,}[0-9]`), "|")
}()

func itoa(n int) string { return string(rune('0' + n)) }

// A shape for the scrub: an RE2 pattern whose group 1 is the part masked, and the lookarounds the
// Python pattern has, as checks. before reads the text ahead of a match start; after reads the
// match (its end and group 1's start).
type shape struct {
	re     *regexp.Regexp
	before func(t string, at int) bool
	after  func(t string, end, v int) bool
}

func isAlnum(c byte) bool {
	return 'a' <= c && c <= 'z' || 'A' <= c && c <= 'Z' || '0' <= c && c <= '9'
}
func isWord(c byte) bool { return isAlnum(c) || c == '_' }

// notAfter is a lookbehind (?<![...]) over an ASCII class: true at the start of the text, or when
// the byte before is not in the class. A non-ASCII character before is never in an ASCII class,
// and its last UTF-8 byte is not either.
func notAfter(in func(byte) bool) func(string, int) bool {
	return func(t string, at int) bool { return at == 0 || !in(t[at-1]) }
}

var (
	notWordDash = notAfter(func(c byte) bool { return isWord(c) || c == '-' })
	notAlnum    = notAfter(isAlnum)
	wordStart   = notAfter(isWord) // \b before a word character
)

var shapes = []shape{
	{re: regexp.MustCompile(`(sk-(?:ant-)?[A-Za-z0-9_-]{20,})`), before: notWordDash},
	{re: regexp.MustCompile(`(gh[pousr]_[A-Za-z0-9]{20,})`), before: notWordDash},
	{re: regexp.MustCompile(`(github_pat_[A-Za-z0-9_]{20,})`), before: notWordDash},
	{re: regexp.MustCompile(`(glpat-[A-Za-z0-9_-]{20,})`), before: notWordDash},
	{re: regexp.MustCompile(`(AKIA[0-9A-Z]{16})`), before: notAlnum,
		after: func(t string, end, _ int) bool { return end == len(t) || !isAlnum(t[end]) }},
	{re: regexp.MustCompile(`(xox[abpr]-[A-Za-z0-9-]{10,})`), before: notWordDash},
	{re: regexp.MustCompile(`(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})`), before: notWordDash},
	// (?=[A-Za-z._~+/=-]*[0-9]) at the value's start. The whitespace run before the value is
	// maximal in every match (the value cannot start with a space), so when this check fails no
	// other match starts at the same place, and the next start is the one to try.
	{re: regexp.MustCompile(`(?:Bearer|Basic)[` + pyspace + `]+([A-Za-z0-9._~+/=-]{8,})`), before: wordStart,
		after: func(t string, _, v int) bool {
			for ; v < len(t); v++ {
				c := t[v]
				if '0' <= c && c <= '9' {
					return true
				}
				if !(isAlnum(c) || strings.IndexByte("._~+/=-", c) >= 0) {
					return false
				}
			}
			return false
		}},
	// (?![$%{]) on the value's first character is a class RE2 can say.
	{re: regexp.MustCompile(`://[^:@/` + pyspace + `"'<>\\]+:([^:@/` + pyspace + `"'<>$%{\\][^:@/` + pyspace + `"'<>\\]*)@`)},
	{re: regexp.MustCompile(`[?&][\w-]*(?:key|secret|token|passw|auth|session|sig)[\w-]*=([^&` + pyspace + `"'<>#\\]+)`)},
}

// sub replaces group 1 of every match with its mask, as Python's re.sub does with the lookarounds
// in the pattern: a match is the leftmost whose checks hold, the search resumes at its end, and a
// rejected candidate moves the search one character on. Every pattern starts with an ASCII
// character, so one byte on is one character on.
func (s shape) sub(t string) string {
	var b strings.Builder
	last, p := 0, 0
	for p <= len(t) {
		loc := s.re.FindStringSubmatchIndex(t[p:])
		if loc == nil {
			break
		}
		at, end, vs, ve := p+loc[0], p+loc[1], p+loc[2], p+loc[3]
		if (s.before != nil && !s.before(t, at)) || (s.after != nil && !s.after(t, end, vs)) {
			p = at + 1
			continue
		}
		b.WriteString(t[last:vs])
		b.WriteString(Secret(t[vs:ve]))
		last, p = ve, end
	}
	if last == 0 {
		return t
	}
	b.WriteString(t[last:])
	return b.String()
}

// Secret is the mask of one value: `<secret:` + the first 8 hex of its UTF-8 sha256 + `>`.
func Secret(value string) string {
	sum := sha256.Sum256([]byte(value))
	return "<secret:" + hex.EncodeToString(sum[:4]) + ">"
}

// SecretField reports whether a field of this name holds a credential, unless the app exempted
// the name. The test is Python's `key(?!words?|board)|secret|token|passw|auth(?!or(?!i))|
// credential|cookie|session|bearer`, case-insensitive over ASCII only.
func SecretField(name string, exempt ...string) bool {
	return secretField(name, set(exempt))
}

func secretField(name string, exempt map[string]bool) bool {
	if exempt[name] {
		return false
	}
	n := asciiLower(name)
	for _, w := range []string{"secret", "token", "passw", "credential", "cookie", "session", "bearer"} {
		if strings.Contains(n, w) {
			return true
		}
	}
	// key, unless keyword(s) or keyboard; auth, unless author (authori- is still auth)
	return anyAt(n, "key", func(rest string) bool {
		return !strings.HasPrefix(rest, "word") && !strings.HasPrefix(rest, "board")
	}) || anyAt(n, "auth", func(rest string) bool {
		return !(strings.HasPrefix(rest, "or") && !strings.HasPrefix(rest, "ori"))
	})
}

// anyAt reports whether some occurrence of word in s is followed by text ok accepts.
func anyAt(s, word string, ok func(rest string) bool) bool {
	for i := 0; ; {
		j := strings.Index(s[i:], word)
		if j < 0 {
			return false
		}
		if ok(s[i+j+len(word):]) {
			return true
		}
		i += j + 1
	}
}

func asciiLower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if 'A' <= c && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	return string(b)
}

func set(names []string) map[string]bool {
	m := make(map[string]bool, len(names))
	for _, n := range names {
		m[n] = true
	}
	return m
}

// keeps reports a value left as it is under a credential field: empty, a reference, a mask.
func keeps(v string) bool { return v == "" || reference.MatchString(v) || maskWhole.MatchString(v) }

// unquote reads a quoted token as its value: a single-quoted one is its contents, a double-quoted
// one is a JSON string, or its contents when it is not valid JSON.
func unquote(tok string) string {
	if tok[0] == '\'' {
		return tok[1 : len(tok)-1]
	}
	var s string
	if err := json.Unmarshal([]byte(tok), &s); err != nil {
		return tok[1 : len(tok)-1]
	}
	return s
}

// Credentials is the text with every credential field's value and every credential shape masked.
// It has the type Boundary.Scrub takes.
func Credentials(text string) string { return credentials(text, nil) }

// CredentialsExcept is Credentials for an app whose fields of these names hold no credential. An
// exemption keeps the field's value from being masked as a field; a credential shape inside it is
// masked all the same.
func CredentialsExcept(exempt ...string) func(string) string {
	ex := set(exempt)
	return func(text string) string { return credentials(text, ex) }
}

func credentials(text string, exempt map[string]bool) string {
	text = pairs(text, exempt)
	for _, s := range shapes {
		text = s.sub(text)
	}
	return text
}

// CredentialsData is Credentials for parsed data: a string under a credential field is masked
// whole, any other string as text. Maps and slices are walked, keeping their Go types; other
// values are kept.
func CredentialsData(v any, exempt ...string) any { return data(v, set(exempt), nil) }

func data(v any, ex map[string]bool, key *string) any {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, x := range t {
			k := k
			out[k] = data(x, ex, &k)
		}
		return out
	case map[string]string:
		out := make(map[string]string, len(t))
		for k, x := range t {
			k := k
			out[k] = data(x, ex, &k).(string)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = data(x, ex, nil)
		}
		return out
	case []string:
		out := make([]string, len(t))
		for i, x := range t {
			out[i] = data(x, ex, nil).(string)
		}
		return out
	case string:
		if key != nil && secretField(*key, ex) && !keeps(t) {
			return Secret(t)
		}
		return credentials(t, ex)
	}
	return v
}

// --- the field pairs in text --------------------------------------------------------------
//
// Python's pattern: a key (a double-quoted string, a single-quoted one, or a bare dotted name
// not preceded by [\w.-]), a separator \s*[:=]\s*, and a quoted value. It is matched here by hand,
// at each start position in turn, because the bare key's lookbehind has no RE2 form. Every part is
// deterministic (no backtracking can give a different match at the same start), so trying each
// start position in order and resuming after a match is exactly what Python's sub does.

// isSpace is Python's ASCII \s as a byte test (pyspace is its spelling inside a pattern).
func isSpace(c byte) bool { return c == ' ' || '\t' <= c && c <= '\r' }
func isBare(c byte) bool  { return isWord(c) || c == '.' || c == '-' }

// dq reads "(?:[^"\\\n]|\\.)*" from t[at]=='"' and returns the index past it, or -1.
func dq(t string, at int) int {
	for i := at + 1; i < len(t); i++ {
		switch t[i] {
		case '"':
			return i + 1
		case '\n':
			return -1
		case '\\':
			if i+1 >= len(t) || t[i+1] == '\n' {
				return -1
			}
			i++
		}
	}
	return -1
}

// sq reads '[^'\n]*' from t[at]=='\” and returns the index past it, or -1.
func sq(t string, at int) int {
	for i := at + 1; i < len(t); i++ {
		switch t[i] {
		case '\'':
			return i + 1
		case '\n':
			return -1
		}
	}
	return -1
}

func quoted(t string, at int) int {
	switch t[at] {
	case '"':
		return dq(t, at)
	case '\'':
		return sq(t, at)
	}
	return -1
}

// pairAt matches a pair starting at t[at]: the key's end, the separator's end, the value's end.
func pairAt(t string, at int) (k, sep, end int, ok bool) {
	k = quoted(t, at)
	if k < 0 {
		if !isBare(t[at]) || (at > 0 && isBare(t[at-1])) {
			return 0, 0, 0, false
		}
		for k = at; k < len(t) && isBare(t[k]); k++ {
		}
	}
	i := k
	for i < len(t) && isSpace(t[i]) {
		i++
	}
	if i >= len(t) || (t[i] != ':' && t[i] != '=') {
		return 0, 0, 0, false
	}
	for i++; i < len(t) && isSpace(t[i]); i++ {
	}
	if i >= len(t) {
		return 0, 0, 0, false
	}
	if end = quoted(t, i); end < 0 {
		return 0, 0, 0, false
	}
	return k, i, end, true
}

func pairs(t string, exempt map[string]bool) string {
	var b strings.Builder
	last := 0
	for at := 0; at < len(t); {
		k, sep, end, ok := pairAt(t, at)
		if !ok {
			at++
			continue
		}
		key, value := t[at:k], unquote(t[sep:end])
		name := key
		if key[0] == '"' || key[0] == '\'' {
			name = unquote(key)
		} else {
			// A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
			name = key[strings.LastIndexByte(key, '.')+1:]
		}
		if secretField(name, exempt) && !keeps(value) {
			q := t[sep : sep+1]
			b.WriteString(t[last:sep])
			b.WriteString(q + Secret(value) + q)
			last = end
		}
		at = end
	}
	if last == 0 {
		return t
	}
	b.WriteString(t[last:])
	return b.String()
}
