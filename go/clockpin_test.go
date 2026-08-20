package flightrecorder

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// WithClockAt makes a recorded Now answer the instant plus the time since, and the tape carries
// that answer as its `now` event, exactly as if the machine's clock had said so. Running: two
// reads are two instants, in order, so an app stamping its writes with the clock still stamps
// them apart. The pin lives on the context it was set on: the outer context keeps the outer pin,
// or none.
func TestASetClockAnswersTheInstantRunningAndRecordsItAsNow(t *testing.T) {
	rec, err := New(t.TempDir(), Boundary{})
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 8, 16, 8, 0, 0, 0, time.UTC)
	next := at.AddDate(0, 0, 1)
	within := func(got, want time.Time) bool { d := got.Sub(want); return d >= 0 && d < 5*time.Second }
	tick := func(ctx context.Context) (any, error) { return Now(ctx).Format(time.RFC3339Nano), nil }

	pinned := WithClockAt(context.Background(), at)
	inner := WithClockAt(pinned, next)
	first := Now(pinned)
	if !within(first, at) {
		t.Fatalf("the set clock answered %v, want within 5s after %v", first, at)
	}
	if got := Now(inner); !within(got, next) {
		t.Fatalf("the inner pin answered %v", got)
	}
	time.Sleep(5 * time.Millisecond)
	later := Now(pinned)
	if !later.After(first) {
		t.Fatalf("a set clock runs; it does not stop: %v then %v", first, later)
	}
	if !within(later, at) {
		t.Fatalf("the outer context kept its pin but answered %v", later)
	}

	var seen []string
	for _, ctx := range []context.Context{pinned, inner, pinned} {
		v, err := rec.Call(ctx, "tick", map[string]any{}, tick)
		if err != nil {
			t.Fatal(err)
		}
		seen = append(seen, v.(string))
	}
	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}
	for i, prefix := range []string{"2026-08-16T08:00:0", "2026-08-17T08:00:0", "2026-08-16T08:00:0"} {
		if !strings.HasPrefix(seen[i], prefix) {
			t.Errorf("call %d answered %q, want %s...", i, seen[i], prefix)
		}
	}

	// Outside any pinned context the clock is the machine's again.
	if d := time.Since(Now(context.Background())); d < 0 || d > 5*time.Second {
		t.Errorf("unpinned Now is %v off the real clock", d)
	}

	data, err := os.ReadFile(rec.Path())
	if err != nil {
		t.Fatal(err)
	}
	tape, err := LoadTape(string(data))
	if err != nil {
		t.Fatal(err)
	}
	for i, w := range seen {
		if got := tape.Call(i).Event("now", 0)["v"]; got != w {
			t.Errorf("call %d recorded now.v = %v, want %q", i, got, w)
		}
	}
}
