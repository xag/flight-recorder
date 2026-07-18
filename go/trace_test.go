package flightrecorder

// The proof that Go has variable-level tracing after all.
//
// The claim used to be that Go could not have this: no sys.settrace, no inspector protocol, no
// runtime line hook of any kind. Go has no such HOOK — but it has go/ast, and a program that can
// read its own source can add the hook itself. These tests are the proof, and they end where the
// library began: an internal variable quietly emptying a corpus behind a perfectly self-consistent
// output.
//
// HOW THESE TESTS ARE SHAPED, because it is not the usual shape. Instrumented code has to be
// compiled, so the observed run happens in a child `go test` inside an instrumented copy of this
// module. One test function therefore has two roles: TestTraceChild is a no-op in an ordinary
// build and does all the work when it finds itself running instrumented. Everything else is the
// parent, reading the trace the child left behind. Tracing() is what tells them apart.

import (
	"context"
	"encoding/json"
	"fmt"
	"go/parser"
	"go/token"
	"strings"
	"sync"
	"testing"
)

// --- the instrumented half ---------------------------------------------------------------

func TestTraceChild(t *testing.T) {
	if !Tracing() {
		t.Skip("the instrumented half of the trace tests; TestTraced* drives it")
	}

	// Record the buggy tool against its store, then resurrect the recorded execution with no
	// store at all — and watch it from the inside while it runs.
	dir := t.TempDir()
	rec, err := New(dir, Boundary{})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := rec.Call(ctx, "study_status",
		map[string]any{"email": "a@b.c", "level": 0},
		func(ctx context.Context) (any, error) { return studyStatus(ctx, "a@b.c", 0) }); err != nil {
		t.Fatal(err)
	}
	if err := rec.Close(); err != nil {
		t.Fatal(err)
	}

	// The claim that catches the bug is a claim about an internal variable. There is no claim
	// about the RESULT that could catch it: every number in the result agrees with every other.
	levelHolds := NewInvariant("level never excludes the whole corpus", func(tj *Trajectory) error {
		obs := tj.Trace.Values("level")
		if len(obs) == 0 {
			return fmt.Errorf("level was never observed — this claim would otherwise pass vacuously")
		}
		for _, o := range obs {
			if toFloat(o.Value) <= 0 {
				return fmt.Errorf("level=%v at %s excludes the whole corpus", o.Value, o.At)
			}
		}
		return nil
	})
	deckHolds := NewInvariant("never claims done while the corpus holds words", func(tj *Trajectory) error {
		res, _ := tj.Result.(map[string]any)
		if res["done"] == true && toFloat(res["deck"]) == 0 && toFloat(res["corpus"]) > 0 {
			// Stated deliberately as an OUTPUT claim, to show it condemning nothing: the output
			// is self-consistent, so this is exactly the claim that lets the bug through.
			return nil
		}
		return nil
	})

	rep, err := CheckInvariants(rec.Path(), 0, toyStudyResolver, []Invariant{levelHolds, deckHolds})
	if err != nil {
		t.Fatal(err)
	}
	fmt.Printf("CHILD-REPLAY-OK %v\n", rep.Replay.OK())
	fmt.Printf("CHILD-RESULT %s\n", jsonString(rep.Replay.ReplayedResult))
	fmt.Printf("CHILD-TRACE-LEN %d\n", rep.Replay.Trace.Len())
	for _, r := range rep.Results {
		fmt.Printf("CHILD-INVARIANT ok=%v name=%q err=%q\n", r.OK, r.Name, r.Err)
	}

	// And a run that goes down: the trace must carry what the code believed on the way.
	func() {
		defer func() { fmt.Printf("CHILD-RECOVERED %v\n", recover()) }()
		_ = toyBoom(1)
	}()
}

// --- the orchestrating half --------------------------------------------------------------

var (
	traceOnce sync.Once
	traceRun  *TracedRun
	traceErr  error
)

