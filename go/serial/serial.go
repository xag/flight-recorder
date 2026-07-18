// Package serial is the Go half of the tape-v1 "Value encoding" contract (spec/tape-v1.md):
// everything crossing the recorded boundary becomes JSON with revivable markers for
// datetimes, and anything the tape cannot represent degrades to an opaque marker rather
// than breaking the recorded call. The failure direction is always "the recording is a bit
// poorer", never "the app broke because it was being recorded".
//
// This mirrors flight_recorder/serial.py and js/src/serial.js. Where they branch on a
// dynamic type, Go branches on reflection.
package serial

import (
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"regexp"
	"strings"
	"time"
)

const maxDepth = 16

// Redacted is what a field's value becomes under a bare (nil) rule, or when a rule/scrub panics.
const Redacted = "[REDACTED]"

// A memory address in a rendered value is a POINTER — different on every run — so recording it
// would make the effect it belongs to never match on replay. Scrub it, exactly as the Python
// and JS recorders do.
var addrRe = regexp.MustCompile(` 0x[0-9a-fA-F]+`)

// Datetime layouts. A Go time.Time is always located, so the "naive vs aware" split the Python
// codec preserves does not arise on encode; on decode we accept both, plus a date-only value
// produced by another runtime.
const (
	isoAware = "2006-01-02T15:04:05.999999999Z07:00"
	isoNaive = "2006-01-02T15:04:05.999999999"
	isoDate  = "2006-01-02"
)

// SafeRepr renders any value for an opaque marker, with memory addresses scrubbed and length
// capped. It never panics.
func SafeRepr(v any, limit int) string {
	s := addrRe.ReplaceAllString(fmt.Sprintf("<%T %v>", v, v), "")
	r := []rune(s)
	if len(r) <= limit {
		return s
	}
	return string(r[:limit-1]) + "…"
}

func opaque(v any) map[string]any {
	return map[string]any{"__opaque__": SafeRepr(v, 200)}
}

// ToJsonable encodes one boundary value into a jsonable tree with markers. The result contains
// only nil, bool, integers, float64, string, []any, map[string]any, and single-key markers —
// exactly the surface the tape-v1 checker accepts.
func ToJsonable(v any) any { return encode(v, 0) }

func encode(v any, depth int) any {
	if depth > maxDepth {
		return opaque(v)
	}
	if v == nil {
		return nil
	}
	// time before the reflection switch: it is data with a marker, not a struct to walk.
	switch t := v.(type) {
	case time.Time:
		return map[string]any{"__dt__": t.Format(isoAware)}
	case *time.Time:
		if t == nil {
			return nil
		}
		return map[string]any{"__dt__": t.Format(isoAware)}
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
		if math.IsNaN(f) || math.IsInf(f, 0) { // NaN/±Inf are not JSON
			return opaque(v)
		}
		return f
	case reflect.String:
		return rv.String()
	case reflect.Slice, reflect.Array:
		// Raw bytes are entropy or a payload, not structure: hex, tagged opaque, like JS.
		if rv.Kind() == reflect.Slice && rv.Type().Elem().Kind() == reflect.Uint8 {
			b := rv.Bytes()
			head := b
			if len(head) > 32 {
				head = head[:32]
			}
			return map[string]any{"__opaque__": fmt.Sprintf("<bytes %d: %x>", len(b), head)}
		}
		out := make([]any, rv.Len())
		for i := 0; i < rv.Len(); i++ {
			out[i] = encode(rv.Index(i).Interface(), depth+1)
		}
		return out
	case reflect.Map:
		out := map[string]any{}
		for _, k := range rv.MapKeys() {
			out[fmt.Sprint(k.Interface())] = encode(rv.MapIndex(k).Interface(), depth+1)
		}
		return out
	case reflect.Struct:
		return encodeStruct(rv, depth)
	case reflect.Pointer, reflect.Interface:
		if rv.IsNil() {
			return nil
		}
		return encode(rv.Elem().Interface(), depth) // a deref is not a level of nesting
	default:
		return opaque(v) // func, chan, complex, unsafe.Pointer
	}
}

