package serial

// Traced INTERNAL values are a different problem from boundary values, and this file is the
// difference. A boundary value is an input to be revived faithfully; a traced value is a claim to
// be asserted against, it is captured on every executed line, and it is whatever the code happened
// to be holding — a 10k-row slice, a channel, a struct with a mutex in it.
//
// So: values are DATA, never `%v` strings (you cannot do arithmetic on "2", and `&{0xc000…}` is
// both opaque and different on every run, which would make two traces of the same execution
// unequal). And anything long is cut to a prefix that still knows its true length.
//
// ToTraceJsonable must NEVER panic. It runs inside the instrumented function's own frame; a panic
// there would propagate into the very execution the trace exists to observe, and the tracer would
// have destroyed its own evidence. Anything hostile degrades to an opaque marker instead.

import (
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"sort"
	"time"
)

// Caps for traced values. A local can be enormous and the tracer re-encodes it on every line that
// touches its frame; the head is what an invariant reads, the length is what it counts.
const (
	TraceMaxItems = 100
	TraceMaxChars = 512
)

// Truncated is a sequence the tracer cut to a prefix. Len is the TRUE length, so a claim about
// "how many" is still checkable when the contents are not.
type Truncated struct {
	Head []any
	Len  int
}

// TruncatedText is the string equivalent.
type TruncatedText struct {
	Head string
	Len  int
}

func (t Truncated) String() string     { return fmt.Sprintf("<%d items: %v…>", t.Len, t.Head) }
func (t TruncatedText) String() string { return fmt.Sprintf("<%d chars: %s…>", t.Len, t.Head) }

// ToTraceJsonable encodes one traced internal value. It never panics.
func ToTraceJsonable(v any) (out any) {
	defer func() {
		if r := recover(); r != nil {
			out = opaque(v)
		}
	}()
	return traceEncode(v, 0)
}

func traceEncode(v any, depth int) any {
	if depth > maxDepth {
		return opaque(v)
	}
	if v == nil {
		return nil
	}
	switch t := v.(type) {
	case time.Time:
		return map[string]any{"__dt__": t.Format(isoAware)}
	case *time.Time:
		if t == nil {
			return nil
		}
		return map[string]any{"__dt__": t.Format(isoAware)}
	case error:
		// An error is a value the code is reasoning about, and its message is the whole of what
		// the code can see. Encoding its struct fields would hide that behind an empty map.
		return capString(t.Error())
	}

	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.Bool:
		return rv.Bool()
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return rv.Int()
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr:
		return rv.Uint()
	case reflect.Float32, reflect.Float64:
		f := rv.Float()
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return opaque(v)
		}
		return f
	case reflect.String:
		return capString(rv.String())
	case reflect.Slice, reflect.Array:
		if rv.Kind() == reflect.Slice && rv.Type().Elem().Kind() == reflect.Uint8 {
			b := rv.Bytes()
			head := b
			if len(head) > 32 {
				head = head[:32]
			}
			return map[string]any{"__opaque__": fmt.Sprintf("<bytes %d: %x>", len(b), head)}
		}
		n := rv.Len()
		lim := n
		if lim > TraceMaxItems {
			lim = TraceMaxItems
		}
		head := make([]any, lim)
		for i := 0; i < lim; i++ {
			head[i] = traceEncode(rv.Index(i).Interface(), depth+1)
		}
		if n <= TraceMaxItems {
			return head
		}
		return map[string]any{"__seq__": map[string]any{"len": n, "head": head}}
	case reflect.Map:
		// Go map iteration order is randomized per run; a trace must not be. Sort the keys, or
		// two traces of the same execution would differ for no reason the code is responsible for.
		keys := rv.MapKeys()
		names := make([]string, len(keys))
		byName := map[string]reflect.Value{}
		for i, k := range keys {
			names[i] = fmt.Sprint(k.Interface())
			byName[names[i]] = rv.MapIndex(k)
		}
		sort.Strings(names)
		if len(names) > TraceMaxItems {
			names = names[:TraceMaxItems]
		}
		out := map[string]any{}
		for _, name := range names {
			out[name] = traceEncode(byName[name].Interface(), depth+1)
		}
		return escapeMarker(out)
	case reflect.Struct:
		m, ok := traceStruct(rv, depth).(map[string]any)
		if !ok {
			return opaque(v)
		}
		return escapeMarker(m)
	case reflect.Pointer, reflect.Interface:
		if rv.IsNil() {
			return nil
		}
		return traceEncode(rv.Elem().Interface(), depth) // a deref is not a level of nesting
	default:
		return opaque(v) // func, chan, complex, unsafe.Pointer
	}
}