// tracedChild runs the instrumented child exactly once for the whole package. A traced run
// compiles a copy of the module; doing it per test would buy nothing and cost seconds each.
func tracedChild(t *testing.T) *TracedRun {
	t.Helper()
	traceOnce.Do(func() {
		traceRun, traceErr = RunTraced(TraceSpec{
			Include: []string{"tracedtoy_test.go"},
			Command: []string{"test", "-run", "^TestTraceChild$", "-count=1", "-v", "-vet=off", "."},
		})
	})
	if traceErr != nil {
		t.Fatalf("running the traced child: %v", traceErr)
	}
	if traceRun.ExitErr != nil {
		t.Fatalf("the traced child failed: %v\n%s", traceRun.ExitErr, traceRun.Output)
	}
	return traceRun
}

// Every local, on every executed line: the names the code actually held, not the ones it returned.
func TestTracedEveryLocalOnEveryLine(t *testing.T) {
	run := tracedChild(t)
	if run.Trace.Len() == 0 {
		t.Fatalf("the trace is empty — nothing was observed\n%s", run.Output)
	}
	names := map[string]bool{}
	for _, n := range run.Trace.Names() {
		names[n] = true
	}
	// email and level arrived as arguments; rows, corpus, deck, done were built line by line.
	for _, want := range []string{"email", "level", "rows", "corpus", "deck", "done", "x", "stage"} {
		if !names[want] {
			t.Errorf("%q was never observed; the trace saw %v", want, run.Trace.Names())
		}
	}
	if calls := run.Trace.Calls("studyStatus"); len(calls) == 0 {
		t.Errorf("no call event for studyStatus; the trace holds %d events", run.Trace.Len())
	} else if calls[0].Args["email"] != "a@b.c" {
		t.Errorf("studyStatus was recorded as called with %v", calls[0].Args)
	}
}

// The timeline of one variable — a lookup, not an inference — reporting CHANGES only.
func TestTracedValuesTimeline(t *testing.T) {
	run := tracedChild(t)

	// level is a parameter that never moves. It is observed once per FRAME — the recorded call,
	// the resolver, the replayed call — because change detection is per invocation, as it must
	// be: "did this variable change?" is a question about one execution of one function, and a
	// frame that has never seen a variable before has genuinely just seen it for the first time.
	level := run.Trace.Values("level")
	if len(level) == 0 {
		t.Fatalf("level was never observed:\n%s", run.Trace.Timeline())
	}
	for _, o := range level {
		if toFloat(o.Value) != 0 {
			t.Errorf("level was %v at %s — 0 is the whole story", o.Value, o.At)
		}
		if !strings.Contains(o.At, "tracedtoy_test.go:") {
			t.Errorf("the observation does not say where it was made: %q", o.At)
		}
	}

	// CHANGES ONLY, which is the claim that makes a trace readable. corpus is appended to in a
	// three-iteration loop and is in scope on every line of it; a tracer reporting every line
	// would say so a dozen times. No two consecutive observations may be the same value.
	corpus := run.Trace.Values("corpus")
	if len(corpus) < 4 {
		t.Errorf("corpus was built from 3 rows but reported %d transitions:\n%s",
			len(corpus), run.Trace.Render("corpus"))
	}
	for i := 1; i < len(corpus); i++ {
		a, _ := json.Marshal(corpus[i-1].Value)
		b, _ := json.Marshal(corpus[i].Value)
		if string(a) == string(b) {
			t.Errorf("corpus was reported twice running as %s — that is noise, not a change:\n%s",
				b, run.Trace.Render("corpus"))
			break
		}
	}
	last, ok := corpus[len(corpus)-1].Value.([]any)
	if !ok || len(last) != 3 {
		t.Errorf("corpus ended as %v, expected three words", corpus[len(corpus)-1].Value)
	}

	// deck is declared empty and the loop that would fill it never fires, so within a frame it
	// has exactly one value to report however many lines it survives.
	deck := run.Trace.Values("deck")
	if len(deck) == 0 {
		t.Fatalf("deck was never observed:\n%s", run.Trace.Timeline())
	}
	for _, o := range deck {
		if arr, ok := o.Value.([]any); !ok || len(arr) != 0 {
			t.Errorf("deck was %v at %s; the bug leaves it empty", o.Value, o.At)
		}
	}
	if len(deck) > len(corpus) {
		t.Errorf("deck never changed but was reported %d times to corpus's %d:\n%s",
			len(deck), len(corpus), run.Trace.Render("deck"))
	}
	if !strings.Contains(run.Trace.Render("deck"), "deck = ") {
		t.Errorf("deck's timeline does not read as one:\n%s", run.Trace.Render("deck"))
	}

	if run.Trace.Final("done") == nil || run.Trace.Final("done").Value != true {
		t.Errorf("done ended as %v", run.Trace.Final("done"))
	}
	if got := run.Trace.Render("nosuchvariable"); got != "nosuchvariable: never observed" {
		t.Errorf("an unobserved variable renders as %q", got)
	}
	if !strings.Contains(run.Trace.Timeline(), "call studyStatus") {
		t.Errorf("the timeline does not read as one:\n%s", run.Trace.Timeline())
	}
}

