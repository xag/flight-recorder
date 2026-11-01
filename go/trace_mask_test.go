package flightrecorder

// The trace is masked like the tape: the boundary's Redact and Scrub rewrite every traced payload
// (a call's args, a line's delta, a return's value) before the tripwire reads the line, and never
// the envelope (e, fn, at).
//
// Go writes its trace in a child process, and a func does not cross a process boundary. So the
// child arms the boundary's masking itself (MaskTrace), and what the parent's TraceSpec carries
// across is that masking was declared: a child that traces a payload without having armed it
// refuses its trace, as the tripwire does.

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
	"github.com/xag/flight-recorder/go/tracehook"
)

const localSecret = "hunter2-local"

// maskBoundary masks the credential in prose by value, the local named `secret` by name, and
// names three envelope keys in Redact to prove they are never touched. Its Forbid is the
// credential's shape: it must NOT fire, because it reads the line after the masking.
var maskBoundary = Boundary{
	Redact: serial.Rules{"secret": nil, "e": nil, "fn": nil, "at": nil},
	Scrub:  func(s string) string { return strings.ReplaceAll(s, tracedCredential, "[KEY]") },
	Forbid: []string{credentialPattern},
}

// TestTraceMaskChild is the instrumented half. It arms the boundary's masking only when the
// parent asks it to, so the same child can show what happens when it forgets.
func TestTraceMaskChild(t *testing.T) {
	if !Tracing() {
		t.Skip("the instrumented half of the trace masking tests; TestTracedMask* drives it")
	}
	if os.Getenv("FR_TEST_ARM_MASK") == "1" {
		MaskTrace(maskBoundary)
	}
	_ = toySecrets()

	path := os.Getenv(tracehook.EnvPath)
	data, err := os.ReadFile(path)
	holds := func(s string) bool {
		return strings.Contains(s, tracedCredential) || strings.Contains(s, localSecret)
	}
	live := fmt.Sprint(tracehook.Events())
	fmt.Printf("CHILD-TRACE-FILE-EXISTS %v\n", err == nil)
	fmt.Printf("CHILD-TRACE-HOLDS-SECRET %v\n", holds(string(data)))
	fmt.Printf("CHILD-LIVE-HOLDS-SECRET %v\n", holds(live))
	fmt.Printf("CHILD-ALIVE true\n")
}

func runMaskChild(t *testing.T, arm bool, forbid []string) (*TracedRun, error) {
	t.Helper()
	env := []string{"FR_TEST_ARM_MASK=0"}
	if arm {
		env = []string{"FR_TEST_ARM_MASK=1"}
	}
	return RunTraced(TraceSpec{
		Include: []string{"tracedtoy_test.go"},
		Command: []string{"test", "-run", "^TestTraceMaskChild$", "-count=1", "-v", "-vet=off", "."},
		Env:     env,
		Forbid:  forbid,
		Redact:  maskBoundary.Redact,
		Scrub:   maskBoundary.Scrub,
	})
}

var (
	maskOnce sync.Once
	maskRun  *TracedRun
	maskErr  error
)

func maskedChild(t *testing.T) *TracedRun {
	t.Helper()
	maskOnce.Do(func() { maskRun, maskErr = runMaskChild(t, true, maskBoundary.Forbid) })
	if maskErr != nil {
		t.Fatalf("the masked traced run failed: %v\n%s", maskErr, output(maskRun))
	}
	if maskRun.ExitErr != nil {
		t.Fatalf("the traced child failed: %v\n%s", maskRun.ExitErr, maskRun.Output)
	}
	return maskRun
}

func traceText(run *TracedRun) string {
	var b strings.Builder
	for _, ev := range run.Trace.Events {
		b.WriteString(jsonString(ev))
		b.WriteString("\n")
	}
	return b.String()
}

