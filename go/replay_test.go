package flightrecorder

import (
	"context"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
)

func recordToySession(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	rec, err := New(dir, Boundary{Redact: serial.Rules{"token": nil}})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := rec.Call(ctx, "greet",
		map[string]any{"email": "t@example.com", "token": "secret"}, toyGreet); err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Call(ctx, "work", map[string]any{}, toyWork); err != nil {
		t.Fatal(err)
	}
	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}
	return rec.Path()
}

// The core replay claim: the same code, fed the recorded answers, reproduces the recorded run —
// every event consumed, result and error matching, no divergence, and the same account of what
// it was doing.
func TestReplayRoundTripMatches(t *testing.T) {
	path := recordToySession(t)
	for i, name := range []string{"greet", "work"} {
		rep, err := Replay(path, i, toyResolver)
		if err != nil {
			t.Fatalf("call %d (%s): %v", i, name, err)
		}
		if !rep.OK() {
			t.Errorf("call %d (%s) did not reproduce: divergence=%q resultMatch=%v errorMatch=%v "+
				"consumed=%d/%d writeDivs=%v", i, name, rep.Divergence, rep.ResultMatch, rep.ErrorMatch,
				rep.EventsConsumed, rep.EventsTotal, rep.WriteDivs)
		}
		if rep.SemDivergence != "" {
			t.Errorf("call %d (%s) semantic divergence: %s", i, name, rep.SemDivergence)
		}
	}
}

// A replay that takes a different path must be caught, not silently pass: the first boundary
// question the code asks disagrees with the recording.
func TestReplayDetectsDivergence(t *testing.T) {
	path := recordToySession(t)
	diverged := func(fn string, kwargs map[string]any) (func(context.Context) (any, error), error) {
		return func(ctx context.Context) (any, error) {
			// greet's first recorded event is a db read; asking for an effect instead diverges.
			_, _ = Effect(ctx, "app.DIFFERENT", []any{"abc"}, func() (map[string]any, error) {
				return nil, nil
			})
			return "x", nil
		}, nil
	}
	rep, err := Replay(path, 0, diverged)
	if err != nil {
		t.Fatal(err)
	}
	if rep.OK() {
		t.Errorf("expected a divergence, replay reported OK")
	}
	if rep.Divergence == "" {
		t.Errorf("expected a boundary-divergence message, got none")
	}
}

// A replay whose result differs is not OK, even when every boundary question still matches.
func TestReplayDetectsResultMismatch(t *testing.T) {
	path := recordToySession(t)
	mismatch := func(fn string, kwargs map[string]any) (func(context.Context) (any, error), error) {
		if fn != "greet" {
			return toyResolver(fn, kwargs)
		}
		return func(ctx context.Context) (any, error) {
			r, err := toyGreet(ctx) // asks the same questions, in the same order...
			_ = r
			return "a different answer", err // ...but returns something else
		}, nil
	}
	rep, err := Replay(path, 0, mismatch)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Divergence != "" {
		t.Fatalf("did not expect a boundary divergence, got %q", rep.Divergence)
	}
	if rep.ResultMatch {
		t.Errorf("expected a result mismatch, replay reported a match")
	}
	if rep.OK() {
		t.Errorf("replay reported OK despite a different result")
	}
}
