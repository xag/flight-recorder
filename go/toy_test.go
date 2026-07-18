package flightrecorder

import (
	"context"
	"errors"
)

// The toy program under test. It is deliberately shared between the record test and the replay
// test: replay's whole claim is that the SAME code, fed the recorded answers, reproduces the
// recorded run — so both must run this identical body, not two lookalikes.

func toyGreet(ctx context.Context) (any, error) {
	id := "t@example.com"
	snap, _ := QueryOne(ctx, "get", `collection("users").document("t@example.com")`,
		func() (Snapshot, error) {
			return Snapshot{ID: &id, Exists: true, Data: map[string]any{"name": "alpha", "x": 1}}, nil
		})
	_ = SampleIndices(ctx, 3, 2)
	_ = Now(ctx)
	_ = Perf(ctx)
	if _, err := RandBytes(ctx, 4); err != nil {
		return nil, err
	}
	out, _ := Effect(ctx, "app.fetch", []any{"abc"}, func() (map[string]any, error) {
		return map[string]any{"key": "abc", "v": 30}, nil
	})
	name, _ := snap.Data.(map[string]any)["name"].(string)
	Note(ctx, "greeted", map[string]any{"who": out["key"]})
	return "hello " + name, nil
}

func toyWork(ctx context.Context) (any, error) {
	return nil, Span(ctx, "assign", map[string]any{"n": 1}, func(ctx context.Context) error {
		// The effect errors, but the span handles it and completes ok — so the call has no
		// top-level error, and the span's end carries outcome "ok".
		_, _ = Effect(ctx, "app.maybe", []any{7}, func() (int, error) {
			return 0, errors.New("kaput")
		})
		_ = RandFloat(ctx)
		_ = RandIntn(ctx, 100)
		return Exec(ctx, "set", `collection("users").document("t@example.com")`,
			[]any{map[string]any{"seen": true}}, func() error { return nil })
	})
}

func toyResolver(fn string, kwargs map[string]any) (func(context.Context) (any, error), error) {
	switch fn {
	case "greet":
		return toyGreet, nil
	case "work":
		return toyWork, nil
	default:
		return nil, errors.New("unknown fn: " + fn)
	}
}