// traceStruct records exported fields only. An unexported field cannot be read through
// reflection without unsafe, and a tracer that reached for unsafe to read a little more would be
// buying a crash inside the observed frame with someone else's money.
func traceStruct(rv reflect.Value, depth int) any {
	t := rv.Type()
	out := map[string]any{}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if f.PkgPath != "" {
			continue
		}
		out[f.Name] = traceEncode(rv.Field(i).Interface(), depth+1)
	}
	return out
}

var traceMarkers = map[string]bool{
	"__dt__": true, "__date__": true, "__opaque__": true, "__snap__": true,
	"__seq__": true, "__str__": true, "__esc__": true, "__undef__": true,
}

// A user map shaped exactly like a marker would revive as the marker's meaning instead of as
// itself. Escape it so the round trip is honest.
func escapeMarker(m map[string]any) any {
	if len(m) != 1 {
		return m
	}
	for k := range m {
		if traceMarkers[k] {
			return map[string]any{"__esc__": m}
		}
	}
	return m
}

func capString(s string) any {
	r := []rune(s)
	if len(r) <= TraceMaxChars {
		return s
	}
	return map[string]any{"__str__": map[string]any{"len": len(r), "head": string(r[:TraceMaxChars])}}
}

// FromTraceJsonable revives a traced value into something an invariant can assert on. It accepts
// every marker the contract defines, including __snap__, which only the Python tracer emits —
// a trace is a shared format and a Go invariant may be reading a Python runtime's tape.
func FromTraceJsonable(v any) any {
	switch t := v.(type) {
	case map[string]any:
		if len(t) == 1 {
			for k, x := range t {
				switch k {
				case "__dt__", "__date__":
					if s, ok := x.(string); ok {
						if tm, ok := parseISO(s); ok {
							return tm
						}
					}
					return x
				case "__opaque__":
					return x
				case "__undef__":
					return nil
				case "__snap__":
					return FromTraceJsonable(x)
				case "__seq__":
					spec, _ := x.(map[string]any)
					head, _ := spec["head"].([]any)
					out := make([]any, len(head))
					for i, e := range head {
						out[i] = FromTraceJsonable(e)
					}
					return Truncated{Head: out, Len: int(toNum(spec["len"]))}
				case "__str__":
					spec, _ := x.(map[string]any)
					head, _ := spec["head"].(string)
					return TruncatedText{Head: head, Len: int(toNum(spec["len"]))}
				case "__esc__":
					inner, _ := x.(map[string]any)
					out := map[string]any{}
					for ik, iv := range inner {
						out[ik] = FromTraceJsonable(iv)
					}
					return out
				}
			}
		}
		out := map[string]any{}
		for k, x := range t {
			out[k] = FromTraceJsonable(x)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = FromTraceJsonable(x)
		}
		return out
	default:
		return v
	}
}

func toNum(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int:
		return float64(n)
	case int64:
		return float64(n)
	case json.Number:
		f, _ := n.Float64()
		return f
	}
	return 0
}

// Render is a one-line display of a traced value, for a timeline or a failure message.
func Render(v any, limit int) string {
	var s string
	switch t := v.(type) {
	case string:
		s = t
	case Truncated, TruncatedText:
		s = fmt.Sprint(t)
	default:
		if b, err := json.Marshal(ToTraceJsonable(v)); err == nil {
			s = string(b)
		} else {
			s = SafeRepr(v, limit)
		}
	}
	r := []rune(s)
	if len(r) <= limit {
		return s
	}
	return string(r[:limit-1]) + "…"
}
