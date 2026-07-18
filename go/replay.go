package flightrecorder

// Replay: re-execute a recorded call with the recording as its world. Recorded events are fed
// back in their original order; the replayed code must ask the boundary the same questions in
// the same order (anything else is a divergence naming the first difference) and gets handed the
// recorded answers. Writes are compared, never executed.
//
// This is the strict half of the Python replay engine. Probe replay (matching a mutated tape by
// shape) and the sys.settrace variable-level tracer have no port here: Go has no settrace, and
// probe is a later brick. What ports cleanly is the Feed and the verdict — result/error match,
// boundary divergence, and the independent semantic-divergence signal.

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/xag/flight-recorder/go/serial"
)

// ReplayedEffectError stands in for a recorded effect exception when it is re-raised on replay.
type ReplayedEffectError struct {
	Type string
	Repr string
}

func (e *ReplayedEffectError) Error() string {
	if e.Repr != "" {
		return e.Repr
	}
	return e.Type
}

// replayDivergence is panicked by the feed on a mismatch and recovered by Replay.
type replayDivergence struct{ msg string }

// SemPair is one semantic claim: a name and a phase (begin|end|point). Payloads are a reader's
// business and are deliberately not compared.
type SemPair struct {
	Name  string
	Phase string
}

// feed is the recording as the world: events in order, popped by kind/shape.
type feed struct {
	events    []map[string]any
	pos       int
	consumed  int
	writeDivs []string
}

func (f *feed) remaining() int { return len(f.events) - f.pos }

func (f *feed) skipSems() {
	for f.pos < len(f.events) {
		if k, _ := f.events[f.pos]["k"].(string); k != "sem" {
			return
		}
		f.pos++
		f.consumed++
	}
}

func (f *feed) matches(ev map[string]any, kind, sig, op, fn string) bool {
	if k, _ := ev["k"].(string); k != kind {
		return false
	}
	if kind == "db" && sig != "" {
		return str(ev["op"]) == op && str(ev["sig"]) == sig
	}
	if kind == "fx" && fn != "" {
		return str(ev["fn"]) == fn
	}
	return true
}

func want(kind, sig, op, fn string) string {
	switch {
	case sig != "":
		return fmt.Sprintf("%s %s %s", kind, op, sig)
	case fn != "":
		return fmt.Sprintf("%s %s", kind, fn)
	default:
		return kind
	}
}

func (f *feed) popExpect(kind, sig, op, fn string) map[string]any {
	f.skipSems()
	if f.pos >= len(f.events) {
		panic(&replayDivergence{fmt.Sprintf(
			"replay asked for a %q event at position %d but the recording is exhausted — "+
				"the replayed code takes a longer path than the recorded one", kind, f.pos)})
	}
	ev := f.events[f.pos]
	if !f.matches(ev, kind, sig, op, fn) {
		got := str(ev["k"])
		if got == "db" {
			got = fmt.Sprintf("db %s %s", str(ev["op"]), str(ev["sig"]))
		} else if got == "fx" {
			got = fmt.Sprintf("fx %s", str(ev["fn"]))
		}
		panic(&replayDivergence{fmt.Sprintf(
			"boundary divergence at event %d:\n  recorded: %s\n  replayed: %s", f.pos, got, want(kind, sig, op, fn))})
	}
	f.pos++
	f.consumed++
	return ev
}

// replayState serves boundary answers from the feed and captures the replayed code's own
// semantic claims (which are never fed back — testimony is not evidence).
type replayState struct {
	feed *feed
	sems []SemPair
}

func (rs *replayState) now() time.Time {
	ev := rs.feed.popExpect("now", "", "", "")
	t, _ := parseISO(str(ev["v"]))
	return t
}

func (rs *replayState) perf() float64 {
	ev := rs.feed.popExpect("perf", "", "", "")
	return toFloat(ev["v"])
}

func (rs *replayState) expectRand(method string) map[string]any {
	ev := rs.feed.popExpect("rand", "", "", "")
	if m := str(ev["m"]); m != method {
		panic(&replayDivergence{fmt.Sprintf(
			"random divergence: replayed code drew %q but the recording holds a %q draw here", method, m)})
	}
	return ev
}

func (rs *replayState) sample(n, k int) []int {
	ev := rs.expectRand("sample")
	return toIntSlice(ev["idx"])
}

func (rs *replayState) bytes(n int) ([]byte, error) {
	ev := rs.expectRand("bytes")
	return hexDecode(str(ev["hex"]))
}

