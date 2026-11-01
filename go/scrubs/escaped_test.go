package scrubs

// What actually leaked: a pair inside a JSON string, and unquoted assignmentForms. A session read a
// file holding `"ALPACA_API_SECRET_KEY": "..."`; a transcript holds that tool result as a JSON
// string, so the pair reached the tape as `\"ALPACA_API_SECRET_KEY\": \"...\"`. These mirror the
// Python reference's tests over the same domain.

import (
	"bytes"
	"encoding/json"
	"math/rand"
	"strings"
	"testing"
	"time"
)

var (
	exemptNames = []string{"key", "token_env", "auth"}
	keys        = []string{"ALPACA_API_KEY_ID", "api_token", "Authorization", "password", "session", "key",
		"token_env", "auth", "author", "keywords", "command", "url", "DB"}
	sk     = "sk-ant-" + "api03-abcdefghijklmnopqrstu"
	ghp    = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123"
	akia   = "AKIA" + "ABCDEFGHIJKLMNOP"
	values = []string{"PKRAW0001", `s3cr"et\x`, "Bearer abc123def456", sk, ghp, akia, "postgres://u:pw1234@h/db",
		"https://x.io/mcp?api_key=abc123&x=1", "${MY_TOKEN}", "{env:MY_TOKEN}", "$MY_TOKEN", "", "mcpServers",
		"use a Bearer token", "é", "<secret:0123abcd>",
		`curl "https://x.io/?token=abc123" -s`, `x://u:ab\c@h`, `curl "https://u:pw1234@h/x" -s`}
)

// pyJSON is json.dumps(v, ensure_ascii=False): no HTML escaping, as the Python reference writes.
func pyJSON(v any, indent bool) string {
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	if indent {
		enc.SetIndent("", "  ")
	}
	if err := enc.Encode(v); err != nil {
		panic(err)
	}
	return strings.TrimSuffix(b.String(), "\n")
}

func nest(text string, depth int) string {
	for i := 0; i < depth; i++ {
		text = pyJSON(text, false)
	}
	return text
}

func unnest(t *testing.T, text string) string {
	t.Helper()
	var s string
	if err := json.Unmarshal([]byte(text), &s); err != nil {
		t.Fatalf("the masked text is no longer a JSON string: %v\n%s", err, text)
	}
	return s
}

func without(vs []string, chars string) []string {
	var out []string
	for _, v := range vs {
		if !strings.ContainsAny(v, chars) {
			out = append(out, v)
		}
	}
	return out
}

func TestAPairInsideAJSONStringMasksAsItsParseDoes(t *testing.T) {
	nestable := without(values, "\"\\\n")
	for depth := 1; depth <= 3; depth++ {
		for _, exempt := range [][]string{nil, exemptNames} {
			scrub := CredentialsExcept(exempt...)
			for _, k := range keys {
				for _, v := range nestable {
					doc := map[string]any{"servers": map[string]any{"x": map[string]any{k: v}}}
					for _, inner := range []string{pyJSON(doc, true), pyJSON(k, false) + " = " + pyJSON(v, false) + "\n"} {
						outer := nest(inner, depth)
						masked := scrub(outer)
						if got, want := unnest(t, masked), scrub(nest(inner, depth-1)); got != want {
							t.Fatalf("depth %d, exempt %v: the masked text parses to\n%s\nbut its parse masks to\n%s", depth, exempt, got, want)
						}
						if scrub(masked) != masked {
							t.Fatalf("depth %d: masking twice changed %q", depth, outer)
						}
					}
				}
			}
		}
	}
}

func TestTheTranscriptRowThatLeakedIsMasked(t *testing.T) {
	keyValue := "PKRAW" + "0001TESTVALUE"
	traceLine := pyJSON(map[string]any{"e": "L", "d": map[string]any{"env": map[string]any{
		"ALPACA_API_SECRET_KEY": keyValue, "PATH": "/usr/bin"}}}, false)
	row := pyJSON(map[string]any{"type": "user", "message": map[string]any{"content": []any{
		map[string]any{"type": "tool_result", "tool_use_id": "t1", "content": traceLine}}}}, false)
	for depth, text := range []string{row, nest(row, 1)} {
		masked := Credentials(text)
		if strings.Contains(masked, keyValue) || !strings.Contains(masked, "/usr/bin") {
			t.Errorf("depth %d: the key survived, or the path did not:\n%s", depth+1, masked)
		}
		if Credentials(masked) != masked {
			t.Errorf("depth %d: masking twice changed the row", depth+1)
		}
	}
}

