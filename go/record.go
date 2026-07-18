// Package flightrecorder records what the outside world told your code — every database
// answer, HTTP response, clock read and random draw — as one JSONL tape per call, conformant
// to spec/tape-v1.md, and replays that tape against the real code so a past run reproduces
// exactly.
//
// The cardinal rule is INSTRUMENT, NEVER DUPLICATE: nothing here evaluates a query, computes a
// date, or knows what any value means. It records the questions your code asked the world and
// the answers it got; on replay it feeds those answers back and checks the questions still match.
//
// Go cannot monkeypatch a module's functions the way the Python recorder shims `datetime` and
// `random`, so instrumentation is explicit and idiomatic: the active call (recording) or feed
// (replay) rides on the context, and every boundary read goes through this package's primitives
// — Now, Perf, Effect, SampleIndices, RandBytes, Query/QueryOne/Exec. Each does the real thing
// while recording, and serves the recorded answer while replaying. Semantic spans (Span/Note)
// are the app's testimony, well-nested by construction because a span wraps the body it encloses.
package flightrecorder

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	mathrand "math/rand"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"sync"
	"time"

	"github.com/xag/flight-recorder/go/serial"
)

const formatVersion = 1

// A monotonic origin for Perf. Arbitrary, per-process — exactly what a monotonic clock is.
var processStart = time.Now()

// Boundary declares the app-specific inputs the recorder needs: constants to pin in the header,
// redaction (field-name Rules and value Scrub), and the forbid tripwire.
type Boundary struct {
	Constants    map[string]any
	Redact       serial.Rules
	Scrub        serial.Scrub
	Forbid       []string
	HeaderExtras map[string]any
}

// ForbiddenValue is raised when a Boundary.Forbid pattern matches the record the recorder was
// about to write. It names the RULE, never the match.
type ForbiddenValue struct{ Pattern, What string }

func (e *ForbiddenValue) Error() string {
	return fmt.Sprintf("%s matches a forbidden pattern (%q) after redaction — nothing was written; "+
		"name the field in Boundary.Redact, or widen a rule that stopped matching, and record again", e.What, e.Pattern)
}

// Recorder owns one session file.
type Recorder struct {
	mu     sync.Mutex
	f      *os.File
	path   string
	seq    int
	redact serial.Rules
	scrub  serial.Scrub
	forbid []*regexp.Regexp
}

// New opens a session file in dir and writes the header.
func New(dir string, b Boundary) (*Recorder, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	var forbid []*regexp.Regexp
	for _, p := range b.Forbid {
		re, err := regexp.Compile(p)
		if err != nil {
			return nil, fmt.Errorf("bad forbid pattern %q: %w", p, err)
		}
		forbid = append(forbid, re)
	}
	stamp := time.Now().Format("20060102-150405")
	path := filepath.Join(dir, fmt.Sprintf("flight-%s-%d.jsonl", stamp, os.Getpid()))
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	r := &Recorder{f: f, path: path, redact: b.Redact, scrub: b.Scrub, forbid: forbid}

	constants := map[string]any{}
	for k, v := range b.Constants {
		constants[k] = serial.ToJsonable(v)
	}
	header := map[string]any{
		"ev":        "session",
		"version":   formatVersion,
		"started":   time.Now().Format(time.RFC3339Nano),
		"go":        runtime.Version(),
		"constants": constants,
	}
	for k, v := range b.HeaderExtras {
		header[k] = serial.ToJsonable(v)
	}
	if err := r.writeObj(header, "the session record"); err != nil {
		f.Close()
		os.Remove(path)
		return nil, err
	}
	return r, nil
}

// Path is the session file's path.
func (r *Recorder) Path() string { return r.path }

// Close closes the session file.
func (r *Recorder) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.f != nil {
		err := r.f.Close()
		r.f = nil
		return err
	}
	return nil
}

func (r *Recorder) forbiddenHit(line []byte) string {
	for _, re := range r.forbid {
		if re.Match(line) {
			return re.String()
		}
	}
	return ""
}

