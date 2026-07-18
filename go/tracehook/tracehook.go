// Package tracehook is the runtime half of Go's variable-level tracer: the handful of functions
// that instrumented source calls, and the writer that turns those calls into the shared trace
// format. Nothing here is meant to be typed by a human — flightrecorder.Instrument writes the
// calls — but it is a normal package, so an instrumented build is a normal build.
//
// Why it is a separate package: the instrumented copy of YOUR code imports this. If the hook
// lived in the flightrecorder package, tracing that package's own code would be circular, and
// tracing a package that does not otherwise use flight-recorder would drag the whole recorder in.
//
// Two invariants this package must not break:
//
//   - It must never panic. Every call happens inside the observed function's own frame, so a
//     panic here propagates into the very execution the trace exists to explain, and the tracer
//     would have destroyed its own evidence. Everything is behind a recover.
//   - It must never change what it observes. It reads values, copies them, and encodes them; it
//     calls no method on them (no String(), no Error() on anything but a real error), takes no
//     lock of the program's, and hands nothing back.
//
// Concurrency: a Frame is per invocation, created by Enter, so two goroutines in the same
// function have two frames and cannot smear each other's deltas. Only the writer is shared, and
// it is mutex-guarded, so events from concurrent goroutines interleave but never tear.
package tracehook

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"regexp"
	"sync"

	"github.com/xag/flight-recorder/go/serial"
)

// TraceVersion 2: values are DATA. Version 1 (Python's, historically) held reprs, and asserting
// arithmetic over reprs failed confusingly rather than loudly.
const TraceVersion = 2

// EnvPath names the file the tracer writes to. Its presence is what turns tracing on: an
// instrumented binary run without it is a normal binary that pays for a few map lookups.
const EnvPath = "FLIGHT_RECORDER_TRACE"

// EnvForbid carries the parent's Boundary.Forbid patterns — a JSON array of strings — into this
// process. It exists because of an asymmetry that is easy to miss: the Boundary is declared in the
// parent, and the TRACE IS WRITTEN HERE, in a separate process the parent started. A tripwire
// enforced only where it was declared guards nothing at all in the one place that opens a file.
//
// A trace is the worst artifact to leave unguarded, not the least: it records every local on every
// executed line, which means values BEFORE they reach any redaction — and tracing is precisely what
// you switch on when you are debugging the request that went wrong, which is to say the request
// that carried the real credential.
//
// WHY THE ENVIRONMENT, AND WHAT WAS REJECTED
//
//   - A file of patterns beside the trace: a second artifact to write, find, and clean up, with its
//     own set of "what if it is missing" states. Nothing is gained over the channel already in use.
//   - Compiling the patterns into the instrumented copy as generated Go: needs codegen, is a
//     compile error away from breaking every traced run, and is not free for the boundary that
//     declares nothing.
//   - Scanning the finished trace in the parent and deleting it on a hit: too late by definition.
//     The bytes reached the disk. The guard's whole claim is that they never do.
//
// The environment is how the trace PATH already crosses (EnvPath), so the rules ride the same
// channel as the thing they govern: they arrive together, or there is no tracing to guard. What
// travels is the PATTERN — declared configuration, not a credential — and never a match.
const EnvForbid = "FLIGHT_RECORDER_TRACE_FORBID"

// RefusalSuffix is appended to the trace path to name the refusal file the tracer leaves when a
// pattern hits. Both halves derive the name from this, so parent and child cannot disagree.
const RefusalSuffix = ".forbidden"

// RefusalPath is where a refusal for the trace at path would be reported.
func RefusalPath(tracePath string) string { return tracePath + RefusalSuffix }

var (
	mu       sync.Mutex
	out      *os.File
	path     string
	events   []map[string]any
	started  bool
	live     bool
	forbid   []*regexp.Regexp
	refused  string
	failShut bool
)

func start() {
	started = true
	path = os.Getenv(EnvPath)
	if path == "" {
		return
	}
	if !loadForbid() {
		// Fail SHUT. The patterns were compiled once already, in the parent, so arriving here
		// unreadable means the channel is broken — and the one thing worse than no trace is a trace
		// written with the tripwire silently disarmed.
		fmt.Fprintf(os.Stderr, "flight-recorder: %s is unreadable — tracing is off\n", EnvForbid)
		return
	}
	f, err := os.Create(path)
	if err != nil {
		// A tracer that cannot write its file must not take the run down with it: the run is the
		// thing of value, the trace is the commentary.
		return
	}
	out, live = f, true
	emit(map[string]any{"e": "H", "trace_version": TraceVersion})
}

