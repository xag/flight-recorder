package serial

import (
	"encoding/json"
	"testing"
)

// ForbidView reads a line written by Go's encoder as the characters it holds: the HTML escapes and
// the two separators undone, everything else (an escaped backslash before a u-sequence included)
// as written.
func TestForbidViewReadsTheCharactersGosEncoderEscapes(t *testing.T) {
	for _, s := range []string{"<a>&b", "x\u2028y\u2029z", `\u003c and \u2028 as text`, "plain"} {
		line, err := json.Marshal(map[string]any{"v": s})
		if err != nil {
			t.Fatal(err)
		}
		got := string(ForbidView(line))
		var back map[string]any
		if err := json.Unmarshal(line, &back); err != nil || back["v"] != s {
			t.Fatalf("the test line does not round-trip: %v", err)
		}
		// Every character the text holds is in the view as itself, except a backslash or a
		// quote, which keep their JSON escapes.
		expect := `{"v":"` + jsonEscapeOnlyQuoteAndBackslash(s) + `"}`
		if got != expect {
			t.Errorf("%q: the tripwire reads %s, want %s", s, got, expect)
		}
	}
}

func jsonEscapeOnlyQuoteAndBackslash(s string) string {
	out := []rune{}
	for _, r := range s {
		if r == '\\' || r == '"' {
			out = append(out, '\\')
		}
		out = append(out, r)
	}
	return string(out)
}