func (r *Recorder) writeObj(obj map[string]any, what string) error {
	line, err := json.Marshal(obj)
	if err != nil {
		return err
	}
	if hit := r.forbiddenHit(line); hit != "" {
		return &ForbiddenValue{Pattern: hit, What: what}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	_, err = r.f.Write(append(line, '\n'))
	return err
}

func (r *Recorder) scrubEvent(ev map[string]any) map[string]any {
	if len(r.redact) == 0 && r.scrub == nil {
		return ev
	}
	for _, key := range []string{"args", "kwargs", "res", "err", "data"} {
		if v, ok := ev[key]; ok {
			ev[key] = serial.Redact(v, r.redact, r.scrub)
		}
	}
	return ev
}

// --- the ambient: recording OR replay, carried on the context -------------------------

type ctxKey struct{}

// ambient is what a boundary primitive consults. Exactly one of its fields is set: call while
// recording, replay while replaying.
type ambient struct {
	call   *call
	replay *replayState
}

func ambientFrom(ctx context.Context) *ambient {
	a, _ := ctx.Value(ctxKey{}).(*ambient)
	return a
}

type call struct {
	rec    *Recorder
	events []map[string]any
	sid    int
}

func (c *call) nextSid() int {
	c.sid++
	return c.sid
}

// emit scrubs, runs the forbid tripwire (panicking with *ForbiddenValue on a hit, which Call
// recovers), and appends to the call buffer — the guard before the append, since the buffer
// becomes the call record.
func (c *call) emit(ev map[string]any) {
	ev = c.rec.scrubEvent(ev)
	if line, err := json.Marshal(ev); err == nil {
		if hit := c.rec.forbiddenHit(line); hit != "" {
			panic(&ForbiddenValue{Pattern: hit, What: fmt.Sprintf("a recorded %v event", ev["k"])})
		}
	}
	c.events = append(c.events, ev)
}

// Call records one top-level tool call: it runs body with an active call on the context,
// buffers every event body produces, and writes the call line when body returns.
func (r *Recorder) Call(ctx context.Context, fn string, kwargs map[string]any,
	body func(context.Context) (any, error)) (any, error) {

	c := &call{rec: r}
	cctx := context.WithValue(ctx, ctxKey{}, &ambient{call: c})
	t0 := time.Now()

	var result any
	var bodyErr error
	func() {
		defer func() {
			if rec := recover(); rec != nil {
				if fv, ok := rec.(*ForbiddenValue); ok {
					bodyErr = fv
					return
				}
				panic(rec)
			}
		}()
		result, bodyErr = body(cctx)
	}()

	ms := float64(time.Since(t0).Microseconds()) / 1000.0
	if werr := r.writeCall(fn, kwargs, c.events, result, bodyErr, ms); werr != nil && bodyErr == nil {
		bodyErr = werr
	}
	return result, bodyErr
}

func (r *Recorder) writeCall(fn string, kwargs map[string]any, events []map[string]any,
	result any, callErr error, ms float64) error {

	var errField any
	if callErr != nil {
		errField = callErr.Error()
	}
	evs := make([]any, len(events))
	for i, e := range events {
		evs[i] = e
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	seq := r.seq + 1
	obj := map[string]any{
		"ev":     "call",
		"seq":    seq,
		"fn":     fn,
		"kwargs": serial.Redact(serial.ToJsonable(kwargs), r.redact, r.scrub),
		"events": evs,
		"result": serial.Redact(serial.ToJsonable(result), r.redact, r.scrub),
		"error":  errField,
		"ts":     time.Now().Format(time.RFC3339Nano),
		"ms":     round2(ms),
	}
	line, err := json.Marshal(obj)
	if err != nil {
		return err
	}
	if hit := r.forbiddenHit(line); hit != "" {
		return &ForbiddenValue{Pattern: hit, What: fmt.Sprintf("the call record for %q", fn)}
	}
	if _, err := r.f.Write(append(line, '\n')); err != nil {
		return err
	}
	r.seq = seq // only a written call consumes a seq, so the tape stays 1-based and contiguous
	return nil
}

func round2(f float64) float64 { return math.Round(f*100) / 100 }

// --- boundary primitives (record while recording, serve while replaying) --------------

// Now records and returns the wall clock.
func Now(ctx context.Context) time.Time {
	a := ambientFrom(ctx)
	if a == nil {
		return time.Now()
	}
	if a.replay != nil {
		return a.replay.now()
	}
	v := time.Now()
	a.call.emit(map[string]any{"k": "now", "v": v.Format(time.RFC3339Nano)})
	return v
}

// Perf records and returns a monotonic clock reading in milliseconds (arbitrary origin).
func Perf(ctx context.Context) float64 {
	a := ambientFrom(ctx)
	if a == nil {
		return float64(time.Since(processStart).Nanoseconds()) / 1e6
	}
	if a.replay != nil {
		return a.replay.perf()
	}
	v := float64(time.Since(processStart).Nanoseconds()) / 1e6
	a.call.emit(map[string]any{"k": "perf", "v": v})
	return v
}

// Effect records a module-level effect: the (args → result/exception) that IS the external
// world. While replaying it serves the recorded result (or re-raises the recorded error) and
// asserts the args match — a different question here is a path divergence.
func Effect[T any](ctx context.Context, name string, args []any, fn func() (T, error)) (T, error) {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return replayEffect[T](a.replay, name, args)
	}
	res, err := fn()
	if a == nil {
		return res, err
	}
	ev := map[string]any{
		"k":      "fx",
		"fn":     name,
		"args":   jsonableSlice(args),
		"kwargs": map[string]any{},
	}
	if err != nil {
		ev["err"] = map[string]any{"type": errType(err), "repr": truncate(err.Error(), 300), "args": []any{}}
	} else {
		ev["res"] = serial.ToJsonable(res)
	}
	a.call.emit(ev)
	return res, err
}

// SampleIndices draws k distinct positions from [0, n) and records the positions (not the
// members), so replay can pick the same members from a mutated population.
func SampleIndices(ctx context.Context, n, k int) []int {
	if k > n {
		k = n
	}
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return a.replay.sample(n, k)
	}
	idx := mathrand.Perm(n)[:k]
	if a != nil {
		a.call.emit(map[string]any{"k": "rand", "m": "sample", "n": n, "kk": k, "idx": idx})
	}
	return idx
}

