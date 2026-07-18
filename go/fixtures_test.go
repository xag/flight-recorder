package flightrecorder

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
	"github.com/xag/flight-recorder/go/spec"
)

// The Go implementation's contributed fixtures. Two tapes join spec/fixtures/, produced by this
// recorder, so Go takes its place in the cross-runtime conformance sweep: every checker
// (validate.py, validate.js, validate.go) validates every fixture, and every fixture was
// produced by an implementation. `go-sem-toy.jsonl` reproduces the universal `enrol` scenario
// the Node and Python sem fixtures also carry — same semantic tree, same store.get/set/boom leaf
// effects — so a reader recovers the same account no matter which runtime wrote the tape.

// ToyError is a structured error carrying its own args, like the ToyError in the JS/Python toys.
type ToyError struct {
	Msg string
	N   int
}

func (e *ToyError) Error() string { return e.Msg }
func (e *ToyError) Args() []any   { return []any{e.Msg, e.N} }

func sp(s string) *string { return &s }

// The plain toy: fx (success and error), a chained read and write, both clocks, and all four
// random shapes — a rich basic tape, the Go counterpart of node-toy.jsonl / python-toy.jsonl.
func fixturePlainToy(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	rec, err := New(dir, Boundary{
		Constants: map[string]any{"toy.LIMIT": 3},
		Redact:    serial.Rules{"password": nil},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	if _, err := rec.Call(ctx, "greet", map[string]any{"user": "alice"}, func(ctx context.Context) (any, error) {
		row, _ := Effect(ctx, "store.get", []any{"alice"}, func() (map[string]any, error) {
			return map[string]any{"name": "Alice", "x": 3}, nil
		})
		_, _ = Query(ctx, "stream", `collection("users").where("x", ">", 0)`, func() ([]Snapshot, error) {
			return []Snapshot{
				{ID: sp("0"), Exists: true, Data: map[string]any{"name": "alpha", "x": 1}},
				{ID: sp("1"), Exists: true, Data: map[string]any{"name": "beta", "x": 2}},
			}, nil
		})
		_ = SampleIndices(ctx, 3, 2)
		if _, err := RandBytes(ctx, 4); err != nil {
			return nil, err
		}
		_ = RandFloat(ctx)
		_ = RandIntn(ctx, 100)
		at := Now(ctx)
		_ = Perf(ctx)
		_ = Exec(ctx, "set", `store.set(greeted:alice)`,
			[]any{map[string]any{"at": at}}, func() error { return nil })
		name, _ := row["name"].(string)
		return map[string]any{"name": name}, nil
	}); err != nil {
		t.Fatal(err)
	}

	// A tool that itself fails after an effect that raised: the fx.err branch and call.error.
	_, _ = rec.Call(ctx, "explode", map[string]any{"user": "ghost"}, func(ctx context.Context) (any, error) {
		_, err := Effect(ctx, "store.boom", []any{"ghost"}, func() (any, error) {
			return nil, &ToyError{Msg: "no such key: ghost", N: 42}
		})
		return nil, err
	})

	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(rec.Path())
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// toyEnrol is the universal sem scenario: a clock read that belongs to the call, then a span
// enclosing a nested span (load_corpus), a point note, a span whose body raises (register, ending
// with outcome "error", the error caught by the caller), a second point note, and span data
// carrying both a value marker (a datetime) and a value redaction must reach (a password).
func toyEnrol(ctx context.Context) (any, error) {
	at := Now(ctx) // read while the span's args are evaluated — it belongs to the call
	var result any
	err := Span(ctx, "enrol", map[string]any{"user": "alice", "started": at, "password": "hunter2"},
		func(ctx context.Context) error {
		// A chained read, not an effect: the canonical scenario puts a `db` event inside a span,
		// which is the one enclosure a reader most wants to see and the one an fx-only span
		// never demonstrates.
		var snap Snapshot
		_ = Span(ctx, "load_corpus", nil, func(ctx context.Context) error {
			id := "alice"
			snap, _ = QueryOne(ctx, "get", `collection("users").document("alice")`,
				func() (Snapshot, error) {
					return Snapshot{ID: &id, Exists: true,
						Data: map[string]any{"name": "Alice", "x": 3}}, nil
				})
			return nil
		})
		Note(ctx, "corpus_read", map[string]any{"found": snap.Exists})

		regErr := Span(ctx, "register", map[string]any{"password": "hunter2"}, func(ctx context.Context) error {
			if _, e := Effect(ctx, "store.set", []any{"user:alice", map[string]any{"password": "hunter2"}},
				func() (string, error) { return "OK", nil }); e != nil {
				return e
			}
			_, e := Effect(ctx, "store.boom", []any{"alice"}, func() (any, error) {
				return nil, &ToyError{Msg: "no such key: alice", N: 42}
			})
			return e // the span ends with outcome "error"
		})
		if regErr != nil {
			Note(ctx, "registration_failed", map[string]any{"why": regErr.Error()})
		}

		data, _ := snap.Data.(map[string]any)
		name, _ := data["name"].(string)
		result = map[string]any{"user": "alice", "name": name}
		return nil
	})
	return result, err
}

func fixtureSemToy(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	rec, err := New(dir, Boundary{Redact: serial.Rules{"password": nil}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Call(context.Background(), "enrol",
		map[string]any{"user": "alice", "password": "hunter2"}, toyEnrol); err != nil {
		t.Fatal(err)
	}
	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(rec.Path())
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// The scenarios must stay conformant on every run, whether or not the committed fixtures are
// regenerated — so this is not gated. It also checks the sem tape exercises every phase and both
// outcomes and carries a value marker in sem data, the same bar the JS/Python sem tests hold.
func TestFixtureScenariosConform(t *testing.T) {
	toy := fixturePlainToy(t)
	if v := spec.ValidateTape(toy); len(v) > 0 {
		t.Errorf("go-toy scenario is not conformant: %v", v)
	}

	sem := fixtureSemToy(t)
	if v := spec.ValidateTape(sem); len(v) > 0 {
		t.Errorf("go-sem-toy scenario is not conformant: %v", v)
	}
	for _, marker := range []string{`"phase":"begin"`, `"phase":"end"`, `"phase":"point"`,
		`"outcome":"ok"`, `"outcome":"error"`, `"__dt__"`} {
		if !strings.Contains(sem, marker) {
			t.Errorf("go-sem-toy scenario missing %s — it must exercise every sem shape", marker)
		}
	}
	if strings.Contains(sem, "hunter2") {
		t.Errorf("the password rode the sem tape in the clear")
	}
}

// Regenerate the committed Go fixtures. Gated, like the JS/Python regen: it overwrites bytes
// under version control, so it runs only when asked.
func TestRegenFixtures(t *testing.T) {
	if os.Getenv("FR_REGEN_FIXTURES") == "" {
		t.Skip("set FR_REGEN_FIXTURES=1 to regenerate the committed Go fixtures")
	}
	fixtures := filepath.Join("..", "spec", "fixtures")
	for name, text := range map[string]string{
		"go-toy.jsonl":     fixturePlainToy(t),
		"go-sem-toy.jsonl": fixtureSemToy(t),
	} {
		if v := spec.ValidateTape(text); len(v) > 0 {
			t.Fatalf("refusing to write a non-conformant %s: %v", name, v)
		}
		if err := os.WriteFile(filepath.Join(fixtures, name), []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("wrote %s (%d bytes)", name, len(text))
	}
}
