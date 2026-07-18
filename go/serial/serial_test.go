package serial

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// marshal→unmarshal a jsonable tree, the way it travels to and from a tape line.
func roundTripJSON(t *testing.T, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var back any
	if err := json.Unmarshal(b, &back); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return back
}

func TestScalarsAndMarkers(t *testing.T) {
	tm := time.Date(2026, 7, 11, 15, 48, 9, 996081000, time.UTC)
	j := ToJsonable(map[string]any{
		"s": "hi", "n": 3, "f": 1.5, "b": true, "z": nil, "when": tm,
	}).(map[string]any)

	if j["s"] != "hi" || j["b"] != true || j["z"] != nil {
		t.Errorf("scalars mangled: %#v", j)
	}
	dt, ok := j["when"].(map[string]any)
	if !ok || dt["__dt__"] != "2026-07-11T15:48:09.996081Z" {
		t.Errorf("datetime not marked: %#v", j["when"])
	}
}

func TestOpaqueScrubsAddress(t *testing.T) {
	ch := make(chan int)
	m := ToJsonable(ch).(map[string]any)
	s, ok := m["__opaque__"].(string)
	if !ok {
		t.Fatalf("channel should be opaque, got %#v", m)
	}
	if strings.Contains(s, "0x") {
		t.Errorf("opaque still carries an address (nondeterministic): %q", s)
	}
}

func TestBytesAreOpaqueHex(t *testing.T) {
	m := ToJsonable([]byte{0xde, 0xad, 0xbe, 0xef}).(map[string]any)
	if s, _ := m["__opaque__"].(string); !strings.Contains(s, "deadbeef") {
		t.Errorf("bytes should render as hex, got %#v", m)
	}
}

func TestDepthDegrades(t *testing.T) {
	// 20 levels of nesting; past depth 16 the value must degrade to __opaque__ rather than
	// recurse forever or panic.
	var v any = "deep"
	for i := 0; i < 20; i++ {
		v = map[string]any{"x": v}
	}
	j := ToJsonable(v)
	// walk down and confirm we hit an __opaque__ before reaching a bare "deep"
	cur := j
	sawOpaque := false
	for i := 0; i < 25; i++ {
		m, ok := cur.(map[string]any)
		if !ok {
			break
		}
		if _, isOpaque := m["__opaque__"]; isOpaque {
			sawOpaque = true
			break
		}
		cur = m["x"]
	}
	if !sawOpaque {
		t.Errorf("depth cap did not degrade to __opaque__")
	}
}

func TestReviveDatetime(t *testing.T) {
	tm := time.Date(2026, 7, 11, 15, 48, 9, 0, time.UTC)
	wire := roundTripJSON(t, ToJsonable(map[string]any{"when": tm}))
	revived := FromJsonable(wire).(map[string]any)
	got, ok := revived["when"].(time.Time)
	if !ok {
		t.Fatalf("datetime not revived to time.Time: %#v", revived["when"])
	}
	if !got.Equal(tm) {
		t.Errorf("datetime round-trip: got %v want %v", got, tm)
	}
}

func TestReviveUndefIsNil(t *testing.T) {
	// A tape from JS may carry __undef__; here it revives to nil, same as null.
	revived := FromJsonable(map[string]any{"__undef__": true})
	if revived != nil {
		t.Errorf("__undef__ should revive to nil, got %#v", revived)
	}
}

func TestRedactFieldRules(t *testing.T) {
	tree := map[string]any{
		"user":  "alice",
		"token": "secret-123",
		"nested": map[string]any{
			"password": "hunter2",
			"keep":     "ok",
		},
	}
	rules := Rules{
		"token":    nil,                                        // bare rule → REDACTED
		"password": func(x any) any { return "***" },           // transform
	}
	out := Redact(tree, rules, nil).(map[string]any)
	if out["token"] != Redacted {
		t.Errorf("token not redacted: %#v", out["token"])
	}
	if out["user"] != "alice" {
		t.Errorf("unrelated field touched: %#v", out["user"])
	}
	nested := out["nested"].(map[string]any)
	if nested["password"] != "***" || nested["keep"] != "ok" {
		t.Errorf("nested redaction wrong: %#v", nested)
	}
}

func TestScrubSweepsEverywhere(t *testing.T) {
	secret := "sk-abc123"
	scrub := func(s string) string { return strings.ReplaceAll(s, secret, "[SK]") }
	tree := map[string]any{
		"note":       "the key is sk-abc123 do not share",     // prose
		"positional": []any{"sk-abc123"},                       // no field name
		"user:sk-abc123": "v",                                  // value, key untouched (keys aren't leaves)
	}
	out := Redact(tree, nil, scrub).(map[string]any)
	if strings.Contains(out["note"].(string), secret) {
		t.Errorf("scrub missed prose: %#v", out["note"])
	}
	if out["positional"].([]any)[0].(string) != "[SK]" {
		t.Errorf("scrub missed positional: %#v", out["positional"])
	}

	// Idempotence: scrubbing an already-scrubbed tree changes nothing.
	twice := Redact(out, nil, scrub)
	b1, _ := json.Marshal(out)
	b2, _ := json.Marshal(twice)
	if string(b1) != string(b2) {
		t.Errorf("scrub not idempotent:\n once: %s\n twice: %s", b1, b2)
	}
}

func TestRedactRulePanicMasks(t *testing.T) {
	rules := Rules{"boom": func(x any) any { panic("nope") }}
	out := Redact(map[string]any{"boom": "value"}, rules, nil).(map[string]any)
	if out["boom"] != Redacted {
		t.Errorf("a panicking rule must mask, not leak or crash: %#v", out["boom"])
	}
}

func TestShortTruncates(t *testing.T) {
	long := strings.Repeat("x", 200)
	s := Short(long, 60)
	if len([]rune(s)) > 60 {
		t.Errorf("Short did not cap length: %d runes", len([]rune(s)))
	}
	if !strings.HasSuffix(s, "…") {
		t.Errorf("truncation should end in ellipsis: %q", s)
	}
}

// A struct's exported fields are the recorded surface, honoring json tags; unexported and
// `json:"-"` fields never reach the tape.
func TestStructEncoding(t *testing.T) {
	type row struct {
		Name   string `json:"name"`
		Secret string `json:"-"`
		hidden int
		Count  int
	}
	j := ToJsonable(row{Name: "a", Secret: "s", hidden: 9, Count: 2}).(map[string]any)
	if j["name"] != int64(0) && j["name"] != "a" {
		t.Errorf("json-tag name not honored: %#v", j)
	}
	if _, present := j["Secret"]; present {
		t.Errorf("json:\"-\" field leaked: %#v", j)
	}
	if _, present := j["hidden"]; present {
		t.Errorf("unexported field leaked: %#v", j)
	}
	if j["Count"] != int64(2) {
		t.Errorf("untagged exported field wrong: %#v", j["Count"])
	}
}
