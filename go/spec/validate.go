// Package spec is the Tape v1 conformance checker for Go — the mirror of the
// normative Python arbiter (spec/validate.py) and the JS one (js/src/spec/validate.js).
//
// Like its siblings it is written against nothing but the JSON: it imports no part of
// the Go flight-recorder implementation, so it cannot accidentally bless whatever that
// implementation happens to do. All three checkers must agree on every fixture in
// spec/fixtures/, and every fixture must have been produced by an implementation.
//
// ValidateTape returns a list of human-readable violations; empty means conformant.
package spec

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	version  = 1
	maxDepth = 16
)

var (
	// __undef__ exists for JavaScript, which has two nothings. Python and Go have one, so
	// this runtime never emits it and revives it as nil — the marker costs this runtime
	// nothing and buys the other one exact fidelity.
	markers = map[string]bool{"__dt__": true, "__date__": true, "__undef__": true, "__opaque__": true}
	// Reserved by the trace encoding — a *reader* must tolerate them, so they are legal in a
	// tape even though a v1 recorder never emits them.
	reservedMarkers = map[string]bool{"__snap__": true, "__seq__": true, "__str__": true, "__esc__": true}
	eventKinds      = map[string]bool{"fx": true, "db": true, "now": true, "perf": true, "rand": true, "sem": true}
	semPhases       = map[string]bool{"begin": true, "end": true, "point": true}
	hexRe           = regexp.MustCompile(`^[0-9a-f]+$`)
)

// Python's datetime.fromisoformat is permissive; these three layouts cover what it accepts
// for the shapes a tape carries. A "9s" fractional makes the fraction optional on parse, so
// one datetime layout matches both "…:05" and "…:05.996081".
const (
	isoAware = "2006-01-02T15:04:05.999999999Z07:00"
	isoNaive = "2006-01-02T15:04:05.999999999"
	isoDate  = "2006-01-02"
)

func isISOStr(s string) bool {
	if _, err := time.Parse(isoAware, s); err == nil {
		return true
	}
	if _, err := time.Parse(isoNaive, s); err == nil {
		return true
	}
	if _, err := time.Parse(isoDate, s); err == nil {
		return true
	}
	return false
}

func isISO(v any) bool {
	s, ok := v.(string)
	return ok && isISOStr(s)
}

func isTZAware(v any) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	_, err := time.Parse(isoAware, s)
	return err == nil
}

// asInt mirrors Python's isinstance(x, int): a JSON integer literal, never a float, never a
// bool. UseNumber preserves the literal so "1" is an int but "1.0" is not.
func asInt(v any) (int, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	s := string(n)
	if strings.ContainsAny(s, ".eE") {
		return 0, false
	}
	i, err := strconv.Atoi(s)
	if err != nil {
		return 0, false
	}
	return i, true
}

func isNumber(v any) bool {
	_, ok := v.(json.Number)
	return ok
}

func asFloat(v any) (float64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	if err != nil {
		return 0, false
	}
	return f, true
}

// checkValue: a boundary value is JSON, with at most a marker at any node.
func checkValue(v any, path string, out *[]string, depth int) {
	if depth > maxDepth {
		*out = append(*out, fmt.Sprintf("%s: nested deeper than %d; must degrade to __opaque__", path, maxDepth))
		return
	}
	switch t := v.(type) {
	case nil, string, bool, json.Number:
		return
	case []any:
		for i, x := range t {
			checkValue(x, fmt.Sprintf("%s[%d]", path, i), out, depth+1)
		}
	case map[string]any:
		if len(t) == 1 {
			var k string
			for kk := range t {
				k = kk
			}
			if markers[k] {
				switch k {
				case "__dt__", "__date__":
					if !isISO(t[k]) {
						*out = append(*out, fmt.Sprintf("%s: %s payload is not ISO-8601: %v", path, k, t[k]))
					}
				case "__undef__":
					if b, ok := t[k].(bool); !ok || !b {
						*out = append(*out, fmt.Sprintf("%s: __undef__ payload must be true", path))
					}
				case "__opaque__":
					if s, ok := t[k].(string); !ok {
						*out = append(*out, fmt.Sprintf("%s: __opaque__ payload must be a string", path))
					} else if len(s) > 200 {
						*out = append(*out, fmt.Sprintf("%s: __opaque__ payload exceeds 200 chars", path))
					}
				}
				return
			}
			if reservedMarkers[k] {
				return // reserved: legal, not interpreted here
			}
		}
		for k, x := range t {
			checkValue(x, path+"."+k, out, depth+1)
		}
	default:
		*out = append(*out, fmt.Sprintf("%s: %T is not JSON", path, v))
	}
}

