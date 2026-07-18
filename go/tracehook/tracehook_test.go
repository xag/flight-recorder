package tracehook

// The tripwire, tested where it actually fires.
//
// This package's state is per-process and armed once, on the first Live() — which is exactly what
// it must be, since a process either was started with the tracer armed or was not. So this file
// holds ONE test, written as a sequence, rather than several that would fight over the same
// globals. The cross-process half of the story — a parent's patterns reaching a child, and the
// child's refusal reaching the parent — is proved in the flightrecorder package's trace tests.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const credential = "AKIAABCDEFGHIJKLMNOP"

// A credential in a traced local must not reach the trace file, and the refusal must be legible to
// whoever started this process.
func TestForbiddenLocalIsRefusedAndTheTraceIsDestroyed(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "trace.jsonl")
	t.Setenv(EnvPath, path)
	t.Setenv(EnvForbid, `["\\bAKIA[0-9A-Z]{16}\\b", "hunter2"]`)

	if !Live() {
		t.Fatal("the tracer did not arm with a trace path and a valid tripwire")
	}

	// An innocent frame first, so what follows cannot be confused with "tracing never worked".
	f := Enter("Study", "app.go:10", []string{"level"}, 3)
	f.Line("app.go:11", []string{"level"}, 4)
	if data, err := os.ReadFile(path); err != nil || !strings.Contains(string(data), `"Study"`) {
		t.Fatalf("the clean frame was not traced (err=%v): %s", err, data)
	}

	// And now the line that must never land.
	f.Line("app.go:12", []string{"level", "token"}, 4, credential)

	if _, err := os.Stat(path); !os.IsNotExist(err) {
		data, _ := os.ReadFile(path)
		t.Fatalf("the trace file survived a refusal (err=%v) holding:\n%s", err, data)
	}
	if Live() {
		t.Error("the tracer went on tracing after a refusal — a later line could carry the same value")
	}
	if got := Refused(); got != `\bAKIA[0-9A-Z]{16}\b` {
		t.Errorf("the refusal names %q; it should name the pattern that hit", got)
	}
	if len(Events()) != 0 {
		t.Errorf("the in-memory trace still holds %d events after a refusal", len(Events()))
	}

	// The refusal is a FILE, because the process that armed the tripwire is not this one and
	// cannot catch anything thrown here.
	report, err := os.ReadFile(RefusalPath(path))
	if err != nil {
		t.Fatalf("no refusal was reported: %v", err)
	}
	if string(report) != `\bAKIA[0-9A-Z]{16}\b` {
		t.Errorf("the refusal file says %q", report)
	}
	// It names the RULE and never the match: this text ends up in logs.
	if strings.Contains(string(report), credential) {
		t.Errorf("the tripwire wrote the secret it caught into its own report")
	}

	// Nothing more is written, ever. A guard that only skipped the offending line would leave the
	// next observation of the same variable free to carry it.
	f.Line("app.go:13", []string{"token"}, credential)
	f.Leave("app.go:14")
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("the tracer resumed writing after refusing")
	}
}
