package flightrecorder

// Variable-level tracing: every local, on every executed line, of the code you name.
//
// This is the thing that turns "what was `level` when it went wrong?" from an inference into a
// lookup — and, with it, an invariant can assert over an INTERNAL variable, which is the form
// that catches a bug whose output is perfectly self-consistent and still wrong. Python gets it
// from sys.settrace. Node gets it from the V8 Inspector. Go has neither.
//
// WHY SOURCE INSTRUMENTATION, AND WHAT WAS REJECTED
//
// Go is compiled ahead of time and its runtime exposes no per-line hook. There is no settrace to
// port and no inspector protocol to drive. That leaves two families, and only two:
//
//   - Delve, the debugger, driven headless: a breakpoint on every line of every traced file, read
//     the locals at each stop. This is the closest structural analogue to what Node does over the
//     V8 Inspector, and it is what we did NOT choose. It buys nothing here. Delve still needs the
//     traced code compiled into a binary it can launch, so it does not avoid the out-of-process
//     run — it merely adds, on top of it, an external `dlv` executable the user must install, a
//     version handshake against the Go toolchain that breaks on every new Go release before Delve
//     catches up, and a JSON-RPC round trip per variable per line. Worst of all, the values come
//     back as the DEBUGGER's rendering — strings, truncated by the debugger's own rules — when the
//     whole point of trace version 2 is that values are data an invariant can do arithmetic on.
//
//   - Source instrumentation via go/ast: parse the named files, insert an observation call before
//     each statement, print, compile, run. Stdlib only, no external binary, cross-platform, and
//     essentially how `go tool cover` works — a mechanism the Go project itself ships. Values are
//     encoded IN PROCESS by serial.ToTraceJsonable, from the live typed value, so a traced int is
//     an int. That is the one we built.
//
// THE COST WE ACCEPTED, STATED PLAINLY. Instrumented code has to be compiled, so the traced run
// happens in a SEPARATE PROCESS: RunTraced copies the module to a temp tree, rewrites the named
// files there, and runs a `go` command inside the copy with the tracer armed. The original tree
// is never touched. The trace comes back as a file. This is more moving parts than a settrace
// callback and there is no way around it in a language with no line hook; what we could choose
// was to keep the parts stdlib, and we did.
//
// Tracing is for REPLAY. It writes an event per changed local per executed line; performance is
// explicitly not a goal, and nothing here belongs in a request path.

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/xag/flight-recorder/go/serial"
	"github.com/xag/flight-recorder/go/tracehook"
)

// Obs is one sighting of a named variable, at the line whose execution produced it.
type Obs struct {
	At    string // "file.go:12"
	Fn    string // the function whose frame held it
	Name  string
	Value any
}

func (o Obs) String() string {
	return fmt.Sprintf("%s=%s at %s in %s", o.Name, serial.Render(o.Value, 90), o.At, o.Fn)
}

// TraceCall is one entry into an instrumented function, with the arguments it arrived with.
type TraceCall struct {
	At   string
	Fn   string
	Args map[string]any
}

// TraceReturn is one return, with the value that came back (a list, for a multi-result function).
type TraceReturn struct {
	At    string
	Fn    string
	Value any
}

// TraceRaise is one panic on the way out of an instrumented function.
type TraceRaise struct {
	At     string
	Fn     string
	Type   string
	Detail string
}

// Trace is a traced execution's internal state, queryable.
//
// Every traced value is data (see serial.ToTraceJsonable): numbers compare, documents are maps,
// and anything long is a prefix that still reports its true length.
type Trace struct {
	Events []map[string]any
}

// LoadTrace reads a trace file written by any runtime's tracer.
func LoadTrace(path string) (*Trace, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return ParseTrace(string(data))
}

// ParseTrace reads the JSONL trace format. A tape and a trace are different artifacts with
// different formats, and this reads the second one.
func ParseTrace(text string) (*Trace, error) {
	var events []map[string]any
	for _, ln := range strings.Split(text, "\n") {
		if strings.TrimSpace(ln) == "" {
			continue
		}
		var ev map[string]any
		if err := json.Unmarshal([]byte(ln), &ev); err != nil {
			continue // tolerate a torn final line: a truncated trace is still evidence
		}
		events = append(events, ev)
	}
	if len(events) > 0 && str(events[0]["e"]) == "H" {
		if v := int(toFloat(events[0]["trace_version"])); v != tracehook.TraceVersion {
			// A version-1 trace holds reprs, and asserting arithmetic over reprs fails
			// confusingly rather than loudly. Traces are cheap: regenerate.
			return nil, fmt.Errorf("this trace was written by an older tracer (version %d, need %d) "+
				"— re-run the traced replay to regenerate it", v, tracehook.TraceVersion)
		}
		events = events[1:]
	}
	return &Trace{Events: events}, nil
}

// LiveTrace is the trace this process has recorded so far, from `from` events onward. It is empty
// in an ordinary build; inside a RunTraced child it is the running commentary on the code that is
// executing right now, which is how an invariant gets a trace of the replay it is judging.
func LiveTrace(from int) *Trace {
	all := tracehook.Events()
	if from > len(all) {
		from = len(all)
	}
	evs := all[from:]
	out := make([]map[string]any, 0, len(evs))
	for _, e := range evs {
		if str(e["e"]) != "H" {
			out = append(out, e)
		}
	}
	return &Trace{Events: out}
}

