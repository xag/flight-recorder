package flightrecorder

// The stock credential scrub as a Boundary declares it: Scrub masks, Forbid refuses what survived.
//
// Go's encoder writes <, > and & as <, > and &, and the tripwire used to read those
// bytes. A masked `?api_key=<secret:…>` then read as an unmasked value and refused a recording the
// scrub had already cleaned, and a credential right after a `<` escaped every shape whose
// lookbehind reads the character before it. The tripwire reads the characters, as every other
// runtime's does.

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/xag/flight-recorder/go/scrubs"
	"github.com/xag/flight-recorder/go/serial"
)

// Built by concatenation, so this file is not one a scanner reads as carrying a live-looking key.
var proseKey = "sk-" + "proj-0123456789abcdefghijklmnop"

func recordOne(t *testing.T, b Boundary, kwargs map[string]any) (string, error) {
	t.Helper()
	dir := t.TempDir()
	rec, err := New(dir, b)
	if err != nil {
		t.Fatal(err)
	}
	_, callErr := rec.Call(context.Background(), "connect", kwargs,
		func(ctx context.Context) (any, error) { return "ok", nil })
	rec.Close()
	data, _ := os.ReadFile(rec.Path())
	return string(data), callErr
}

func TestABoundaryDeclaringTheCredentialScrubRecordsAndWritesNoCredential(t *testing.T) {
	tape, err := recordOne(t, Boundary{Scrub: scrubs.Credentials, Forbid: scrubs.CredentialShapes},
		map[string]any{
			"url":  "https://x.io/mcp?api_key=abc123&x=1",
			"note": "<" + proseKey + "> & Bearer abc123def456",
		})
	if err != nil {
		t.Fatalf("the tripwire refused a call the scrub had cleaned: %v", err)
	}
	for _, raw := range []string{proseKey, "abc123def456", "api_key=abc123"} {
		if strings.Contains(tape, raw) {
			t.Errorf("the tape holds %q", raw)
		}
	}
	if !scrubs.Mask.Match(serial.ForbidView([]byte(tape))) {
		t.Errorf("the tape holds no mask")
	}
}

func TestAForbidShapeCatchesACredentialRightAfterAnEscapedCharacter(t *testing.T) {
	for _, note := range []string{"<" + proseKey + ">", "a=1&token=abc123", "<Bearer abc123def456>"} {
		_, err := recordOne(t, Boundary{Forbid: scrubs.CredentialShapes}, map[string]any{"note": note})
		var fv *ForbiddenValue
		if !errors.As(err, &fv) {
			t.Errorf("a raw credential in %q was recorded past the tripwire (err=%v)", note, err)
		}
	}
	// And a pattern of the app's own, written against the characters.
	_, err := recordOne(t, Boundary{Forbid: []string{`<script>`}}, map[string]any{"html": "<script>x</script>"})
	var fv *ForbiddenValue
	if !errors.As(err, &fv) {
		t.Errorf("a Forbid pattern holding < never fires (err=%v)", err)
	}
}