var assignmentForms = []func(k, v string) string{
	func(k, v string) string { return k + "=" + v },
	func(k, v string) string { return "export " + k + "=" + v },
	func(k, v string) string { return "set " + k + "=" + v },
	func(k, v string) string { return "$env:" + k + " = " + v },
	func(k, v string) string { return k + `="` + v + `"` },
	func(k, v string) string { return k + "='" + v + "'" },
	func(k, v string) string { return "$env:" + k + ` = "` + v + `"` },
	func(k, v string) string { return "--" + k + "=" + v },
}

func assignable() []string {
	var out []string
	for _, v := range without(values, " \t\r\n\"'`\\&;|<>,)}]") {
		if v != "" && v[0] != '=' {
			out = append(out, v)
		}
	}
	return out
}

func TestAnAssignmentMasksItsValueWhenTheNameHoldsACredential(t *testing.T) {
	for _, k := range keys {
		for _, v := range assignable() {
			for i, form := range assignmentForms {
				text := form(k, v) + " # done"
				want := form(k, Credentials(v)) + " # done"
				if SecretField(k) && !keeps(v) {
					want = form(k, Secret(v)) + " # done"
				}
				if got := Credentials(text); got != want {
					t.Errorf("form %d, %s: got %q, want %q", i, k, got, want)
				}
			}
		}
	}
}

func TestADotenvFileMasksAlikeAtEveryDepth(t *testing.T) {
	var lines []string
	for _, k := range keys {
		for _, v := range assignable() {
			for _, form := range assignmentForms {
				lines = append(lines, form(k, v))
			}
		}
	}
	dotenv := strings.Join(lines, "\n") + "\n"
	want := Credentials(dotenv)
	for depth := 0; depth <= 2; depth++ {
		outer := nest(dotenv, depth)
		masked := Credentials(outer)
		if Credentials(masked) != masked {
			t.Errorf("depth %d: masking twice changed the file", depth)
		}
		for i := 0; i < depth; i++ {
			masked = unnest(t, masked)
		}
		if masked != want {
			t.Errorf("depth %d: the file masks differently held in a string\n%s", depth, firstDiff(masked, want))
		}
	}
}

func TestAComparisonIsNotAnAssignment(t *testing.T) {
	for _, text := range []string{"if token == other:", "token != x", "password==hash(p)", "token<=3"} {
		if got := Credentials(text); got != text {
			t.Errorf("%q masked to %q", text, got)
		}
	}
}

func TestAVariableReferenceIsKept(t *testing.T) {
	for _, ref := range []string{"$MY_TOKEN", "$env:MY_TOKEN", "${MY_TOKEN}", "${env:MY_TOKEN}", "{env:MY_TOKEN}", "%MY_TOKEN%"} {
		if got := CredentialsData(map[string]any{"token": ref}); got.(map[string]any)["token"] != ref {
			t.Errorf("the reference %q was masked", ref)
		}
	}
}

// Found by differential fuzzing: texts the scrub masked differently on a second pass. A scrub must
// be a no-op on its own output, or a replay that re-scrubs what it read off the tape compares
// against something else.
var secondPass = []string{
	"'" + akia + "=Bearer  = Bearer ,", // a shape masked later freed an assignment
	"token = $env:" + akia + `\"KEY`,   // a kept reference had a shape masked inside
	`{"token": "$` + akia + `"}`,
	`{"token": "${` + akia + `}"}`,
	akia + ` = "api_key": "v1"`, // a pair left as it is swallowed the next pair's key
	`token="token": "{"x"`,      // a masked value read back as a key holds "secret"
	`$token='pw' = "api_key"Bearer`,
	// a later pass changed how an earlier one read the text (the passes now run to a fixed point)
	akia + `:""key":"y"`,
	`token=v:"auth:"y"`,
	`token=-=""KEY":"1"`,
	`session=d:\"passw:\"y\"`,
	"token=_x=\"token=\"token\": \"\t)&{",
	`token="token": "_;x=" token="token": "0123456789`,
}

