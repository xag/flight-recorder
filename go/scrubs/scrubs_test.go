package scrubs

// The vectors every runtime checks: spec/fixtures/credentials.json holds the Python
// implementation's output, and this one must produce the same bytes for every text and the same
// data for every document, under each case's exemptions.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
)

type vectorCase struct {
	Exempt     []string `json:"exempt"`
	Text       *string  `json:"text"`
	Masked     string   `json:"masked"`
	Data       any      `json:"data"`
	MaskedData any      `json:"masked_data"`
}

func vectors(t *testing.T) (mask string, cases []vectorCase) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "spec", "fixtures", "credentials.json"))
	if err != nil {
		t.Fatalf("reading the shared vectors: %v", err)
	}
	var doc struct {
		Mask  string       `json:"mask"`
		Cases []vectorCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("the vectors hold no case")
	}
	return doc.Mask, doc.Cases
}

func TestEveryVectorMasksToTheSameBytesAsEveryRuntime(t *testing.T) {
	mask, cases := vectors(t)
	if mask != Mask.String() {
		t.Errorf("the mask is %q here and %q in the vectors", Mask.String(), mask)
	}
	texts, docs := 0, 0
	for i, c := range cases {
		if c.Text != nil {
			texts++
			got := CredentialsExcept(c.Exempt...)(*c.Text)
			if got != c.Masked {
				t.Errorf("case %d (exempt %v): text masked differently\n%s", i, c.Exempt, firstDiff(got, c.Masked))
			}
			if again := CredentialsExcept(c.Exempt...)(got); again != got {
				t.Errorf("case %d: masking the masked text changed it\n%s", i, firstDiff(again, got))
			}
			continue
		}
		docs++
		got := CredentialsData(c.Data, c.Exempt...)
		if !reflect.DeepEqual(got, c.MaskedData) {
			a, _ := json.Marshal(got)
			b, _ := json.Marshal(c.MaskedData)
			t.Errorf("case %d (exempt %v): data masked differently\n%s", i, c.Exempt, firstDiff(string(a), string(b)))
		}
		if again := CredentialsData(got, c.Exempt...); !reflect.DeepEqual(again, got) {
			t.Errorf("case %d: masking the masked data changed it", i)
		}
	}
	if texts == 0 || docs == 0 {
		t.Errorf("the vectors held %d texts and %d documents; both forms must be checked", texts, docs)
	}
}

func firstDiff(got, want string) string {
	i := 0
	for i < len(got) && i < len(want) && got[i] == want[i] {
		i++
	}
	clip := func(s string) string {
		lo, hi := i-60, i+60
		if lo < 0 {
			lo = 0
		}
		if hi > len(s) {
			hi = len(s)
		}
		if lo > hi {
			return ""
		}
		return s[lo:hi]
	}
	return "  at byte " + strconv.Itoa(i) + "\n  got:  " + clip(got) + "\n  want: " + clip(want)
}

// Credential-shaped samples are built by concatenation, so this file is not one a scanner reads as
// carrying live-looking keys. One per shape, in CredentialShapes order.
var samples = []string{
	"sk-ant-" + "api03-abcdefghijklmnopqrstu",
	"ghp_" + "abcdefghijklmnopqrstuvwxyz0123",
	"github_pat_" + "abcdefghijklmnopqrstuvwxyz_0123",
	"glpat-" + "abcdefghijklmnopqrstu",
	"AKIA" + "ABCDEFGHIJKLMNOP",
	"xoxb-" + "1234567890-abc",
	"eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
	"Authorization: Bearer " + "abc123def456",
	"postgres://u:" + "pw1234@h/db",
	"https://x.io/mcp?api_key=" + "abc123&x=1",
}

// goLine is a value as Go writes it on a tape or a trace, read as the tripwire reads it.
func goLine(v any) []byte {
	b, _ := json.Marshal(v)
	return serial.ForbidView(b)
}

func forbidShapes(t *testing.T) []*regexp.Regexp {
	t.Helper()
	var out []*regexp.Regexp
	for _, p := range CredentialShapes {
		re, err := regexp.Compile(p)
		if err != nil {
			t.Fatalf("a credential shape does not compile as a Forbid pattern: %q: %v", p, err)
		}
		out = append(out, re)
	}
	return out
}

// The pairing a Boundary declares: Scrub masks, Forbid refuses what survived. Whatever the scrub
// leaves, no shape fires on, read as the JSON line a tape or trace writes; and every shape still
// fires on its raw credential, so the tripwire is armed.
func TestTheShapesAsForbidPassAScrubbedTextAndCatchARawOne(t *testing.T) {
	res := forbidShapes(t)
	if len(res) != len(samples) {
		t.Fatalf("%d shapes and %d samples", len(res), len(samples))
	}
	for i, re := range res {
		if !re.MatchString(samples[i]) {
			t.Errorf("shape %d does not fire on its raw credential %q", i, samples[i])
		}
		line := goLine(map[string]any{"note": samples[i]})
		if !re.Match(line) {
			t.Errorf("shape %d does not fire on its credential inside a JSON line", i)
		}
		if masked := Credentials(samples[i]); masked == samples[i] || !Mask.MatchString(masked) {
			t.Errorf("the scrub left sample %d as it was: %q", i, masked)
		}
	}
	_, cases := vectors(t)
	for i, c := range cases {
		var masked string
		if c.Text != nil {
			masked = CredentialsExcept(c.Exempt...)(*c.Text)
		} else {
			masked = string(goLine(CredentialsData(c.Data, c.Exempt...)))
		}
		line := goLine(map[string]any{"v": masked})
		for j, re := range res {
			if re.MatchString(masked) || re.Match(line) {
				t.Errorf("case %d: shape %d fires on what the scrub left", i, j)
			}
		}
	}
	for _, s := range samples {
		for j, re := range res {
			if re.MatchString(Credentials(s)) {
				t.Errorf("shape %d fires on the scrubbed sample %q", j, Credentials(s))
			}
		}
	}
}

