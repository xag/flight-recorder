package flightrecorder

// Go's encoder writes U+2028 and U+2029 as the six-character escapes \u2028 and \u2029, with or
// without HTML escaping. The tripwire read those bytes, so a credential right after a line or
// paragraph separator had a `8` or `9` before it, a word character, and slipped past every shape
// whose lookbehind reads the character before it. The tripwire reads the characters.

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/xag/flight-recorder/go/scrubs"
	"github.com/xag/flight-recorder/go/tracehook"
)

var separators = []string{"\u2028", "\u2029"}

func TestACredentialRightAfterALineSeparatorIsRefusedOnATape(t *testing.T) {
	for _, sep := range separators {
		_, err := recordOne(t, Boundary{Forbid: scrubs.CredentialShapes}, map[string]any{"note": sep + proseKey})
		var fv *ForbiddenValue
		if !errors.As(err, &fv) {
			t.Errorf("a credential after U+%04X was recorded past the tripwire (err=%v)", []rune(sep)[0], err)
		}
	}
}

// TestTraceSeparatorChild is the instrumented half: it traces a local holding a credential right
// after U+2028, and reports on the file it was writing.
func TestTraceSeparatorChild(t *testing.T) {
	if !Tracing() {
		t.Skip("the instrumented half of the separator trace test; TestTracedACredentialRightAfter* drives it")
	}
	_ = toySeparated()
	data, err := os.ReadFile(os.Getenv(tracehook.EnvPath))
	fmt.Printf("CHILD-TRACE-FILE-EXISTS %v\n", err == nil)
	fmt.Printf("CHILD-TRACE-HOLDS-SECRET %v\n", strings.Contains(string(data), separatedKey))
}

func TestTracedACredentialRightAfterALineSeparatorIsRefused(t *testing.T) {
	if Tracing() {
		t.Skip("this process is the instrumented child")
	}
	run, err := RunTraced(TraceSpec{
		Include: []string{"tracedtoy_test.go"},
		Command: []string{"test", "-run", "^TestTraceSeparatorChild$", "-count=1", "-v", "-vet=off", "."},
		Forbid:  scrubs.CredentialShapes,
	})
	var fv *ForbiddenValue
	if !errors.As(err, &fv) {
		t.Fatalf("a traced credential after U+2028 passed the tripwire (err=%v)\n%s", err, output(run))
	}
	if got := childLine(t, run.Output, "CHILD-TRACE-FILE-EXISTS "); got != "false" {
		t.Errorf("the trace file survived the refusal")
	}
	if got := childLine(t, run.Output, "CHILD-TRACE-HOLDS-SECRET "); got != "false" {
		t.Errorf("the credential reached the trace file")
	}
}