func checkSnapshot(s any, path string, out *[]string) {
	m, ok := s.(map[string]any)
	if !ok {
		*out = append(*out, path+": snapshot must be an object")
		return
	}
	for _, key := range []string{"id", "exists", "data"} {
		if _, present := m[key]; !present {
			*out = append(*out, fmt.Sprintf("%s: snapshot missing '%s'", path, key))
		}
	}
	if ex, present := m["exists"]; present {
		if _, isb := ex.(bool); !isb {
			*out = append(*out, path+".exists: must be a bool")
		}
	}
	if d, present := m["data"]; present {
		checkValue(d, path+".data", out, 0)
	}
}

func checkEvent(e any, path string, out *[]string) {
	m, ok := e.(map[string]any)
	if !ok {
		*out = append(*out, path+": event must be an object")
		return
	}
	k, _ := m["k"].(string)
	if !eventKinds[k] {
		return // unknown kind: a reader must ignore it (forward compatibility)
	}

	switch k {
	case "fx":
		if _, ok := m["fn"].(string); !ok {
			*out = append(*out, path+": fx needs a string 'fn'")
		}
		if args, ok := m["args"].([]any); !ok {
			*out = append(*out, path+": fx needs an array 'args'")
		} else {
			checkValue(args, path+".args", out, 0)
		}
		if kw, ok := m["kwargs"].(map[string]any); !ok {
			*out = append(*out, path+": fx needs an object 'kwargs' ({} in JS)")
		} else {
			checkValue(kw, path+".kwargs", out, 0)
		}
		_, hasRes := m["res"]
		_, hasErr := m["err"]
		if hasRes == hasErr {
			*out = append(*out, path+": fx must carry exactly one of 'res' / 'err'")
		}
		if hasRes {
			checkValue(m["res"], path+".res", out, 0)
		}
		if hasErr {
			em, ok := m["err"].(map[string]any)
			if !ok {
				*out = append(*out, path+".err: must be an object with a string 'type'")
			} else if _, ok := em["type"].(string); !ok {
				*out = append(*out, path+".err: must be an object with a string 'type'")
			}
		}

	case "db":
		if _, ok := m["op"].(string); !ok {
			*out = append(*out, path+": db needs a string 'op'")
		}
		if _, ok := m["sig"].(string); !ok {
			*out = append(*out, path+": db needs a string 'sig'")
		}
		_, hasRes := m["res"]
		_, hasArgs := m["args"]
		if hasRes && hasArgs {
			*out = append(*out, path+": db carries 'res' (a read) or 'args' (a write), never both")
		}
		if !hasRes && !hasArgs {
			*out = append(*out, path+": db must carry 'res' or 'args'")
		}
		if hasRes {
			if arr, ok := m["res"].([]any); ok {
				for i, s := range arr {
					checkSnapshot(s, fmt.Sprintf("%s.res[%d]", path, i), out)
				}
			} else {
				checkSnapshot(m["res"], path+".res", out)
			}
		}
		if hasArgs {
			checkValue(m["args"], path+".args", out, 0)
		}

	case "now":
		// ISO-8601, and deliberately NOT required to be timezone-aware: an app-visible value,
		// round-tripped exactly as the app received it.
		if !isISO(m["v"]) {
			*out = append(*out, fmt.Sprintf("%s: now.v must be an ISO-8601 string, got %v", path, m["v"]))
		}

	case "perf":
		// A separate clock from now: monotonic, arbitrary origin, not a wall time.
		if !isNumber(m["v"]) {
			*out = append(*out, fmt.Sprintf("%s: perf.v must be a number (milliseconds), got %v", path, m["v"]))
		}

	case "sem":
		// Testimony, not evidence. Validate its SHAPE only; `name` is the app's own vocabulary
		// and no implementation may interpret it.
		if name, ok := m["name"].(string); !ok || name == "" {
			*out = append(*out, path+": sem needs a non-empty string 'name'")
		}
		phase, _ := m["phase"].(string)
		if !semPhases[phase] {
			*out = append(*out, fmt.Sprintf("%s: sem.phase must be one of begin|end|point, got %v", path, m["phase"]))
		}
		if _, ok := asInt(m["sid"]); !ok {
			*out = append(*out, path+": sem needs an int 'sid', unique within the call")
		}
		if d, present := m["data"]; present {
			if dm, ok := d.(map[string]any); !ok {
				*out = append(*out, path+": sem.data must be an object")
			} else {
				checkValue(dm, path+".data", out, 0)
			}
		}
		if oc, present := m["outcome"]; present {
			if phase != "end" {
				*out = append(*out, fmt.Sprintf("%s: sem.outcome belongs to an 'end', not a %v", path, m["phase"]))
			}
			if s, _ := oc.(string); s != "ok" && s != "error" {
				*out = append(*out, fmt.Sprintf("%s: sem.outcome must be 'ok' or 'error', got %v", path, oc))
			}
		}

	case "rand":
		checkRand(m, path, out)
	}
}