// loadForbid compiles the patterns handed over in the environment. Caller holds mu.
func loadForbid() bool {
	raw := os.Getenv(EnvForbid)
	if raw == "" {
		return true // no tripwire declared: the free path, and the only one that existed before
	}
	var pats []string
	if err := json.Unmarshal([]byte(raw), &pats); err != nil {
		return false
	}
	for _, p := range pats {
		re, err := regexp.Compile(p)
		if err != nil {
			return false
		}
		forbid = append(forbid, re)
	}
	return true
}

// vet is the tripwire, run on the encoded event BEFORE it reaches the in-memory tape or the file.
// It reports whether the event may be recorded. Caller holds mu.
//
// An event that will not marshal cannot be inspected, and something that cannot be inspected cannot
// be cleared — so it is refused too, rather than waved through unread.
func vet(ev map[string]any) bool {
	b, err := json.Marshal(ev)
	if err != nil {
		refuse("<an event that could not be encoded for inspection>")
		return false
	}
	for _, re := range forbid {
		if re.Match(b) {
			refuse(re.String())
			return false
		}
	}
	return true
}

// refuse shuts the tracer down for good and destroys the trace. Caller holds mu.
//
// Three things happen here, and each is deliberate. The file is REMOVED, not merely closed: the
// lines already on disk are clean, but a trace with a hole in it where the interesting value was
// is evidence that misleads, and the refusal is the finding — not a partial trace. Tracing is
// switched off permanently, so no later line can carry the same value past a guard that has
// already fired. And the refusal is written where the PARENT will find it, because a guard that
// fires in a child process and is swallowed there is not a guard.
//
// It names the pattern and never the match. This message goes to a log; a tripwire that quotes the
// secret it caught has become the leak it was there to prevent.
func refuse(pattern string) {
	if refused != "" {
		return
	}
	refused, live = pattern, false
	events = nil
	if out != nil {
		out.Close()
		out = nil
	}
	if path != "" {
		os.Remove(path)
		os.WriteFile(RefusalPath(path), []byte(pattern), 0o644)
	}
	fmt.Fprintf(os.Stderr,
		"flight-recorder: a traced value matches a forbidden pattern (%q) — the trace was refused "+
			"and nothing was written\n", pattern)
}

// Refused is the pattern that shut this process's tracer down, or "" if none did.
func Refused() string {
	mu.Lock()
	defer mu.Unlock()
	return refused
}

// Live reports whether this process is running with the tracer armed.
func Live() bool {
	mu.Lock()
	defer mu.Unlock()
	if !started {
		start()
	}
	return live
}

// emit writes one event. Caller holds mu.
//
// The tripwire runs FIRST — before the in-memory tape, not merely before the file. An invariant
// running inside this process reads Events() while the run is still going, so the buffer is an
// artifact like any other and a value refused to the disk must not be handed to it either.
func emit(ev map[string]any) {
	if len(forbid) > 0 && !vet(ev) {
		return
	}
	events = append(events, ev)
	if out == nil {
		return
	}
	b, err := json.Marshal(ev)
	if err != nil {
		return
	}
	out.Write(append(b, '\n'))
}

// Events copies every event recorded so far, so an invariant running inside the traced process
// can read the trace of the execution it is judging without waiting for the file to be closed.
func Events() []map[string]any {
	mu.Lock()
	defer mu.Unlock()
	cp := make([]map[string]any, len(events))
	copy(cp, events)
	return cp
}

// Count is how many events have been recorded — the cheap way to mark a point in the tape and
// later take the slice that one replay produced.
func Count() int {
	mu.Lock()
	defer mu.Unlock()
	return len(events)
}

// Close flushes and closes the trace file. An instrumented process that exits without calling it
// still has every line on disk (each event is written as it happens); this only tidies up.
func Close() {
	mu.Lock()
	defer mu.Unlock()
	if out != nil {
		out.Close()
		out = nil
	}
}