func (rs *replayState) randFloat() float64 { return toFloat(rs.expectRand("float")["v"]) }
func (rs *replayState) randInt() int        { return int(toFloat(rs.expectRand("int")["v"])) }

func reviveSnap(v any) Snapshot {
	m, _ := v.(map[string]any)
	var id *string
	if s, ok := m["id"].(string); ok {
		id = &s
	}
	exists, _ := m["exists"].(bool)
	var data any
	if d, ok := m["data"]; ok {
		data = serial.FromJsonable(d)
	}
	return Snapshot{ID: id, Exists: exists, Data: data}
}

func (rs *replayState) queryOne(op, sig string) Snapshot {
	ev := rs.feed.popExpect("db", sig, op, "")
	return reviveSnap(ev["res"])
}

func (rs *replayState) query(op, sig string) []Snapshot {
	ev := rs.feed.popExpect("db", sig, op, "")
	arr, _ := ev["res"].([]any)
	out := make([]Snapshot, len(arr))
	for i, s := range arr {
		out[i] = reviveSnap(s)
	}
	return out
}

func (rs *replayState) execCompare(op, sig string, argsJsonable []any) {
	ev := rs.feed.popExpect("db", sig, op, "")
	if !jsonEqual(ev["args"], argsJsonable) {
		rs.feed.writeDivs = append(rs.feed.writeDivs, fmt.Sprintf(
			"%s on %s:\n    recorded: %s\n    replayed: %s", op, sig, jsonString(ev["args"]), jsonString(argsJsonable)))
	}
}

func (rs *replayState) note(name string) {
	rs.sems = append(rs.sems, SemPair{name, "point"})
}

func (rs *replayState) span(ctx context.Context, name string, body func(context.Context) error) (err error) {
	rs.sems = append(rs.sems, SemPair{name, "begin"})
	defer func() {
		// The end still lands whether the body returned, errored, or panicked — the recorded
		// span did, and a shorter sem sequence would look like a changed account.
		rs.sems = append(rs.sems, SemPair{name, "end"})
		if rec := recover(); rec != nil {
			panic(rec)
		}
	}()
	return body(ctx)
}

func replayEffect[T any](rs *replayState, name string, args []any) (T, error) {
	var zero T
	ev := rs.feed.popExpect("fx", "", "", name)
	if !jsonEqual(ev["args"], jsonableSlice(args)) {
		panic(&replayDivergence{fmt.Sprintf(
			"effect %s called with different arguments than recorded:\n  recorded: %s\n  replayed: %s",
			name, jsonString(ev["args"]), jsonString(jsonableSlice(args)))})
	}
	if errObj, ok := ev["err"].(map[string]any); ok {
		return zero, &ReplayedEffectError{Type: str(errObj["type"]), Repr: str(errObj["repr"])}
	}
	var out T
	if b, err := json.Marshal(ev["res"]); err == nil {
		_ = json.Unmarshal(b, &out)
	}
	return out, nil
}

// ReplayReport is the verdict. The three signals are independent: a boundary Divergence says the
// recording is stale, a result/error mismatch says the code produces something else, and
// SemDivergence says the code's own account of what it was doing changed (not gating — that may
// be a refactor as easily as a bug).
type ReplayReport struct {
	Fn             string
	ResultMatch    bool
	ErrorMatch     bool
	Divergence     string
	EventsConsumed int
	EventsTotal    int
	WriteDivs      []string
	SemsRecorded   []SemPair
	SemsReplayed   []SemPair
	SemDivergence  string
	ReplayedResult any
	ReplayedError  string
}

// OK is a strict match: same result, same error, no boundary divergence, no write divergence,
// and every recorded event consumed (the replayed code took neither a shorter nor a longer path).
func (r ReplayReport) OK() bool {
	return r.ResultMatch && r.ErrorMatch && r.Divergence == "" &&
		len(r.WriteDivs) == 0 && r.EventsConsumed == r.EventsTotal
}

// Resolver maps a recorded call (its fn name and revived kwargs) to the function to re-execute.
type Resolver func(fn string, kwargs map[string]any) (func(context.Context) (any, error), error)