func checkRand(m map[string]any, path string, out *[]string) {
	mm, _ := m["m"].(string)
	switch mm {
	case "sample":
		for _, key := range []string{"n", "kk"} {
			if _, ok := asInt(m[key]); !ok {
				*out = append(*out, fmt.Sprintf("%s: rand.%s must be an int", path, key))
			}
		}
		idx, isArr := m["idx"].([]any)
		allInts := isArr
		var idxVals []int
		if isArr {
			for _, x := range idx {
				iv, iok := asInt(x)
				if !iok {
					allInts = false
					break
				}
				idxVals = append(idxVals, iv)
			}
		}
		if !allInts {
			*out = append(*out, path+": rand.idx must be an array of ints")
		} else if n, nok := asInt(m["n"]); nok {
			var bad []int
			for _, i := range idxVals {
				if i < 0 || i >= n {
					bad = append(bad, i)
				}
			}
			if len(bad) > 0 {
				*out = append(*out, fmt.Sprintf("%s: rand.idx %v out of range for population %d", path, bad, n))
			}
			if kk, kok := asInt(m["kk"]); kok && len(idxVals) != kk {
				*out = append(*out, fmt.Sprintf("%s: rand.idx has %d positions but kk=%d", path, len(idxVals), kk))
			}
		}
	case "bytes":
		n, nok := asInt(m["n"])
		if !nok || n < 0 {
			*out = append(*out, path+": rand.n must be a non-negative int")
		}
		hx, hok := m["hex"].(string)
		if !hok || (hx != "" && !hexRe.MatchString(hx)) {
			*out = append(*out, path+": rand.hex must be a lowercase hex string")
		} else if nok && len(hx) != 2*n {
			*out = append(*out, fmt.Sprintf("%s: rand.hex is %d chars but n=%d implies %d", path, len(hx), n, 2*n))
		}
	case "float":
		f, ok := asFloat(m["v"])
		if !ok || !(f >= 0.0 && f < 1.0) {
			*out = append(*out, fmt.Sprintf("%s: rand.v must be a number in [0, 1), got %v", path, m["v"]))
		}
	case "int":
		if _, ok := asInt(m["v"]); !ok {
			*out = append(*out, fmt.Sprintf("%s: rand.v must be an int, got %v", path, m["v"]))
		}
	default:
		*out = append(*out, fmt.Sprintf("%s: rand.m must be one of sample|bytes|float|int, got %v", path, m["m"]))
	}
}