// Tracing reports whether this process is running instrumented, with the tracer armed. A test
// that drives its own traced child branches on it: false is the orchestrating parent, true is the
// child where the observed code actually runs.
func Tracing() bool { return tracehook.Live() }

// Len is how many events the trace holds.
func (t *Trace) Len() int {
	if t == nil {
		return 0
	}
	return len(t.Events)
}

// Values is the timeline of one variable: every value it held, in order, and where — the
// arguments it arrived with and each line that changed it.
//
// An output can be entirely self-consistent and still be produced by a wrong internal value. That
// value is only visible here.
func (t *Trace) Values(name string) []Obs {
	var out []Obs
	if t == nil {
		return out
	}
	for _, e := range t.Events {
		var bag map[string]any
		switch str(e["e"]) {
		case "L":
			bag, _ = e["d"].(map[string]any)
		case "C":
			bag, _ = e["args"].(map[string]any)
		}
		if bag == nil {
			continue
		}
		if v, ok := bag[name]; ok {
			// The tracer already emits only changes, so every entry here is a transition. There
			// is no second filter to apply and no unchanged value to hide.
			out = append(out, Obs{At: str(e["at"]), Fn: str(e["fn"]), Name: name,
				Value: serial.FromTraceJsonable(v)})
		}
	}
	return out
}

// First is the value a variable arrived with, or nil if it was never observed.
func (t *Trace) First(name string) *Obs {
	vs := t.Values(name)
	if len(vs) == 0 {
		return nil
	}
	return &vs[0]
}

// Final is the last value a variable held, or nil if it was never observed.
func (t *Trace) Final(name string) *Obs {
	vs := t.Values(name)
	if len(vs) == 0 {
		return nil
	}
	return &vs[len(vs)-1]
}

// Names is every distinct variable the trace ever saw, sorted.
func (t *Trace) Names() []string {
	seen := map[string]bool{}
	if t != nil {
		for _, e := range t.Events {
			for _, key := range []string{"d", "args"} {
				if bag, ok := e[key].(map[string]any); ok {
					for k := range bag {
						seen[k] = true
					}
				}
			}
		}
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// matchFn accepts the empty filter, an exact name, or a bare name against a qualified one — so
// Calls("studyStatus") finds "pkg.studyStatus" without the caller knowing how it was qualified.
func matchFn(want, got string) bool {
	return want == "" || got == want || strings.HasSuffix(got, "."+want)
}

// Calls is every entry into a function, with its arguments.
func (t *Trace) Calls(fn string) []TraceCall {
	var out []TraceCall
	if t == nil {
		return out
	}
	for _, e := range t.Events {
		if str(e["e"]) != "C" || !matchFn(fn, str(e["fn"])) {
			continue
		}
		args := map[string]any{}
		if bag, ok := e["args"].(map[string]any); ok {
			for k, v := range bag {
				args[k] = serial.FromTraceJsonable(v)
			}
		}
		out = append(out, TraceCall{At: str(e["at"]), Fn: str(e["fn"]), Args: args})
	}
	return out
}

// Returns is every return out of a function, with the value it produced.
func (t *Trace) Returns(fn string) []TraceReturn {
	var out []TraceReturn
	if t == nil {
		return out
	}
	for _, e := range t.Events {
		if str(e["e"]) != "R" || !matchFn(fn, str(e["fn"])) {
			continue
		}
		out = append(out, TraceReturn{At: str(e["at"]), Fn: str(e["fn"]),
			Value: serial.FromTraceJsonable(e["v"])})
	}
	return out
}

// Raised is every panic the trace saw leave an instrumented function.
func (t *Trace) Raised() []TraceRaise {
	var out []TraceRaise
	if t == nil {
		return out
	}
	for _, e := range t.Events {
		if str(e["e"]) != "X" {
			continue
		}
		out = append(out, TraceRaise{At: str(e["at"]), Fn: str(e["fn"]),
			Type: str(e["type"]), Detail: str(e["v"])})
	}
	return out
}

// Render is one variable's timeline, for a human or a failure message. A trace nobody can read is
// a trace nobody consults.
func (t *Trace) Render(name string) string {
	vs := t.Values(name)
	if len(vs) == 0 {
		return name + ": never observed"
	}
	var b strings.Builder
	for _, o := range vs {
		fmt.Fprintf(&b, "  %-28s %s = %s\n", o.At, name, serial.Render(o.Value, 90))
	}
	return strings.TrimRight(b.String(), "\n")
}

// Timeline renders the whole trace: calls, changed locals, returns and panics, in order.
func (t *Trace) Timeline() string {
	var b strings.Builder
	if t == nil {
		return ""
	}
	for _, e := range t.Events {
		at, fn := str(e["at"]), str(e["fn"])
		switch str(e["e"]) {
		case "C":
			fmt.Fprintf(&b, "  %-28s call %s(%s)\n", at, fn, renderBag(e["args"]))
		case "L":
			fmt.Fprintf(&b, "  %-28s %s\n", at, renderBag(e["d"]))
		case "R":
			fmt.Fprintf(&b, "  %-28s return %s\n", at, serial.Render(serial.FromTraceJsonable(e["v"]), 90))
		case "X":
			fmt.Fprintf(&b, "  %-28s PANIC %s: %s\n", at, str(e["type"]), str(e["v"]))
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func renderBag(v any) string {
	bag, _ := v.(map[string]any)
	keys := make([]string, 0, len(bag))
	for k := range bag {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+serial.Render(serial.FromTraceJsonable(bag[k]), 60))
	}
	return strings.Join(parts, ", ")
}