// encodeStruct records the struct's exported fields — the data surface an app reads and writes
// at a boundary — honoring the `json` tag's name and its `-` skip. Reviving a struct as itself
// is impossible (as with a JS class instance), so revival yields a generic map; the tape keeps
// the surface, not the type.
func encodeStruct(rv reflect.Value, depth int) any {
	t := rv.Type()
	out := map[string]any{}
	for i := 0; i < t.NumField(); i++ {
		f := t.Field(i)
		if f.PkgPath != "" {
			continue // unexported
		}
		name := f.Name
		if tag, ok := f.Tag.Lookup("json"); ok {
			first := strings.Split(tag, ",")[0]
			if first == "-" {
				continue
			}
			if first != "" {
				name = first
			}
		}
		out[name] = encode(rv.Field(i).Interface(), depth+1)
	}
	return out
}

func parseISO(s string) (time.Time, bool) {
	for _, layout := range []string{isoAware, isoNaive, isoDate} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// FromJsonable revives a boundary value. __opaque__ is a one-way door by design — it revives as
// its text. __undef__ (which only another runtime emits) revives to nil, the same as null.
func FromJsonable(v any) any {
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
				case "__undef__":
					return nil
				case "__opaque__":
					return x
				}
			}
		}
		out := map[string]any{}
		for k, x := range t {
			out[k] = FromJsonable(x)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = FromJsonable(x)
		}
		return out
	default:
		return v
	}
}

// Rules redacts by FIELD NAME: a jsonable dict entry whose key is named here has its value
// replaced — by Redacted when the rule is nil, else by the rule's output. A rule that panics
// degrades to Redacted.
type Rules map[string]func(any) any

// Scrub redacts by VALUE: it sweeps every leaf string wherever it sits, catching secrets that
// no field name can see — a positional arg, a key built by interpolation, prose in a body. It
// MUST be idempotent: replay re-derives the question, scrubs it the same way, and compares, so
// a value that is already a mask must scrub to itself.
type Scrub func(string) string

func safeApply(fn func(any) any, x any) (res any) {
	defer func() {
		if r := recover(); r != nil {
			res = Redacted
		}
	}()
	return fn(x)
}

func safeScrub(fn Scrub, s string) (res string) {
	defer func() {
		if r := recover(); r != nil {
			res = Redacted
		}
	}()
	return fn(s)
}

// Redact applies field-name Rules and value Scrub to a jsonable tree. The failure direction is
// always "masked", never "leaked" and never "broke the recorded call".
func Redact(v any, rules Rules, scrub Scrub) any {
	hasRules := len(rules) > 0
	if !hasRules && scrub == nil {
		return v
	}
	leaf := func(x any) any {
		s, ok := x.(string)
		if !ok || scrub == nil {
			return x
		}
		return safeScrub(scrub, s)
	}
	switch t := v.(type) {
	case []any:
		out := make([]any, len(t))
		for i, x := range t {
			out[i] = Redact(x, rules, scrub)
		}
		return out
	case map[string]any:
		out := map[string]any{}
		for k, x := range t {
			if hasRules {
				if rule, present := rules[k]; present {
					if rule == nil {
						out[k] = Redacted
					} else {
						out[k] = leaf(safeApply(rule, x))
					}
					continue
				}
			}
			out[k] = Redact(x, rules, scrub)
		}
		return out
	default:
		return leaf(v)
	}
}

// Short is a compact, stable rendering of a chained-call argument, for db signatures.
func Short(v any, limit int) string {
	var s string
	if b, err := json.Marshal(ToJsonable(v)); err == nil {
		s = string(b)
	} else {
		s = SafeRepr(v, limit)
	}
	r := []rune(s)
	if len(r) <= limit {
		return s
	}
	return string(r[:limit-1]) + "…"
}