// The fragments the differential fuzzing of the ports used (the Python reference's _FRAGMENTS).
var fragments = []string{`\`, `\\`, `\"`, `\\\"`, `"`, `""`, `'`, "`", `$`, `$env:`,
	`${X}`, `%X%`, `--`, `==`, `=`, ` = `, `:`, `: `, `token`, `secretBearer`, `api_key`,
	`password`, `a.b.`, `KEY`, `v1`, akia, `Bearer `, `é`, "\n", ` `, "\t", `<`, `>`, `&`, `?`,
	`://`, `@`, `{`, `}`, `)`, `,`, `;`, `<secret:0123abcd>`, `"api_key"`, `${`, `a=`, `token=`,
	`'pw'`, `x = `, `": "`, `"token": "`, `x="`, `auth:`, `passw:`}

func TestTheScrubIsAFixedPointOverDenseFragments(t *testing.T) {
	rng := rand.New(rand.NewSource(17))
	for i := 0; i < 20000; i++ {
		var b strings.Builder
		for n := 1 + rng.Intn(16); n > 0; n-- {
			b.WriteString(fragments[rng.Intn(len(fragments))])
		}
		text := b.String()
		var exempt []string
		if i%2 == 1 {
			exempt = []string{"password"}
		}
		scrub := CredentialsExcept(exempt...)
		once := scrub(text)
		if again := scrub(once); again != once {
			t.Fatalf("not a fixed point (exempt %v): %q\n once  %q\n twice %q", exempt, text, once, again)
		}
	}
}

func TestAPairLeftAsItIsGivesBackAValueThatOpensAPair(t *testing.T) {
	if got, want := Credentials(`x = "api_key": "v1"`), `x = "api_key": "`+Secret("v1")+`"`; got != want {
		t.Errorf("got %q, want %q", got, want)
	}
	text := `\"x\": \"api_key\": \"v1\"`
	if got := Credentials(text); strings.Contains(got, "v1") {
		t.Errorf("%q masked to %q", text, got)
	}
}

// A transcript is one long string of escaped JSON. When every escaped quote could open a quoted
// token running to the string's end, the time was quadratic in the length.
func TestALongStringOfEscapedJSONScrubsInLinearTime(t *testing.T) {
	rng := rand.New(rand.NewSource(3))
	chars := []byte{'"', 'a', 'b', ' ', ':'}
	inner := make([]byte, 50000)
	for i := range inner {
		inner[i] = chars[rng.Intn(len(chars))]
	}
	for depth := 1; depth <= 2; depth++ {
		text := nest(string(inner), depth)
		start := time.Now()
		Credentials(text)
		if d := time.Since(start); d > time.Second {
			t.Errorf("depth %d: %d bytes took %v", depth, len(text), d)
		}
	}
}

func TestTheScrubIsANoOpOnItsOwnOutput(t *testing.T) {
	for _, text := range secondPass {
		once := Credentials(text)
		if again := Credentials(once); again != once {
			t.Errorf("%q: a second pass changed %q to %q", text, once, again)
		}
		if strings.Contains(once, akia) {
			t.Errorf("%q: the key survived", text)
		}
	}
}

func TestAnUnchangedAssignmentGivesItsValueBackToTheScan(t *testing.T) {
	if got, want := Credentials("a=token=abc123"), "a=token="+Secret("abc123"); got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestAReferenceOpeningAnUnquotedValueIsKept(t *testing.T) {
	for _, text := range []string{"TOKEN=${MY_TOKEN}", "TOKEN={env:MY_TOKEN}", "TOKEN=%MY_TOKEN%", "TOKEN=$MY_TOKEN"} {
		if got := Credentials(text); got != text {
			t.Errorf("%q masked to %q", text, got)
		}
	}
}