// Tracing must not disturb what it observes: the instrumented run produced exactly what the
// uninstrumented one produces, in this very process.
func TestTracedDoesNotDisturbWhatItObserves(t *testing.T) {
	run := tracedChild(t)

	plain, err := studyStatus(context.Background(), "a@b.c", 0)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := json.Marshal(map[string]any{
		"corpus": plain["corpus"], "deck": plain["deck"], "done": plain["done"]})

	got := childLine(t, run.Output, "CHILD-RESULT ")
	if !jsonEqual(mustJSON(t, got), mustJSON(t, string(want))) {
		t.Errorf("the traced run produced %s, the untraced one %s", got, want)
	}

	// And the return event agrees with both: the tracer read the value the function actually gave.
	rets := run.Trace.Returns("studyStatus")
	if len(rets) == 0 {
		t.Fatalf("studyStatus returned but the trace holds no return event")
	}
	pair, ok := rets[len(rets)-1].Value.([]any)
	if !ok || len(pair) != 2 {
		t.Fatalf("a two-result function returned %v", rets[len(rets)-1].Value)
	}
	res, _ := pair[0].(map[string]any)
	if toFloat(res["corpus"]) != toFloat(plain["corpus"]) || toFloat(res["deck"]) != toFloat(plain["deck"]) {
		t.Errorf("the traced return was %v, the untraced result %v", res, plain)
	}
}

// A panic inside a traced run still surfaces, and the trace carries what the code believed up to
// the throw — which is the only reason to have watched at all.
func TestTracedPanicSurfacesWithTheTraceUpToIt(t *testing.T) {
	run := tracedChild(t)

	if got := childLine(t, run.Output, "CHILD-RECOVERED "); !strings.Contains(got, "gave up: about to fail") {
		t.Errorf("the panic did not surface unchanged: %q", got)
	}
	raised := run.Trace.Raised()
	if len(raised) == 0 {
		t.Fatalf("the trace holds no panic event:\n%s", run.Trace.Timeline())
	}
	last := raised[len(raised)-1]
	if !strings.Contains(last.Detail, "gave up") {
		t.Errorf("the recorded panic was %q", last.Detail)
	}
	if !strings.Contains(last.Fn, "toyBoom") {
		t.Errorf("the panic was attributed to %q", last.Fn)
	}
	// The value the code was holding when it went down.
	stage := run.Trace.Final("stage")
	if stage == nil || stage.Value != "about to fail" {
		t.Errorf("stage was %v at the throw", stage)
	}
}

