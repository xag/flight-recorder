package flightrecorder

import (
	"context"
	"errors"
	"os"
	"path/filepath"
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

// The re-write path. Mutation exists precisely to EDIT recorded values, so a tape that passed the
// tripwire when it was written can have a credential put into it by hand — and saving it was, until
// now, the one way to get a forbidden value onto a tape with nothing looking.
func TestForbidRefusesToSaveAMutationThatIntroducesIt(t *testing.T) {
	path := recordToySession(t)
	r, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Forbid(`\bAKIA[0-9A-Z]{16}\b`); err != nil {
		t.Fatal(err)
	}

	// Clean as recorded: arming the tripwire must not, by itself, condemn an innocent tape.
	clean := filepath.Join(t.TempDir(), "clean.jsonl")
	if err := r.Save(clean); err != nil {
		t.Fatalf("an unmutated tape was refused: %v", err)
	}

	db := r.Call(0).Event("db", 0)
	if db == nil {
		t.Fatal("no db read to mutate")
	}
	db["res"] = map[string]any{"id": "u1", "exists": true,
		"data": map[string]any{"name": "AKIAABCDEFGHIJKLMNOP"}}

	out := filepath.Join(t.TempDir(), "mutated.jsonl")
	saveErr := r.Save(out)

	var fv *ForbiddenValue
	if !errors.As(saveErr, &fv) {
		t.Fatalf("the mutated tape was saved anyway (err=%v)", saveErr)
	}
	// The claim is the file, not the error: a refusal that still leaves a half-written tape on
	// disk has leaked everything the guard was standing in front of.
	if _, err := os.Stat(out); !os.IsNotExist(err) {
		data, _ := os.ReadFile(out)
		t.Errorf("a refused Save left a file behind (err=%v):\n%s", err, data)
	}
	if strings.Contains(fv.Error(), "AKIAABCDEFGHIJKLMNOP") {
		t.Errorf("the tripwire quoted the secret it caught: %s", fv.Error())
	}
	if fv.Pattern != `\bAKIA[0-9A-Z]{16}\b` {
		t.Errorf("the refusal names the rule %q", fv.Pattern)
	}
	if !strings.Contains(fv.Error(), `call 0`) {
		t.Errorf("the refusal does not say which record carried it: %s", fv.Error())
	}
}

// Free when unused: a recording that armed nothing saves whatever it holds, exactly as it did
// before the tripwire existed.
func TestSaveWithoutForbidIsUnaffected(t *testing.T) {
	path := recordToySession(t)
	r, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	r.Call(0).Event("db", 0)["res"] = map[string]any{"id": "u1", "exists": true,
		"data": map[string]any{"name": "AKIAABCDEFGHIJKLMNOP"}}

	out := filepath.Join(t.TempDir(), "mutated.jsonl")
	if err := r.Save(out); err != nil {
		t.Fatalf("a recording with no tripwire refused to save: %v", err)
	}
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "AKIAABCDEFGHIJKLMNOP") {
		t.Errorf("the mutation did not reach the saved tape at all, so this proves nothing:\n%s", data)
	}
}

// A bad pattern fails at declaration time, the way New's does — not on the save, hours later, when
// the tripwire turns out to have been guarding nothing.
func TestRecordingForbidRejectsABadPattern(t *testing.T) {
	r, err := LoadTape(fixturePlainToy(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Forbid("AKIA[unclosed"); err == nil {
		t.Fatal("a bad forbid pattern was accepted")
	} else if !strings.Contains(err.Error(), "bad forbid pattern") {
		t.Errorf("unhelpful error for a bad pattern: %v", err)
	}
}
