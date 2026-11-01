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
// URL, a secret in a query string), and the value of every field whose name says it holds a
// credential: in JSON, YAML or TOML text; in such text held inside a JSON string at any depth (a
// pair whose quotes are all escaped alike, which is how a transcript holds a file a tool read);
// and in unquoted assignments (`KEY=value`, `export KEY=value`, `set KEY=value`,
// `$env:KEY = value`, `--password=value`), whose value runs to whitespace or the end of the line.
// CredentialsData masks what that text parses to, and the two agree: parsing masked text gives
// the masked parse.
//
// A masked value is `<secret:1a2b3c4d>`, the first 8 hex of the value's sha256: deterministic, so a
// replay re-derives the same mask, and distinct, so two different keys stay two different masks.
// Both functions are idempotent (a mask is never masked again), which Boundary.Scrub requires. A
// value under a credential field is kept when it is empty, a variable reference (`${TOKEN}`,
// `{env:TOKEN}`, `%TOKEN%`, `$TOKEN`, `$env:TOKEN`) or already a mask.
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
	maskAt = regexp.MustCompile(`^<secret:[0-9a-f]{8}>`)
	// A variable reference names a secret without holding it.
	reference = regexp.MustCompile(`^(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%|\$(?:env:)?\w+)\z`)
	// ...and one that opens an unquoted value, which stops at `}`: `TOKEN=${MY_TOKEN}` is a reference.
	referenceAt = regexp.MustCompile(`^(?:\$\{(?:env:)?\w+\}|\{env:\w+\}|%\w+%)`)
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
	// A name holding a mask is a value an earlier pass masked, read back as the key of the next
	// pair (`token="<secret:...>": "v"`); `secret` in the mask is not the field saying anything.
	if exempt[name] || Mask.MatchString(name) {
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

// keeps reports a value left as it is under a credential field: empty, a reference, or one already
// holding a mask. The last is what makes a second pass a no-op: a reference kept on the first pass
// (`$AKIA...`, `${...}`) can have a shape masked inside it, and what is left (`$<secret:...>`) is no
// longer a reference but must not be masked again.
func keeps(v string) bool { return v == "" || reference.MatchString(v) || Mask.MatchString(v) }

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

// credentials runs the passes until the text stops changing. A later pass can change how an
// earlier one reads the text: a shape or an assignment masks the key of a pair that had swallowed
// the next pair's opening quote, and the next round reads that pair. Repeating makes the result a
// fixed point, so a second scrub is a no-op by construction (a replay re-scrubs what it read off
// the tape and must find it unchanged). It ends: every round that changes the text replaces at
// least one character outside the masks with a mask, and a mask is never masked again. Text that
// holds no credential costs one round.
func credentials(text string, exempt map[string]bool) string {
	pair := func(t string, at int) (pairMatch, bool) { return pairMatchAt(t, at, exempt) }
	escapedPair := func(t string, at int) (pairMatch, bool) { return escapedPairAt(t, at, exempt) }
	escapedValue := func(t string, at int) (int, string, bool) { return escapedValueAt(t, at, exempt) }
	for {
		before := text
		text = chained(text, pair)
		text = chained(text, escapedPair)
		text = sub(text, escapedValue)
		text = assignments(text, exempt)
		for _, s := range shapes {
			text = s.sub(text)
		}
		if text == before {
			return text
		}
	}
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

// sq reads '[^'\n]*' from a single quote at t[at] and returns the index past it, or -1.
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

// pairAt matches a pair starting at t[at]: the key's end, the value's start, the value's end. A
// double-quoted key does not open at an escaped quote (Python's (?<!\\)"): without that, every
// escaped quote in a long string of escaped JSON opened a token running to the string's end, and
// the pass took time quadratic in the length.
func pairAt(t string, at int) (k, sep, end int, ok bool) {
	if t[at] == '"' && at > 0 && t[at-1] == '\\' {
		return 0, 0, 0, false
	}
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

// A pairMatch is a pair matched at a start position: where the token its value opens with begins,
// where the match ends, and its masked text, "" when the pair is left as it is.
type pairMatch struct {
	value, end int
	masked     string
}

// pairMatchAt is the quoted-value pair at t[at], masked when its name holds a credential.
func pairMatchAt(t string, at int, exempt map[string]bool) (pairMatch, bool) {
	k, vs, end, ok := pairAt(t, at)
	if !ok {
		return pairMatch{}, false
	}
	key, value := t[at:k], unquote(t[vs:end])
	name := key
	if key[0] == '"' || key[0] == '\'' {
		name = unquote(key)
	} else {
		// A bare dotted TOML key (`env.API_KEY`) is a table path: its field is the last part.
		name = lastPart(key)
	}
	m := pairMatch{value: vs, end: end}
	if secretField(name, exempt) && !keeps(value) {
		q := t[vs : vs+1]
		m.masked = t[at:vs] + q + Secret(value) + q
	}
	return m, true
}

// chained is a substitution pass in which a pair left as it is gives its value back when that
// value opens a pair of its own. In `x = "api_key": "v1"` the pair `x = "api_key"` is not a
// credential, and a scan that resumed after its value never saw `"api_key": "v1"`. So the scan
// tries one pair anchored at the value's start, and only if none opens there does it skip the
// value whole: resuming inside a quoted value would read its escaped quotes as the openings of
// new tokens. A masked pair resumes the search after its end. This is Python's _chained.
func chained(t string, at func(t string, p int) (pairMatch, bool)) string {
	var b strings.Builder
	pos, anchor, skip, changed := 0, -1, 0, false
	for {
		var m pairMatch
		start, found := -1, false
		if anchor >= 0 {
			start = anchor
			m, found = at(t, anchor)
		} else {
			for p := pos; p < len(t) && !found; p++ {
				if m, found = at(t, p); found {
					start = p
				}
			}
		}
		if !found {
			if anchor >= 0 {
				b.WriteString(t[pos:skip])
				pos, anchor = skip, -1
				continue
			}
			if !changed {
				return t
			}
			b.WriteString(t[pos:])
			return b.String()
		}
		anchor = -1
		if m.masked == "" {
			b.WriteString(t[pos:m.value])
			pos, anchor, skip = m.value, m.value, m.end
			continue
		}
		b.WriteString(t[pos:start])
		b.WriteString(m.masked)
		pos, changed = m.end, true
	}
}

// --- a pair inside a JSON string, and unquoted assignments ------------------------------------
//
// Three more passes, run in this order after the pairs and before the shapes, each matched by
// hand: two need a backreference (all of a pair's quotes carry the same run of backslashes) and
// all three a lookbehind, neither of which RE2 has. As with the pairs, every part of each pattern
// is deterministic at a given start, so a failed match moves on one position and a match resumes
// at its end, which is Python's re.sub.

// sub rewrites t with every match of at: at reports, for a start position, the match's end and
// its replacement (the matched text itself when nothing is to be masked).
func sub(t string, at func(t string, p int) (end int, repl string, ok bool)) string {
	var b strings.Builder
	last, changed := 0, false
	for p := 0; p < len(t); {
		end, repl, ok := at(t, p)
		if !ok {
			p++
			continue
		}
		if repl != t[p:end] {
			b.WriteString(t[last:p])
			b.WriteString(repl)
			last, changed = end, true
		}
		p = end
	}
	if !changed {
		return t
	}
	b.WriteString(t[last:])
	return b.String()
}

// run is the end of the run of bytes from i that in accepts.
func run(t string, i int, in func(byte) bool) int {
	for i < len(t) && in(t[i]) {
		i++
	}
	return i
}

func isBlank(c byte) bool { return c == ' ' || c == '\t' }

// plain is [^"\\\n]: what a key or a value inside a JSON string may hold here.
func plain(c byte) bool { return c != '"' && c != '\\' && c != '\n' }

// escQuote reads exactly n backslashes and a quote at t[i], and returns the index past them, or -1.
func escQuote(t string, i, n int) int {
	if i+n >= len(t) || strings.Count(t[i:i+n], `\`) != n || t[i+n] != '"' {
		return -1
	}
	return i + n + 1
}

// sep reads [ \t]*[:=][ \t]* (or [ \t]*=[ \t]* when only is '=') at t[i], and returns its end.
func sepAt(t string, i int, only byte) int {
	i = run(t, i, isBlank)
	if i >= len(t) || !(t[i] == '=' || only == 0 && t[i] == ':') {
		return -1
	}
	return run(t, i+1, isBlank)
}

// bareKey reads [A-Za-z0-9_.-]+ at t[at] where the byte before is not one of those (Python's
// (?<![\w.-])), and returns its end, or -1.
func bareKey(t string, at int) int {
	if !isBare(t[at]) || at > 0 && isBare(t[at-1]) {
		return -1
	}
	return run(t, at, isBare)
}

func lastPart(k string) string { return k[strings.LastIndexByte(k, '.')+1:] }

// escapedPairAt is (?<!\\)(?P<q>\\+)"(?P<k>[^"\\\n]+)(?P=q)"(?P<sep>[ \t]*[:=][ \t]*)
// (?P<q2>(?P=q))"(?P<v>[^"\\\n]*)(?P=q)": a pair inside a JSON string, at any depth. The key is
// the name as written: it holds no escape. The value's token opens at q2, its run of backslashes.
func escapedPairAt(t string, at int, exempt map[string]bool) (pairMatch, bool) {
	if t[at] != '\\' || at > 0 && t[at-1] == '\\' {
		return pairMatch{}, false
	}
	qe := run(t, at, func(c byte) bool { return c == '\\' })
	if qe >= len(t) || t[qe] != '"' {
		return pairMatch{}, false
	}
	n := qe - at
	ks := qe + 1
	ke := run(t, ks, plain)
	if ke == ks {
		return pairMatch{}, false
	}
	ss := escQuote(t, ke, n)
	if ss < 0 {
		return pairMatch{}, false
	}
	se := sepAt(t, ss, 0)
	if se < 0 {
		return pairMatch{}, false
	}
	vs := escQuote(t, se, n)
	if vs < 0 {
		return pairMatch{}, false
	}
	ve := run(t, vs, plain)
	end := escQuote(t, ve, n)
	if end < 0 {
		return pairMatch{}, false
	}
	m := pairMatch{value: se, end: end}
	k, v, q := t[ks:ke], t[vs:ve], t[at:qe]
	if secretField(k, exempt) && !keeps(v) {
		m.masked = q + `"` + k + q + `"` + t[ss:se] + q + `"` + Secret(v) + q + `"`
	}
	return m, true
}

// escapedValueAt is (?<![\w.-])(?P<k>[A-Za-z0-9_.-]+)(?P<sep>[ \t]*[:=][ \t]*)
// (?P<q>\\+)"(?P<v>[^"\\\n]*)(?P=q)": a bare key and an escaped quoted value, a dotenv or YAML
// line held in a JSON string.
func escapedValueAt(t string, at int, exempt map[string]bool) (int, string, bool) {
	ke := bareKey(t, at)
	if ke < 0 {
		return 0, "", false
	}
	se := sepAt(t, ke, 0)
	if se < 0 {
		return 0, "", false
	}
	qe := run(t, se, func(c byte) bool { return c == '\\' })
	if qe == se || qe >= len(t) || t[qe] != '"' {
		return 0, "", false
	}
	ve := run(t, qe+1, plain)
	end := escQuote(t, ve, qe-se)
	if end < 0 {
		return 0, "", false
	}
	k, v, q := t[at:ke], t[qe+1:ve], t[se:qe]
	if !secretField(lastPart(k), exempt) || keeps(v) {
		return end, t[at:end], true
	}
	return end, t[at:se] + q + `"` + Secret(v) + q + `"`, true
}

// assignValue is [^\s"'`\\&;|<>,)}\]]: what an unquoted assigned value runs over.
func assignValue(c byte) bool {
	return !isSpace(c) && strings.IndexByte("\"'`\\&;|<>,)}]", c) < 0
}

// assignAt matches (?<![\w.-])(?P<k>[A-Za-z0-9_.-]+)(?P<sep>[ \t]*=[ \t]*)(?P<v>(?!=)[^\s"'`\\&;|<>,)}\]]+)
// at t[at]: an unquoted assignment, whose value runs to whitespace or the end of the line. It
// returns the key's end and the value's span. A value starting with `=` is a comparison
// (`token == x`), not an assignment.
func assignAt(t string, at int) (ke, vs, ve int, ok bool) {
	if ke = bareKey(t, at); ke < 0 {
		return 0, 0, 0, false
	}
	vs = sepAt(t, ke, '=')
	if vs < 0 || vs >= len(t) || t[vs] == '=' {
		return 0, 0, 0, false
	}
	if ve = run(t, vs, assignValue); ve == vs {
		return 0, 0, 0, false
	}
	return ke, vs, ve, true
}

// assignments masks the unquoted assignments, scanning left to right. Unlike the other passes, an
// assignment left as it is gives its value back to the scan: the search resumes right after its
// name, not after its value. In `a=token=x` the value of `a` holds an assignment of its own, and in
// `AKIA...=Bearer = v` a shape masked later turns the first name into a mask and frees
// `Bearer = v`; a scan that had swallowed it would mask it only on a second pass, and a scrub must
// be a no-op on its own output. A value directly followed by a mask is the rest of a value already
// masked (`$env:<secret:...>`) and is kept, as is one a reference opens (`${MY_TOKEN}`).
func assignments(t string, exempt map[string]bool) string {
	var b strings.Builder
	last, changed := 0, false
	for p := 0; p < len(t); {
		ke, vs, ve, ok := assignAt(t, p)
		if !ok {
			p++
			continue
		}
		v := t[vs:ve]
		if !secretField(lastPart(t[p:ke]), exempt) || keeps(v) ||
			maskAt.MatchString(t[ve:]) || referenceAt.MatchString(t[vs:]) {
			p = ke
			continue
		}
		b.WriteString(t[last:vs])
		b.WriteString(Secret(v))
		last, p, changed = ve, ve, true
	}
	if !changed {
		return t
	}
	b.WriteString(t[last:])
	return b.String()
}