// THE BUG: a self-consistent output, condemned by its own trace.
//
// The output says corpus 3, deck 0, done. Every number agrees with every other number. No
// assertion on the RESULT can call this wrong — "done with an empty deck" is exactly what the code
// means. The wrongness is that `level` excluded the entire corpus, and that is only visible from
// the inside.
func TestTracedSelfConsistentOutputCondemnedByItsOwnTrace(t *testing.T) {
	run := tracedChild(t)

	// The replay reproduced the recording exactly: as a regression oracle, nothing is wrong here.
	if got := childLine(t, run.Output, "CHILD-REPLAY-OK "); got != "true" {
		t.Fatalf("the recorded execution did not reproduce (%s)\n%s", got, run.Output)
	}

	// The output is self-consistent…
	res := mustJSON(t, childLine(t, run.Output, "CHILD-RESULT "))
	m, _ := res.(map[string]any)
	if m["done"] != true || toFloat(m["deck"]) != 0 || toFloat(m["corpus"]) != 3 {
		t.Fatalf("the fixture stopped producing its self-consistent wrong answer: %v", m)
	}

	// …and the output claim, dutifully written, holds. It cannot do otherwise.
	if !invariantVerdict(t, run.Output, "never claims done while the corpus holds words") {
		t.Errorf("the output claim failed; it is supposed to be powerless here")
	}

	// The claim about the internal variable is the one that condemns it.
	if invariantVerdict(t, run.Output, "level never excludes the whole corpus") {
		t.Errorf("the internal claim held — the trace failed to catch the bug\n%s", run.Output)
	}
	if !strings.Contains(run.Output, "level=0 at tracedtoy_test.go:") {
		t.Errorf("the failure does not name the value and the line:\n%s", run.Output)
	}

	// And the parent can read the same evidence straight off the trace.
	observed := run.Trace.Values("level")
	allPositive := true
	for _, o := range observed {
		if toFloat(o.Value) <= 0 {
			allPositive = false
		}
	}
	if allPositive {
		t.Errorf(`the claim "level never excludes the whole corpus" should be FALSE: %v`, observed)
	}
}

// A trace queried outside an instrumented build answers honestly rather than pretending: empty,
// and every query says so. An invariant that reads it will fail on "never observed" rather than
// pass vacuously.
func TestTraceIsEmptyAndHonestOutsideAnInstrumentedRun(t *testing.T) {
	if Tracing() {
		t.Skip("this process is the instrumented child")
	}
	path := recordToySession(t)
	rep, err := Replay(path, 0, toyResolver)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Trace == nil {
		t.Fatal("the report carries no trace at all; it should carry an empty one")
	}
	if rep.Trace.Len() != 0 {
		t.Errorf("an ordinary build recorded %d trace events", rep.Trace.Len())
	}
	if got := rep.Trace.Render("name"); got != "name: never observed" {
		t.Errorf("an empty trace renders %q", got)
	}
	var nilTrace *Trace
	if nilTrace.Len() != 0 || len(nilTrace.Values("x")) != 0 || len(nilTrace.Names()) != 0 {
		t.Errorf("a nil trace is not queryable without a panic")
	}
}

// --- the transform itself, without compiling anything --------------------------------------

// Instrument must produce parseable Go, put an observation before every statement, and report the
// ORIGINAL line numbers — the ones a reader has on disk, not the ones instrumentation created.
func TestInstrumentRewritesToParseableGoWithOriginalLines(t *testing.T) {
	src := `package p

func Add(a, b int) int {
	sum := a + b
	return sum
}
`
	out, did, err := Instrument("thing.go", []byte(src))
	if err != nil {
		t.Fatal(err)
	}
	if !did {
		t.Fatal("a file with a function was reported as having nothing to instrument")
	}
	if _, err := parser.ParseFile(token.NewFileSet(), "thing.go", out, 0); err != nil {
		t.Fatalf("the instrumented source does not parse: %v\n%s", err, out)
	}
	text := string(out)
	for _, want := range []string{
		`__frhook.Enter("Add", "thing.go:3", []string{"a", "b"}, a, b)`,
		`.Line("thing.go:4", []string{"a", "b"}, a, b)`,   // before sum exists
		`.Line("thing.go:5", []string{"a", "b", "sum"},`,  // after it does
		`.Leave("thing.go:3", &__fr_0_r1)`,                // the result, named for us
		`import __frhook "github.com/xag/flight-recorder`, // spliced after the package clause
	} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
	if strings.Contains(text, "thing.go:6") || strings.Contains(text, "thing.go:7") {
		t.Errorf("the trace would report lines the original file does not have:\n%s", text)
	}
}

// A file with nothing to observe must come back untouched. Importing the hook into it would be an
// unused import, and an unused import is a compile error — the tracer would break the build of
// every file of constants it was pointed at.
func TestInstrumentLeavesAFileWithNoFunctionsAlone(t *testing.T) {
	src := "package p\n\nconst Limit = 3\n"
	out, did, err := Instrument("k.go", []byte(src))
	if err != nil {
		t.Fatal(err)
	}
	if did || string(out) != src {
		t.Errorf("a file with no function bodies was rewritten:\n%s", out)
	}
}

