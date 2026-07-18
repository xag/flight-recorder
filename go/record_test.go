package flightrecorder

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
	"github.com/xag/flight-recorder/go/spec"
)

// The recorder's whole point: its output must satisfy the frozen tape-v1 checker. This records
// the toy session (every event kind) and asserts the tape is conformant — the same bar the
// Python and .NET recorders clear.
func TestRecordEmitsConformantTape(t *testing.T) {
	dir := t.TempDir()
	rec, err := New(dir, Boundary{
		Constants: map[string]any{"app.MODE": "test"},
		Redact:    serial.Rules{"token": nil}, // bare rule → [REDACTED]
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	if _, err := rec.Call(ctx, "greet",
		map[string]any{"email": "t@example.com", "token": "secret-should-not-appear"}, toyGreet); err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Call(ctx, "work", map[string]any{}, toyWork); err != nil {
		t.Fatal(err)
	}
	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(rec.Path())
	if err != nil {
		t.Fatal(err)
	}
	if v := spec.ValidateTape(string(data)); len(v) > 0 {
		t.Fatalf("recorded tape is NOT conformant: %d violation(s):\n  %s\n--- tape ---\n%s",
			len(v), strings.Join(v, "\n  "), data)
	}
	if strings.Contains(string(data), "secret-should-not-appear") {
		t.Errorf("redacted token leaked onto the tape:\n%s", data)
	}
	if !strings.Contains(string(data), serial.Redacted) {
		t.Errorf("expected a [REDACTED] marker in the tape, none found:\n%s", data)
	}
}

// The forbid tripwire: a value that must never reach a tape aborts the call, and neither it nor
// the credential is written.
func TestForbidTripwireAbortsCall(t *testing.T) {
	dir := t.TempDir()
	rec, err := New(dir, Boundary{Forbid: []string{`\bAKIA[0-9A-Z]{16}\b`}})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	_, callErr := rec.Call(ctx, "leaky", map[string]any{}, func(ctx context.Context) (any, error) {
		_, _ = Effect(ctx, "app.creds", []any{}, func() (string, error) {
			return "AKIAABCDEFGHIJKLMNOP", nil
		})
		return "unreached", nil
	})

	var fv *ForbiddenValue
	if !errors.As(callErr, &fv) {
		t.Fatalf("expected a ForbiddenValue, got %v", callErr)
	}
	rec.Close()

	data, _ := os.ReadFile(rec.Path())
	if strings.Contains(string(data), "AKIAABCDEFGHIJKLMNOP") {
		t.Errorf("the forbidden credential reached the tape:\n%s", data)
	}
	if strings.Contains(fv.Error(), "AKIAABCDEFGHIJKLMNOP") {
		t.Errorf("the tripwire quoted the secret it caught: %s", fv.Error())
	}
}