type semFrame struct {
	sid  int
	name string
}

// checkSemNesting: the one structural promise sem makes — begin/end pairs are well-nested
// within a call. Enclosure is derived from ORDER, so nesting is the only thing that makes
// the derivation sound.
func checkSemNesting(evs []any, path string, out *[]string) {
	var stack []semFrame
	seen := map[int]bool{}
	for j, e := range evs {
		m, ok := e.(map[string]any)
		if !ok {
			continue
		}
		if k, _ := m["k"].(string); k != "sem" {
			continue
		}
		sid, sok := asInt(m["sid"])
		phase, _ := m["phase"].(string)
		name, _ := m["name"].(string)
		if !sok || !semPhases[phase] {
			continue // already reported by checkEvent; do not compound it
		}

		if phase == "begin" || phase == "point" {
			if seen[sid] {
				*out = append(*out, fmt.Sprintf("%s.events[%d]: sem sid %d is reused — a sid must be "+
					"unique within the call, or an 'end' cannot name its 'begin'", path, j, sid))
			}
			seen[sid] = true
			if phase == "begin" {
				stack = append(stack, semFrame{sid, name})
			}
		} else { // end
			if len(stack) == 0 {
				*out = append(*out, fmt.Sprintf("%s.events[%d]: sem 'end' (sid %d) with no open span", path, j, sid))
			} else if stack[len(stack)-1].sid != sid {
				open := stack[len(stack)-1]
				*out = append(*out, fmt.Sprintf("%s.events[%d]: sem spans are not well-nested — 'end' closes sid "+
					"%d while sid %d (%q) is still open. Spans nest; they never straddle.", path, j, sid, open.sid, open.name))
				// Unwind to it if it is open at all, so one crossing is not reported N times.
				present := false
				for _, f := range stack {
					if f.sid == sid {
						present = true
						break
					}
				}
				if present {
					for len(stack) > 0 && stack[len(stack)-1].sid != sid {
						stack = stack[:len(stack)-1]
					}
					if len(stack) > 0 {
						stack = stack[:len(stack)-1]
					}
				}
			} else {
				stack = stack[:len(stack)-1]
			}
		}
	}
	for _, f := range stack {
		*out = append(*out, fmt.Sprintf("%s: sem span %q (sid %d) is never closed — a completed call "+
			"holds no open spans", path, f.name, f.sid))
	}
}