// A Forbid shape and the scrub's shape are one rule written twice, because RE2 has no lookaround:
// on a text holding one candidate, the shape fires exactly when the scrub masks it. The texts sit
// on each lookaround's edge.
func TestAForbidShapeFiresExactlyWhenTheScrubMasks(t *testing.T) {
	res := forbidShapes(t)
	texts := []string{
		"x" + samples[0], "-" + samples[0], "." + samples[0], "é" + samples[0], samples[0],
		"_" + samples[1], " " + samples[1], "a" + samples[4], samples[4] + "a", samples[4] + "_",
		"0" + samples[4], samples[4] + "!", "AKIA" + "ABCDEFGHIJKLMNO",
		"Bearer abcdefgh", "Bearer abcdefg1", "Bearer 1234567", "Bearer 12345678", "xBearer abc123def",
		"_Bearer abc123def", "Basic\vabc123def", "Basic abc123def", "Bearer ab.cd-ef/1",
		"Bearer Bearer abc123def456", "Bearer abcdefgh 1", "Bearer\n\t abcdefghijklmn9",
		"://u:${PW}@h", "://u:%PW%@h", "://u:{x}@h", "://u:p$w@h", "://:pw@h", "://u:pw@", "://u:pw",
		"://u:pw h@x", "?token=abc", "&x-auth-y=1", "?sig=", "?Key=1", "?api_key=a#b",
		"xoxp-123456789", "xoxp-1234567890", "eyJabcdefghij.abcdefghij.abcdefghij",
		"eyJabcdefghij.abcdefghi.abcdefghij", "github_pat_" + "abcdefghijklmnopqrs",
	}
	for _, text := range texts {
		fires := false
		for _, re := range res {
			if re.MatchString(text) {
				fires = true
			}
		}
		if masks := Credentials(text) != text; masks != fires {
			t.Errorf("%q: the scrub masks=%v but a Forbid shape fires=%v", text, masks, fires)
		}
	}
}

func TestASecretIsTheFirst8HexOfTheSha256(t *testing.T) {
	// sha256("abc") = ba7816bf...
	if got := Secret("abc"); got != "<secret:ba7816bf>" {
		t.Errorf("Secret(abc) = %q", got)
	}
	if !Mask.MatchString(Secret("é")) {
		t.Errorf("a non-ASCII value does not mask to the mask's shape")
	}
}

func TestTheFieldNamesThatHoldCredentials(t *testing.T) {
	for name, want := range map[string]bool{
		"api_key": true, "KEYWORDS": false, "keyboard": false, "keyword_key": true, "Authorization": true,
		"author": false, "AUTHOR": false, "auth": true, "oauth_token": true, "password": true,
		"session_id": true, "SessionCookie": true, "bearer": true, "credentials": true, "secret": true,
		"command": false, "url": false, "monkey": true, "keys": true, "authorize": true, "authored": false,
		"KEY": false, // the Kelvin sign is not a k: the patterns are ASCII
	} {
		if got := SecretField(name); got != want {
			t.Errorf("SecretField(%q) = %v", name, got)
		}
	}
	if SecretField("key", "key") || !SecretField("api_key", "key") {
		t.Errorf("an exemption must keep exactly the field it names")
	}
}

func TestADataValueUnderACredentialFieldIsMaskedWholeAndItsTypesKept(t *testing.T) {
	got := CredentialsData(map[string]any{
		"password": "hunter22", "url": "https://u:pw1234@h/x", "n": 3.0, "ok": true, "nil": nil,
		"list":    []any{"x", map[string]any{"token": "abc"}},
		"env_ref": map[string]any{"token": "${TOKEN}", "secret": "", "key": Secret("x")},
		"strs":    map[string]string{"api_key": "abc"},
		"strlist": []string{"Bearer abc123def456"},
	})
	want := map[string]any{
		"password": Secret("hunter22"), "url": "https://u:" + Secret("pw1234") + "@h/x", "n": 3.0, "ok": true,
		"nil": nil, "list": []any{"x", map[string]any{"token": Secret("abc")}},
		"env_ref": map[string]any{"token": "${TOKEN}", "secret": "", "key": Secret("x")},
		"strs":    map[string]string{"api_key": Secret("abc")},
		"strlist": []string{"Bearer " + Secret("abc123def456")},
	}
	if !reflect.DeepEqual(got, want) {
		a, _ := json.Marshal(got)
		b, _ := json.Marshal(want)
		t.Errorf("got  %s\nwant %s", a, b)
	}
	if !strings.Contains(Credentials(`{"password": "hunter22"}`), Secret("hunter22")) {
		t.Errorf("the text form disagrees with the data form")
	}
}
