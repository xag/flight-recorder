package flightrecorder

// Reading a tape: the analysis layer. A tape is data, so this needs no runtime — a Recording
// reads any conformant tape (recorded by any implementation) and recovers its structure: the
// calls, and each call's semantic-span tree with the raw events each span encloses, in order.
//
// It also edits: a recording is data, so a hostile world is one mutation away. Load a call, empty
// a result or run the clock backwards, mark it a probe, and replay the real code against the world
// that never happened.

import (
	"fmt"
	"os"
	"sort"
	"strings"
)

// Recording is a loaded tape: its header and its calls.
type Recording struct {
	Header map[string]any
	calls  []map[string]any
}

// Load reads a tape from a file.
func Load(path string) (*Recording, error) {
	header, calls, err := loadSession(path)
	if err != nil {
		return nil, err
	}
	return &Recording{Header: header, calls: calls}, nil
}

// LoadTape parses a tape from its text.
func LoadTape(text string) (*Recording, error) {
	header, calls, err := parseSession(text)
	if err != nil {
		return nil, err
	}
	return &Recording{Header: header, calls: calls}, nil
}

// NumCalls is how many calls the tape holds.
func (r *Recording) NumCalls() int { return len(r.calls) }

// Call is a view onto call i, through which its events can be inspected and mutated.
func (r *Recording) Call(i int) *CallView {
	if i < 0 || i >= len(r.calls) {
		return nil
	}
	return &CallView{rec: r, index: i, raw: r.calls[i]}
}

// Save writes the (possibly mutated) recording back to a tape file.
func (r *Recording) Save(path string) error {
	var b strings.Builder
	writeLine := func(obj map[string]any) error {
		line, err := marshalStable(obj)
		if err != nil {
			return err
		}
		b.Write(line)
		b.WriteByte('\n')
		return nil
	}
	if err := writeLine(r.Header); err != nil {
		return err
	}
	for _, c := range r.calls {
		if err := writeLine(c); err != nil {
			return err
		}
	}
	return os.WriteFile(path, []byte(b.String()), 0o644)
}

// CallView inspects and edits one recorded call.
type CallView struct {
	rec   *Recording
	index int
	raw   map[string]any
}

// Fn is the recorded function name.
func (c *CallView) Fn() string { return str(c.raw["fn"]) }

// Events are the call's raw boundary events, in order — mutate them in place to visit a world
// that never happened.
func (c *CallView) Events() []map[string]any { return toMapSlice(c.raw["events"]) }

// Event returns the nth event of a given kind (fx/db/now/perf/rand/sem), or nil.
func (c *CallView) Event(kind string, n int) map[string]any {
	seen := 0
	for _, e := range c.Events() {
		if str(e["k"]) == kind {
			if seen == n {
				return e
			}
			seen++
		}
	}
	return nil
}

// MarkProbe flags this call a probe: a mutated upstream answer changes every downstream question,
// so replay stops comparing arguments — name and order still gate.
func (c *CallView) MarkProbe() { c.raw["probe"] = true }

// --- the semantic-span tree ----------------------------------------------------------

// SpanNode is a node of a call's structure: the call itself, a span, or a point note. A span (and
// the call) carries the raw boundary events directly beneath it — those enclosed by no deeper span
// — plus its child spans and notes, in order.
type SpanNode struct {
	Name     string           // the fn name for the call, else the sem name
	Phase    string           // "call" | "span" | "point"
	Outcome  string           // "ok" | "error" for a call/span; "" for a point
	Data     map[string]any   // a sem event's payload
	Events   []map[string]any // raw events directly under this node
	Children []*SpanNode
}

// Spans recovers the call's span tree — the property the whole `sem` event kind exists for,
// recovered from a tape any runtime could have written.
func (c *CallView) Spans() *SpanNode {
	outcome := "ok"
	if c.raw["error"] != nil {
		outcome = "error"
	}
	root := &SpanNode{Name: c.Fn(), Phase: "call", Outcome: outcome}
	stack := []*SpanNode{root}
	top := func() *SpanNode { return stack[len(stack)-1] }

	for _, e := range c.Events() {
		if str(e["k"]) != "sem" {
			top().Events = append(top().Events, e)
			continue
		}
		switch str(e["phase"]) {
		case "begin":
			node := &SpanNode{Name: str(e["name"]), Phase: "span", Data: dataOf(e)}
			top().Children = append(top().Children, node)
			stack = append(stack, node)
		case "end":
			if len(stack) > 1 {
				top().Outcome = str(e["outcome"])
				stack = stack[:len(stack)-1]
			}
		case "point":
			top().Children = append(top().Children, &SpanNode{
				Name: str(e["name"]), Phase: "point", Data: dataOf(e)})
		}
	}
	return root
}

// RenderSpans is a top-down, human-readable rendering of the span tree — the same shape the
// Python and .NET readers produce, so a tape reads identically whoever wrote it.
func (c *CallView) RenderSpans() string {
	var b strings.Builder
	var walk func(n *SpanNode, depth int)
	walk = func(n *SpanNode, depth int) {
		indent := strings.Repeat("  ", depth)
		if n.Phase == "point" {
			b.WriteString(fmt.Sprintf("%s- %s%s\n", indent, n.Name, renderData(n.Data)))
			return
		}
		outcome := "ok"
		if n.Outcome == "error" {
			outcome = "ERROR"
		}
		b.WriteString(fmt.Sprintf("%s%s  %s%s\n", indent, n.Name, outcome, renderCount(n.Events)))
		for _, ch := range n.Children {
			walk(ch, depth+1)
		}
	}
	walk(c.Spans(), 0)
	return strings.TrimRight(b.String(), "\n")
}

func dataOf(e map[string]any) map[string]any {
	if d, ok := e["data"].(map[string]any); ok {
		return d
	}
	return nil
}

func renderCount(events []map[string]any) string {
	if len(events) == 0 {
		return ""
	}
	kinds := map[string]int{}
	for _, e := range events {
		kinds[str(e["k"])]++
	}
	if len(kinds) == 1 {
		for k := range kinds {
			return fmt.Sprintf("  (%d %s)", len(events), k)
		}
	}
	return fmt.Sprintf("  (%d events)", len(events))
}

func renderData(data map[string]any) string {
	if len(data) == 0 {
		return ""
	}
	keys := make([]string, 0, len(data))
	for k := range data {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		v := data[k]
		if s, ok := v.(string); ok {
			parts[i] = fmt.Sprintf("%s=%q", k, s)
		} else {
			parts[i] = fmt.Sprintf("%s=%v", k, v)
		}
	}
	return "  " + strings.Join(parts, " ")
}