// A Frame is one invocation of one instrumented function: the identity a delta is computed
// against. Two calls to the same function — recursive, concurrent, or merely sequential — get
// two frames, because "did this variable change?" is a question about an invocation.
type Frame struct {
	fn     string
	lastAt string
	prev   map[string]string // name -> canonical JSON of the last value seen
}

// Enter opens a frame and records the call with its arguments.
func Enter(fn, at string, names []string, vals ...any) *Frame {
	f := &Frame{fn: fn, lastAt: at, prev: map[string]string{}}
	if !Live() {
		return f
	}
	defer swallow()
	mu.Lock()
	defer mu.Unlock()
	emit(map[string]any{"e": "C", "fn": fn, "at": at, "args": f.snapshot(names, vals)})
	return f
}

// Line records the locals that CHANGED, and attributes them to the previously executed
// statement — the one whose execution produced the change. A hook fires before a statement runs,
// so the delta it sees is the last statement's work, and reporting it at the upcoming line would
// blame the wrong line for every value in the trace.
//
// Only changes are reported. A variable unchanged across forty lines is forty lines of noise, and
// noise is what makes a trace something people stop reading.
func (f *Frame) Line(at string, names []string, vals ...any) {
	if !Live() {
		return
	}
	defer swallow()
	mu.Lock()
	defer mu.Unlock()
	delta := f.snapshot(names, vals)
	if len(delta) > 0 {
		emit(map[string]any{"e": "L", "fn": f.fn, "at": f.lastAt, "d": delta})
	}
	f.lastAt = at
}

// Leave records the return, or the panic on the way out. ptrs are pointers to the function's
// results: pointers, because a deferred call's arguments are evaluated at DEFER time, and the
// results are not written until the return actually happens.
//
// It recovers and re-panics rather than merely observing, because there is no other way to see a
// panic from inside the frame it is leaving. The panic reaches its handler unchanged; only the
// printed stack gains a "[recovered]" marker.
func (f *Frame) Leave(at string, ptrs ...any) {
	if r := recover(); r != nil {
		f.fail(at, r)
		panic(r)
	}
	if !Live() {
		return
	}
	defer swallow()
	mu.Lock()
	defer mu.Unlock()
	emit(map[string]any{"e": "R", "fn": f.fn, "at": at, "v": returned(ptrs)})
}

func (f *Frame) fail(at string, r any) {
	if !Live() {
		return
	}
	defer swallow()
	mu.Lock()
	defer mu.Unlock()
	emit(map[string]any{
		"e": "X", "fn": f.fn, "at": at,
		"type": fmt.Sprintf("%T", r),
		"v":    serial.SafeRepr(r, 200),
	})
}

// snapshot encodes the named values and returns only those that differ from what this frame last
// saw. Caller holds mu.
func (f *Frame) snapshot(names []string, vals []any) map[string]any {
	delta := map[string]any{}
	for i, name := range names {
		if i >= len(vals) {
			break
		}
		enc := serial.ToTraceJsonable(vals[i])
		// Compare the ENCODED forms. Comparing the live values would need reflect.DeepEqual on
		// anything at all (uncomparable types panic it), and would call a type-changing
		// transition equal where the recorded forms differ.
		key := canonical(enc)
		if was, seen := f.prev[name]; seen && was == key {
			continue
		}
		f.prev[name] = key
		delta[name] = enc
	}
	return delta
}

func canonical(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprintf("%v", v)
	}
	return string(b)
}

// returned derefs the result pointers. A void function returns nil, a single-result function
// returns its value, and a multi-result function returns the list — which is what "the value it
// returned" means in a language that returns more than one.
func returned(ptrs []any) any {
	switch len(ptrs) {
	case 0:
		return nil
	case 1:
		return serial.ToTraceJsonable(deref(ptrs[0]))
	}
	out := make([]any, len(ptrs))
	for i, p := range ptrs {
		out[i] = serial.ToTraceJsonable(deref(p))
	}
	return out
}

func deref(p any) (v any) {
	defer func() {
		if r := recover(); r != nil {
			v = nil
		}
	}()
	rv := reflect.ValueOf(p)
	if rv.Kind() != reflect.Pointer || rv.IsNil() {
		return nil
	}
	return rv.Elem().Interface()
}

// swallow is the "never panic in the observed frame" guarantee, made explicit.
func swallow() { _ = recover() }