// RandBytes draws n bytes of real entropy and records them as hex.
func RandBytes(ctx context.Context, n int) ([]byte, error) {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return a.replay.bytes(n)
	}
	b := make([]byte, n)
	if _, err := cryptorand.Read(b); err != nil {
		return nil, err
	}
	if a != nil {
		a.call.emit(map[string]any{"k": "rand", "m": "bytes", "n": n, "hex": hex.EncodeToString(b)})
	}
	return b, nil
}

// RandFloat draws and records a uniform float in [0, 1).
func RandFloat(ctx context.Context) float64 {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return a.replay.randFloat()
	}
	v := mathrand.Float64()
	if a != nil {
		a.call.emit(map[string]any{"k": "rand", "m": "float", "v": v})
	}
	return v
}

// RandIntn draws and records a uniform integer in [0, n).
func RandIntn(ctx context.Context, n int) int {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return a.replay.randInt()
	}
	v := mathrand.Intn(n)
	if a != nil {
		a.call.emit(map[string]any{"k": "rand", "m": "int", "v": v})
	}
	return v
}

// Snapshot is a document's recordable surface — identity, existence, data.
type Snapshot struct {
	ID     *string
	Exists bool
	Data   any
}

func (s Snapshot) jsonable() map[string]any {
	var id any
	if s.ID != nil {
		id = *s.ID
	}
	var data any
	if s.Exists {
		data = serial.ToJsonable(s.Data)
	}
	return map[string]any{"id": id, "exists": s.Exists, "data": data}
}

