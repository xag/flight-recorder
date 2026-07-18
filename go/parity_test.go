package flightrecorder

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/xag/flight-recorder/go/serial"
	"github.com/xag/flight-recorder/go/spec"
)

// A gate that never admits a call leaves no session file; a declined call runs for real but is
// not recorded, and an admitted call is.
func TestGateDeclinesLeaveNoFile(t *testing.T) {
	dir := t.TempDir()
	rec, err := New(dir, Boundary{
		Enabled: func(fn string, kwargs map[string]any) bool { return fn == "work" },
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	if _, err := rec.Call(ctx, "greet", map[string]any{"user": "alice"}, toyGreet); err != nil {
		t.Fatal(err)
	}
	if rec.Path() != "" {
		t.Errorf("a declined call opened a session file: %s", rec.Path())
	}

	if _, err := rec.Call(ctx, "work", map[string]any{}, toyWork); err != nil {
		t.Fatal(err)
	}
	if rec.Path() == "" {
		t.Fatal("an admitted call opened no session file")
	}
	rec.Close()

	data, _ := os.ReadFile(rec.Path())
	if strings.Contains(string(data), `"fn":"greet"`) {
		t.Errorf("the declined call was recorded:\n%s", data)
	}
	if !strings.Contains(string(data), `"fn":"work"`) {
		t.Errorf("the admitted call was not recorded:\n%s", data)
	}
	if v := spec.ValidateTape(string(data)); len(v) > 0 {
		t.Errorf("gated tape is not conformant: %v", v)
	}
}

type memSink struct {
	mu   sync.Mutex
	last []byte
	n    int
}

func (s *memSink) Publish(name string, data []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.last = data
	s.n++
}

// A sink is handed the whole session — after the header, then after every completed call — and
// what it receives is exactly the file's bytes, and conformant.
func TestSinkPublishesTheSession(t *testing.T) {
	dir := t.TempDir()
	sink := &memSink{}
	rec, err := New(dir, Boundary{Sink: sink})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Call(context.Background(), "work", map[string]any{}, toyWork); err != nil {
		t.Fatal(err)
	}
	rec.Close()

	sink.mu.Lock()
	got, n := string(sink.last), sink.n
	sink.mu.Unlock()
	if got == "" {
		t.Fatal("the sink never received the session")
	}
	if n < 2 {
		t.Errorf("expected a publish after the header and after the call, got %d", n)
	}
	if v := spec.ValidateTape(got); len(v) > 0 {
		t.Errorf("published tape is not conformant: %v", v)
	}
	fileData, _ := os.ReadFile(rec.Path())
	if got != string(fileData) {
		t.Errorf("sink bytes differ from the file:\n sink: %q\n file: %q", got, fileData)
	}
}

// A Recording reader recovers the span tree from a tape and renders it top-down — the same shape
// the Python and .NET readers produce.
func TestReaderRecoversAndRendersSpanTree(t *testing.T) {
	dir := t.TempDir()
	rec, err := New(dir, Boundary{Redact: serial.Rules{"password": nil}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rec.Call(context.Background(), "enrol",
		map[string]any{"user": "alice", "password": "hunter2"}, toyEnrol); err != nil {
		t.Fatal(err)
	}
	rec.Close()

	r, err := Load(rec.Path())
	if err != nil {
		t.Fatal(err)
	}
	cv := r.Call(0)
	tree := cv.Spans()
	if tree.Name != "enrol" || tree.Phase != "call" {
		t.Fatalf("bad root node: %+v", tree)
	}
	enrol := tree.Children[0]
	if enrol.Name != "enrol" || enrol.Outcome != "ok" {
		t.Fatalf("bad enrol span: %+v", enrol)
	}
	var got []string
	for _, c := range enrol.Children {
		got = append(got, fmt.Sprintf("%s/%s/%s", c.Name, c.Phase, c.Outcome))
	}
	want := []string{"load_corpus/span/ok", "corpus_read/point/", "register/span/error", "registration_failed/point/"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("span children:\n got  %v\n want %v", got, want)
	}

	rendered := cv.RenderSpans()
	for _, line := range []string{"register  ERROR  (2 fx)", "load_corpus  ok  (1 fx)"} {
		if !strings.Contains(rendered, line) {
			t.Errorf("render missing %q:\n%s", line, rendered)
		}
	}
}

// Edit the tape to visit a world that never happened: empty/alter a recorded answer, mark it a
// probe, and replay the real code against it. The code runs against the mutation.
func TestMutationProbeReplay(t *testing.T) {
	path := recordToySession(t) // call 0 is greet, which reads a document
	r, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	cv := r.Call(0)
	db := cv.Event("db", 0) // greet's QueryOne("get")
	if db == nil {
		t.Fatal("no db read to mutate")
	}
	db["res"] = map[string]any{"id": "u1", "exists": true, "data": map[string]any{"name": "MUTANT", "x": 1}}
	cv.MarkProbe()

	rep, err := ReplayCall(cv, toyResolver, true)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Divergence != "" || rep.Unanswerable != "" {
		t.Fatalf("probe replay should answer the path: div=%q unanswerable=%q", rep.Divergence, rep.Unanswerable)
	}
	if !rep.OK() {
		t.Errorf("probe replay should be OK, got %+v", rep)
	}
	if rep.ReplayedResult != "hello MUTANT" {
		t.Errorf("the code did not run against the mutated world: %v", rep.ReplayedResult)
	}
}

// Invariants judge the replayed trajectory: a claim about every execution, checked against the
// recording — one holds, one fails.
func TestInvariants(t *testing.T) {
	path := recordToySession(t)
	holds := NewInvariant("the greeting is non-empty", func(tr *Trajectory) error {
		if s, ok := tr.Result.(string); !ok || s == "" {
			return errors.New("empty greeting")
		}
		return nil
	})
	fails := NewInvariant("the greeting names bob", func(tr *Trajectory) error {
		s, _ := tr.Result.(string)
		if !strings.Contains(s, "bob") {
			return fmt.Errorf("no bob in %q", s)
		}
		return nil
	})

	rep, err := CheckInvariants(path, 0, toyResolver, []Invariant{holds, fails})
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Results[0].OK {
		t.Errorf("the holding invariant failed: %s", rep.Results[0].Err)
	}
	if rep.Results[1].OK {
		t.Errorf("the failing invariant passed")
	}
	if rep.OK() {
		t.Errorf("the report should not be OK with a failing invariant")
	}
}