// Replay re-executes call `index` of the session at `path` against the code `resolve` returns,
// feeding the recorded answers back, and returns the verdict.
func Replay(path string, index int, resolve Resolver) (*ReplayReport, error) {
	header, calls, err := loadSession(path)
	if err != nil {
		return nil, err
	}
	_ = header
	if index < 0 || index >= len(calls) {
		return nil, fmt.Errorf("call %d out of range: %d call(s) in the session", index, len(calls))
	}
	rec := calls[index]
	events := toMapSlice(rec["events"])
	f := &feed{events: events}
	rs := &replayState{feed: f}

	report := &ReplayReport{
		Fn:           str(rec["fn"]),
		EventsTotal:  len(events),
		SemsRecorded: semPairs(events),
	}

	kwargs, _ := serial.FromJsonable(rec["kwargs"]).(map[string]any)
	fn, err := resolve(report.Fn, kwargs)
	if err != nil {
		return nil, fmt.Errorf("resolving %q: %w", report.Fn, err)
	}

	ctx := context.WithValue(context.Background(), ctxKey{}, &ambient{replay: rs})
	var result any
	var runErr error
	func() {
		defer func() {
			if r := recover(); r != nil {
				if d, ok := r.(*replayDivergence); ok {
					report.Divergence = d.msg
					return
				}
				panic(r)
			}
		}()
		result, runErr = fn(ctx)
	}()

	// Sems trailing the last boundary answer (an outermost span's end, most often) were never
	// reached by a popExpect; leaving them unread would report a shorter path than recorded.
	f.skipSems()

	report.EventsConsumed = f.consumed
	report.WriteDivs = f.writeDivs
	report.SemsReplayed = rs.sems
	report.SemDivergence = semDivergence(report.SemsRecorded, report.SemsReplayed)
	if runErr != nil {
		report.ReplayedError = runErr.Error()
	}
	report.ErrorMatch = report.ReplayedError == recordedError(rec["error"])

	if report.Divergence == "" {
		rj := serial.ToJsonable(result)
		report.ReplayedResult = rj
		report.ResultMatch = jsonEqual(rj, rec["result"])
	}
	return report, nil
}

// --- session loading & small helpers --------------------------------------------------

func loadSession(path string) (header map[string]any, calls []map[string]any, err error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	for _, ln := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(ln) == "" {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal([]byte(ln), &obj); err != nil {
			continue // tolerate a torn final line
		}
		switch str(obj["ev"]) {
		case "session":
			header = obj
		case "call":
			calls = append(calls, obj)
		}
	}
	if header == nil {
		return nil, nil, fmt.Errorf("%s has no session header — not a flight recording?", path)
	}
	return header, calls, nil
}

func toMapSlice(v any) []map[string]any {
	arr, _ := v.([]any)
	out := make([]map[string]any, 0, len(arr))
	for _, e := range arr {
		if m, ok := e.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func semPairs(events []map[string]any) []SemPair {
	var out []SemPair
	for _, e := range events {
		if str(e["k"]) == "sem" {
			out = append(out, SemPair{str(e["name"]), str(e["phase"])})
		}
	}
	return out
}

func semDivergence(recorded, replayed []SemPair) string {
	show := func(p *SemPair) string {
		if p == nil {
			return "nothing"
		}
		return fmt.Sprintf("%q %s", p.Name, p.Phase)
	}
	n := len(recorded)
	if len(replayed) > n {
		n = len(replayed)
	}
	for i := 0; i < n; i++ {
		var a, b *SemPair
		if i < len(recorded) {
			a = &recorded[i]
		}
		if i < len(replayed) {
			b = &replayed[i]
		}
		if a == nil || b == nil || *a != *b {
			return fmt.Sprintf("semantic divergence at %d: recorded %s, replayed %s — "+
				"the code's account of what it was doing has changed", i, show(a), show(b))
		}
	}
	return ""
}

func recordedError(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

func toFloat(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	case json.Number:
		f, _ := n.Float64()
		return f
	default:
		return 0
	}
}

func toIntSlice(v any) []int {
	arr, _ := v.([]any)
	out := make([]int, len(arr))
	for i, x := range arr {
		out[i] = int(toFloat(x))
	}
	return out
}

func hexDecode(s string) ([]byte, error) {
	return hex.DecodeString(s)
}

// jsonEqual compares two jsonable trees by canonical JSON, so 30 (int) and 30.0 (float) — which
// the tape cannot tell apart — compare equal, and map key order does not matter.
func jsonEqual(a, b any) bool {
	ba, ea := json.Marshal(a)
	bb, eb := json.Marshal(b)
	return ea == nil && eb == nil && string(ba) == string(bb)
}

func jsonString(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprintf("%v", v)
	}
	s := string(b)
	if len(s) > 400 {
		s = s[:400]
	}
	return s
}

// parseISO accepts the datetime shapes a tape carries.
func parseISO(s string) (time.Time, bool) {
	for _, layout := range []string{
		"2006-01-02T15:04:05.999999999Z07:00",
		"2006-01-02T15:04:05.999999999",
		"2006-01-02",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}