func TestTracedScrubMasksTheTraceAsItMasksTheTape(t *testing.T) {
	if Tracing() {
		t.Skip("this process is the instrumented child")
	}
	run := maskedChild(t)
	obs := run.Trace.Final("note")
	if obs == nil {
		t.Fatalf("note was never traced:\n%s", run.Trace.Timeline())
	}
	if s, _ := obs.Value.(string); !strings.Contains(s, "[KEY]") || strings.Contains(s, tracedCredential) {
		t.Errorf("the scrub did not mask the traced local: note=%v", obs.Value)
	}
	if rets := run.Trace.Returns("toySecrets"); len(rets) == 0 {
		t.Errorf("no return was traced:\n%s", run.Trace.Timeline())
	} else if s, _ := rets[0].Value.(string); strings.Contains(s, tracedCredential) {
		t.Errorf("the returned value reached the trace unmasked: %v", rets[0].Value)
	}
	if strings.Contains(traceText(run), tracedCredential) {
		t.Errorf("the credential reached the trace the parent read")
	}
	for _, line := range []string{"CHILD-TRACE-HOLDS-SECRET ", "CHILD-LIVE-HOLDS-SECRET "} {
		if got := childLine(t, run.Output, line); got != "false" {
			t.Errorf("%s%s", line, got)
		}
	}
	if got := childLine(t, run.Output, "CHILD-TRACE-FILE-EXISTS "); got != "true" {
		t.Errorf("the masked trace was not written (the tripwire must read the MASKED line)")
	}
}

func TestTracedRedactedFieldNameMasksTheLocalOfThatName(t *testing.T) {
	if Tracing() {
		t.Skip("this process is the instrumented child")
	}
	run := maskedChild(t)
	obs := run.Trace.Values("secret")
	if len(obs) == 0 {
		t.Fatalf("secret was never traced:\n%s", run.Trace.Timeline())
	}
	for _, o := range obs {
		if o.Value != serial.Redacted {
			t.Errorf("the local named by a redact rule was traced as %v at %s", o.Value, o.At)
		}
	}
	if strings.Contains(traceText(run), localSecret) {
		t.Errorf("the redacted local reached the trace")
	}
}

func TestTracedEnvelopeIsNotRedacted(t *testing.T) {
	if Tracing() {
		t.Skip("this process is the instrumented child")
	}
	run := maskedChild(t)
	kinds := map[string]bool{}
	for _, ev := range run.Trace.Events {
		kinds[str(ev["e"])] = true
		if !strings.Contains(str(ev["fn"]), "toySecrets") {
			t.Errorf("a rule named fn rewrote the envelope: %v", ev)
		}
		if at := str(ev["at"]); !strings.Contains(at, "tracedtoy_test.go:") {
			t.Errorf("a rule named at rewrote the envelope: %v", ev)
		}
	}
	for _, k := range []string{"C", "L", "R"} {
		if !kinds[k] {
			t.Errorf("no %q event in the trace; a rule named e took the envelope apart: %v", k, kinds)
		}
	}
}

// Declared but never armed: the parent said the boundary masks, the child traced a payload
// without arming it. The trace is refused, as the tripwire refuses one, and the parent hears it.
// Forbid is off here, so it is the masking check and nothing else that refuses.
func TestTracedMaskDeclaredButNeverArmedRefusesTheTrace(t *testing.T) {
	if Tracing() {
		t.Skip("this process is the instrumented child")
	}
	run, err := runMaskChild(t, false, nil)
	if !errors.Is(err, ErrTraceUnmasked) {
		t.Fatalf("a trace written without the declared masking came back as %v\n%s", err, output(run))
	}
	if got := childLine(t, run.Output, "CHILD-TRACE-FILE-EXISTS "); got != "false" {
		t.Errorf("the unmasked trace file survived")
	}
	if got := childLine(t, run.Output, "CHILD-LIVE-HOLDS-SECRET "); got != "false" {
		t.Errorf("the in-memory trace kept the unmasked events")
	}
	if run.Trace.Len() != 0 {
		t.Errorf("the parent was handed %d events from a refused trace", run.Trace.Len())
	}
	if run.ExitErr != nil {
		t.Errorf("the traced run failed rather than merely losing its trace: %v", run.ExitErr)
	}
}
