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
// a session touching every event kind and asserts the tape is conformant — the same bar the
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

	// Call 1: a read, a sample draw, both clocks, bytes, an effect, and a point note.
	_, err = rec.Call(ctx, "greet",
		map[string]any{"email": "t@example.com", "token": "secret-should-not-appear"},
		func(ctx context.Context) (any, error) {
			id := "t@example.com"
			DBReadOne(ctx, "get", `collection("users").document("t@example.com")`,
				Snapshot{ID: &id, Exists: true, Data: map[string]any{"name": "alpha", "x": 1}})
			_ = SampleIndices(ctx, 3, 2)
			_ = Now(ctx)
			_ = Perf(ctx)
			if _, err := RandBytes(ctx, 4); err != nil {
				return nil, err
			}
			out, _ := Effect(ctx, "app.fetch", []any{"abc"}, func() (map[string]any, error) {
				return map[string]any{"key": "abc", "v": 30}, nil
			})
			Note(ctx, "greeted", map[string]any{"who": out["key"]})
			return "hello alpha", nil
		})
	if err != nil {
		t.Fatal(err)
	}

	// Call 2: a span enclosing an erroring effect, a float draw, and a write. The span must
	// still close (outcome ok — the effect error is handled), well-nested.
	_, _ = rec.Call(ctx, "work", map[string]any{}, func(ctx context.Context) (any, error) {
		return nil, Span(ctx, "assign", map[string]any{"n": 1}, func(ctx context.Context) error {
			_, _ = Effect(ctx, "app.maybe", []any{7}, func() (int, error) {
				return 0, errors.New("kaput")
			})
			_ = RandFloat(ctx)
			_ = RandIntn(ctx, 100)
			DBWrite(ctx, "set", `collection("users").document("t@example.com")`, []any{map[string]any{"seen": true}})
			return nil
		})
	})

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

// The forbid tripwire: a value that must never reach a tape aborts the call, and nothing about
// it (nor the credential) is written.
func TestForbidTripwireAbortsCall(t *testing.T) {
	dir := t.TempDir()
	rec, err := New(dir, Boundary{
		Forbid: []string{`\bAKIA[0-9A-Z]{16}\b`}, // an AWS-key-shaped value
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	_, callErr := rec.Call(ctx, "leaky", map[string]any{}, func(ctx context.Context) (any, error) {
		// This effect result carries an unredactable secret; recording it must trip the wire.
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
	// The message names the rule, never the match.
	if strings.Contains(fv.Error(), "AKIAABCDEFGHIJKLMNOP") {
		t.Errorf("the tripwire quoted the secret it caught: %s", fv.Error())
	}
}