// Query records and returns a terminal read that yielded several snapshots. sig is the rendered
// chain that led to it. While replaying it serves the recorded snapshots and does not run.
func Query(ctx context.Context, op, sig string, run func() ([]Snapshot, error)) ([]Snapshot, error) {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return a.replay.query(op, sig), nil
	}
	res, err := run()
	if err != nil || a == nil {
		return res, err
	}
	arr := make([]any, len(res))
	for i, s := range res {
		arr[i] = s.jsonable()
	}
	a.call.emit(map[string]any{"k": "db", "op": op, "sig": sig, "res": arr})
	return res, nil
}

// QueryOne records and returns a terminal read that yielded a single snapshot.
func QueryOne(ctx context.Context, op, sig string, run func() (Snapshot, error)) (Snapshot, error) {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		return a.replay.queryOne(op, sig), nil
	}
	res, err := run()
	if err != nil || a == nil {
		return res, err
	}
	a.call.emit(map[string]any{"k": "db", "op": op, "sig": sig, "res": res.jsonable()})
	return res, nil
}

// Exec records a terminal write — the questions (args), not answers. While replaying the write
// is NOT executed: it is compared against the recording, and a mismatch is a write divergence.
func Exec(ctx context.Context, op, sig string, args []any, run func() error) error {
	a := ambientFrom(ctx)
	if a != nil && a.replay != nil {
		a.replay.execCompare(op, sig, jsonableSlice(args))
		return nil
	}
	if a == nil {
		return run()
	}
	if err := run(); err != nil {
		return err
	}
	a.call.emit(map[string]any{"k": "db", "op": op, "sig": sig, "args": jsonableSlice(args)})
	return nil
}

// Note records that something meaningful happened at a point, in the app's own vocabulary.
func Note(ctx context.Context, name string, data map[string]any) {
	a := ambientFrom(ctx)
	if a == nil {
		return
	}
	if a.replay != nil {
		a.replay.note(name)
		return
	}
	ev := map[string]any{"k": "sem", "name": name, "phase": "point", "sid": a.call.nextSid()}
	if len(data) > 0 {
		ev["data"] = jsonableMap(data)
	}
	a.call.emit(ev)
}

// Span records that a stretch of execution constituted a domain act and encloses the raw events
// it produced. Well-nested by construction. If body errors or panics, the end still lands with
// outcome "error" and the panic propagates untouched.
func Span(ctx context.Context, name string, data map[string]any,
	body func(context.Context) error) (err error) {

	a := ambientFrom(ctx)
	if a == nil {
		return body(ctx)
	}
	if a.replay != nil {
		return a.replay.span(ctx, name, body)
	}

	c := a.call
	sid := c.nextSid()
	begin := map[string]any{"k": "sem", "name": name, "phase": "begin", "sid": sid}
	if len(data) > 0 {
		begin["data"] = jsonableMap(data)
	}
	c.emit(begin)

	end := func(outcome string) {
		c.emit(map[string]any{"k": "sem", "name": name, "phase": "end", "sid": sid, "outcome": outcome})
	}
	defer func() {
		if rec := recover(); rec != nil {
			end("error")
			panic(rec)
		}
		if err != nil {
			end("error")
		} else {
			end("ok")
		}
	}()
	err = body(ctx)
	return err
}

// --- helpers --------------------------------------------------------------------------

func jsonableSlice(args []any) []any {
	out := make([]any, len(args))
	for i, a := range args {
		out[i] = serial.ToJsonable(a)
	}
	return out
}

func jsonableMap(m map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		out[k] = serial.ToJsonable(v)
	}
	return out
}

func errType(err error) string {
	t := reflect.TypeOf(err)
	if t == nil {
		return "error"
	}
	if t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	if n := t.Name(); n != "" {
		return n
	}
	return t.String()
}

func truncate(s string, limit int) string {
	r := []rune(s)
	if len(r) <= limit {
		return s
	}
	return string(r[:limit-1]) + "…"
}