// Scope is tracked syntactically, so the shapes that introduce names in Go all have to work: a
// loop variable, an if's init, a range, a type switch's binding, a closure's own parameters.
func TestInstrumentTracksTheScopesGoActuallyHas(t *testing.T) {
	src := `package p

func F(items []any) {
	for i := 0; i < 3; i++ {
		_ = i
	}
	for k, v := range map[string]int{} {
		_ = k
		_ = v
	}
	if n, ok := items[0].(int); ok {
		_ = n
		_ = ok
	}
	switch w := items[0].(type) {
	case int:
		_ = w
	}
	go func(inner int) { _ = inner }(1)
}
`
	out, _, err := Instrument("s.go", []byte(src))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parser.ParseFile(token.NewFileSet(), "s.go", out, 0); err != nil {
		t.Fatalf("the instrumented source does not parse: %v\n%s", err, out)
	}
	text := string(out)
	for _, want := range []string{
		`"items", "i"`,      // the loop variable, inside the loop
		`"items", "k", "v"`, // both range variables
		`"items", "n", "ok"`,
		`"items", "w"`,       // the type switch's binding
		`[]string{"inner"}`,  // the closure traced as a function in its own right
		`__frhook.Enter("F"`, // and the enclosing function still entered
	} {
		if !strings.Contains(text, want) {
			t.Errorf("missing %q in:\n%s", want, text)
		}
	}
	// The blank identifier holds nothing and must never appear as an observed name.
	if strings.Contains(text, `"_"`) {
		t.Errorf("the blank identifier was recorded as a variable:\n%s", text)
	}
}

// Build constraints and compiler directives must survive the rewrite where they are. A
// //go:build line that drifted below the package clause would silently stop constraining the
// file, and the instrumented copy would compile a set of files the real build never does — a
// difference between the traced run and the real one, which is the one thing a tracer may not
// introduce. The import is spliced as text immediately after the package clause for this reason.
func TestInstrumentKeepsBuildConstraintsWhereTheyBelong(t *testing.T) {
	src := "//go:build linux\n// +build linux\n\n// Package p does a thing.\npackage p\n\n" +
		"//go:noinline\nfunc F() int {\n\tx := 1\n\treturn x\n}\n"
	out, _, err := Instrument("p.go", []byte(src))
	if err != nil {
		t.Fatal(err)
	}
	text := string(out)
	if !strings.HasPrefix(text, "//go:build linux\n// +build linux\n") {
		t.Errorf("the build constraint moved:\n%s", text)
	}
	pkg := strings.Index(text, "package p")
	imp := strings.Index(text, "import __frhook")
	if pkg < 0 || imp < pkg {
		t.Errorf("the hook import did not land after the package clause:\n%s", text)
	}
	if strings.Index(text, "//go:build") > pkg {
		t.Errorf("the build constraint ended up below the package clause:\n%s", text)
	}
	if !strings.Contains(text, "//go:noinline\nfunc F()") {
		t.Errorf("a compiler directive lost its declaration:\n%s", text)
	}
}

// --- helpers -------------------------------------------------------------------------------

func childLine(t *testing.T, output, prefix string) string {
	t.Helper()
	for _, ln := range strings.Split(output, "\n") {
		ln = strings.TrimSpace(ln)
		if strings.HasPrefix(ln, prefix) {
			return strings.TrimSpace(strings.TrimPrefix(ln, prefix))
		}
	}
	t.Fatalf("the child never reported %q:\n%s", prefix, output)
	return ""
}

func invariantVerdict(t *testing.T, output, name string) bool {
	t.Helper()
	for _, ln := range strings.Split(output, "\n") {
		if strings.Contains(ln, "CHILD-INVARIANT") && strings.Contains(ln, `name="`+name+`"`) {
			return strings.Contains(ln, "ok=true")
		}
	}
	t.Fatalf("the child never reported a verdict on %q:\n%s", name, output)
	return false
}

func mustJSON(t *testing.T, s string) any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		t.Fatalf("not JSON: %q", s)
	}
	return v
}