func validateLine(obj any, i int, out *[]string, first bool) {
	m, ok := obj.(map[string]any)
	if !ok {
		*out = append(*out, fmt.Sprintf("line %d: not an object", i))
		return
	}
	ev, _ := m["ev"].(string)

	if first {
		if ev != "session" {
			*out = append(*out, fmt.Sprintf("line %d: the first line must be the session header, got ev=%v", i, m["ev"]))
			return
		}
	} else if ev == "session" {
		*out = append(*out, fmt.Sprintf("line %d: a second session header", i))
		return
	}

	if ev == "session" {
		if v, ok := asInt(m["version"]); !ok || v != version {
			*out = append(*out, fmt.Sprintf("line %d: version must be %d, got %v", i, version, m["version"]))
		}
		if !isTZAware(m["started"]) {
			*out = append(*out, fmt.Sprintf("line %d: session.started must be timezone-aware ISO-8601", i))
		}
		if c, ok := m["constants"].(map[string]any); !ok {
			*out = append(*out, fmt.Sprintf("line %d: session.constants must be an object", i))
		} else {
			checkValue(c, fmt.Sprintf("line %d.constants", i), out, 0)
		}
		var runtimes []string
		for _, rk := range []string{"python", "node", "dotnet", "go", "java"} {
			if _, present := m[rk]; present {
				runtimes = append(runtimes, rk)
			}
		}
		if len(runtimes) != 1 {
			*out = append(*out, fmt.Sprintf("line %d: session must name exactly one runtime (python|node|dotnet|go), got %v", i, runtimes))
		}
		return
	}

	if ev == "call" {
		if seq, ok := asInt(m["seq"]); !ok || seq < 1 {
			*out = append(*out, fmt.Sprintf("line %d: call.seq must be an int >= 1", i))
		}
		if _, ok := m["fn"].(string); !ok {
			*out = append(*out, fmt.Sprintf("line %d: call.fn must be a string", i))
		}
		if kw, ok := m["kwargs"].(map[string]any); !ok {
			*out = append(*out, fmt.Sprintf("line %d: call.kwargs must be an object", i))
		} else {
			checkValue(kw, fmt.Sprintf("line %d.kwargs", i), out, 0)
		}
		if r, present := m["result"]; present {
			checkValue(r, fmt.Sprintf("line %d.result", i), out, 0)
		}
		if errv, present := m["error"]; !present {
			*out = append(*out, fmt.Sprintf("line %d: call must carry 'error' (null when it did not raise)", i))
		} else if errv != nil {
			if _, ok := errv.(string); !ok {
				*out = append(*out, fmt.Sprintf("line %d: call.error must be a string or null", i))
			}
		}
		if !isTZAware(m["ts"]) {
			*out = append(*out, fmt.Sprintf("line %d: call.ts must be timezone-aware ISO-8601", i))
		}
		if !isNumber(m["ms"]) {
			*out = append(*out, fmt.Sprintf("line %d: call.ms must be a number", i))
		}
		if evs, ok := m["events"].([]any); !ok {
			*out = append(*out, fmt.Sprintf("line %d: call.events must be an array", i))
		} else {
			for j, e := range evs {
				checkEvent(e, fmt.Sprintf("line %d.events[%d]", i, j), out)
			}
			checkSemNesting(evs, fmt.Sprintf("line %d", i), out)
		}
		return
	}

	// unknown ev (e.g. the reserved "inflight"): a reader must tolerate it.
}

// ValidateTape validates a whole tape. Returns violations; empty means conformant.
func ValidateTape(text string) []string {
	out := []string{}
	var lines []string
	for _, ln := range strings.Split(text, "\n") {
		if strings.TrimSpace(ln) != "" {
			lines = append(lines, ln)
		}
	}
	if len(lines) == 0 {
		return []string{"empty tape: the session header is mandatory"}
	}

	var seqs []int
	for i, ln := range lines {
		dec := json.NewDecoder(strings.NewReader(ln))
		dec.UseNumber()
		var obj any
		if err := dec.Decode(&obj); err != nil {
			// Only the final line may be torn (the process died mid-write).
			if i == len(lines)-1 {
				continue
			}
			out = append(out, fmt.Sprintf("line %d: not JSON (%v)", i, err))
			continue
		}
		validateLine(obj, i, &out, i == 0)
		if m, ok := obj.(map[string]any); ok {
			if ev, _ := m["ev"].(string); ev == "call" {
				if seq, sok := asInt(m["seq"]); sok {
					seqs = append(seqs, seq)
				}
			}
		}
	}

	sorted := append([]int(nil), seqs...)
	sort.Ints(sorted)
	monotonic := true
	for i := range seqs {
		if seqs[i] != sorted[i] {
			monotonic = false
			break
		}
	}
	expected := true
	for i := range seqs {
		if seqs[i] != i+1 {
			expected = false
			break
		}
	}
	if !monotonic || (len(seqs) > 0 && !expected) {
		out = append(out, fmt.Sprintf("call.seq must be 1-based and monotonic; got %v", seqs))
	}
	return out
}
