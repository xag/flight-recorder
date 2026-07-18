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
	"sync"

	"github.com/xag/flight-recorder/go/serial"
)

// TraceVersion 2: values are DATA. Version 1 (Python's, historically) held reprs, and asserting
// arithmetic over reprs failed confusingly rather than loudly.
const TraceVersion = 2

// EnvPath names the file the tracer writes to. Its presence is what turns tracing on: an
// instrumented binary run without it is a normal binary that pays for a few map lookups.
const EnvPath = "FLIGHT_RECORDER_TRACE"

var (
	mu      sync.Mutex
	out     *os.File
	events  []map[string]any
	started bool
	live    bool
)

func start() {
	started = true
	path := os.Getenv(EnvPath)
	if path == "" {
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
func emit(ev map[string]any) {
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
