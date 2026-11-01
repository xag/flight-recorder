package flightrecorder

// A replay masks the replayed side as the recording masked the recorded one. The tape holds an
// effect's args, a write's args and the call's result after the boundary's Redact and Scrub; the
// replayed code produces them raw. Compared raw against the tape, a masked recording diverged on
// its own replay. Python's replay_call masks the replayed side with the same boundary first.

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
)

const replaySecret = "s3cret-value-77"

var maskingBoundary = Boundary{
	Redact: serial.Rules{"token": nil},
	Scrub:  func(s string) string { return strings.ReplaceAll(s, replaySecret, "[S]") },
}

// sendsASecret puts the secret into an effect's args, a write's args and its result: by name
// (`token`) where a Redact rule reaches it, and in prose where only the Scrub does.
func sendsASecret(ctx context.Context) (any, error) {
	if _, err := Effect(ctx, "app.send", []any{"Bearer " + replaySecret, map[string]any{"token": "abc"}},
		func() (string, error) { return "sent", nil }); err != nil {
		return nil, err
	}
	if err := Exec(ctx, "set", `doc("u/1")`, []any{map[string]any{"token": "abc", "note": replaySecret}},
		func() error { return nil }); err != nil {
		return nil, err
	}
	return map[string]any{"note": "used " + replaySecret, "token": "abc"}, nil
}

func sendsASecretResolver(fn string, kwargs map[string]any) (func(context.Context) (any, error), error) {
	if fn != "sends" {
		return nil, errors.New("unknown fn: " + fn)
	}
	return sendsASecret, nil
}

func recordMasked(t *testing.T) string {
	t.Helper()
	rec, err := New(t.TempDir(), maskingBoundary)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Call(context.Background(), "sends", map[string]any{}, sendsASecret); err != nil {
		t.Fatal(err)
	}
	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}
	return rec.Path()
}

func TestAMaskedRecordingReplaysWithItsBoundary(t *testing.T) {
	path := recordMasked(t)
	rep, err := Replay(path, 0, sendsASecretResolver, maskingBoundary)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.OK() {
		t.Fatalf("a masked recording did not reproduce under its own boundary: divergence=%q "+
			"resultMatch=%v writeDivs=%v consumed=%d/%d", rep.Divergence, rep.ResultMatch, rep.WriteDivs,
			rep.EventsConsumed, rep.EventsTotal)
	}
	// What invariants judge is the masked side, the only one the tape holds.
	for _, v := range []any{rep.ReplayedResult, rep.Writes} {
		if s := jsonString(v); strings.Contains(s, replaySecret) || strings.Contains(s, `"abc"`) {
			t.Errorf("the replayed side handed to invariants is unmasked: %s", s)
		}
	}

	// The same through the invariant and call-view entry points.
	inv, err := CheckInvariants(path, 0, sendsASecretResolver, nil, maskingBoundary)
	if err != nil || !inv.Replay.OK() {
		t.Errorf("CheckInvariants with the boundary: err=%v divergence=%q", err, inv.Replay.Divergence)
	}
	rec, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	cv, err2 := ReplayCall(rec.Call(0), sendsASecretResolver, false, maskingBoundary)
	if err2 != nil || !cv.OK() {
		t.Errorf("ReplayCall with the boundary: err=%v divergence=%q", err2, cv.Divergence)
	}
}

// Without the boundary nothing is masked, as before: the same tape diverges on the effect's args.
// This is what made the test above red, and it is kept so the boundary is seen to be the reason.
func TestAMaskedRecordingReplayedWithoutItsBoundaryDiverges(t *testing.T) {
	rep, err := Replay(recordMasked(t), 0, sendsASecretResolver)
	if err != nil {
		t.Fatal(err)
	}
	if rep.OK() || !strings.Contains(rep.Divergence, "app.send") {
		t.Errorf("an unmasked replay of a masked tape matched: divergence=%q", rep.Divergence)
	}
}
