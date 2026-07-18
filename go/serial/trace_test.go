package serial

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// The point of trace version 2: a traced value is DATA. A repr cannot be added up, cannot be
// looked inside, and carries a memory address that differs on every run — so two traces of the
// same execution would never be equal.
func TestTraceValuesAreDataNotReprs(t *testing.T) {
	got := ToTraceJsonable(map[string]any{"n": 30, "ok": true, "who": "alpha"})
	m, ok := got.(map[string]any)
	if !ok {
		t.Fatalf("a map encoded as %T", got)
	}
	if m["n"] != int64(30) {
		t.Errorf("30 encoded as %#v — an invariant cannot do arithmetic on that", m["n"])
	}
	if m["ok"] != true || m["who"] != "alpha" {
		t.Errorf("encoded as %v", m)
	}

	back, _ := FromTraceJsonable(got).(map[string]any)
	if toNum(back["n"])+12 != 42 {
		t.Errorf("the revived value does not survive arithmetic: %#v", back["n"])
	}
}

// Anything long is cut to a prefix that still reports its TRUE length, so a claim about how many
// is checkable even when a claim about which is not.
func TestTraceCapsLongValuesButKeepsTheirLength(t *testing.T) {
	long := make([]int, TraceMaxItems+50)
	enc := ToTraceJsonable(long)
	spec, ok := enc.(map[string]any)["__seq__"].(map[string]any)
	if !ok {
		t.Fatalf("a long slice encoded as %v", enc)
	}
	if n := len(spec["head"].([]any)); n != TraceMaxItems {
		t.Errorf("the head holds %d items, cap is %d", n, TraceMaxItems)
	}
	tr, ok := FromTraceJsonable(enc).(Truncated)
	if !ok {
		t.Fatalf("a truncated sequence revived as %T", FromTraceJsonable(enc))
	}
	if tr.Len != TraceMaxItems+50 || len(tr.Head) != TraceMaxItems {
		t.Errorf("revived as len=%d head=%d — the true length is the point", tr.Len, len(tr.Head))
	}

	text := strings.Repeat("x", TraceMaxChars+10)
	tt, ok := FromTraceJsonable(ToTraceJsonable(text)).(TruncatedText)
	if !ok {
		t.Fatalf("a long string revived as %T", FromTraceJsonable(ToTraceJsonable(text)))
	}
	if tt.Len != TraceMaxChars+10 || len(tt.Head) != TraceMaxChars {
		t.Errorf("revived as len=%d head=%d", tt.Len, len(tt.Head))
	}

	// A short one is left alone: capping everything would make the common case unreadable.
	if got := ToTraceJsonable("alpha"); got != "alpha" {
		t.Errorf("a short string was encoded as %#v", got)
	}
}

// The encoder runs inside the observed function's own frame. A panic there would propagate into
// the very execution the trace exists to explain — the tracer destroying its own evidence.
func TestTraceEncoderNeverPanics(t *testing.T) {
	type cyclic struct {
		Name string
		Next *cyclic
	}
	loop := &cyclic{Name: "a"}
	loop.Next = loop // a structure that would walk forever

	ch := make(chan int)
	var nilMap map[string]int
	var nilPtr *cyclic

	for _, v := range []any{
		loop, ch, nilMap, nilPtr, nil,
		func() {}, complex(1, 2),
		[]any{loop, ch, map[string]any{"deep": loop}},
	} {
		func() {
			defer func() {
				if r := recover(); r != nil {
					t.Errorf("encoding %T panicked: %v", v, r)
				}
			}()
			enc := ToTraceJsonable(v)
			if _, err := json.Marshal(enc); err != nil {
				t.Errorf("encoding %T produced something unmarshalable: %v", v, err)
			}
		}()
	}
}

// A trace whose values differ between two runs of the same execution is a trace that cannot be
// compared. Go randomises map iteration order per run; the encoder must not inherit that.
func TestTraceMapEncodingIsDeterministic(t *testing.T) {
	m := map[string]int{}
	for _, k := range strings.Split("a b c d e f g h i j k l m n o p", " ") {
		m[k] = len(k)
	}
	first, _ := json.Marshal(ToTraceJsonable(m))
	for i := 0; i < 20; i++ {
		again, _ := json.Marshal(ToTraceJsonable(m))
		if string(again) != string(first) {
			t.Fatalf("two encodings of one map differ:\n  %s\n  %s", first, again)
		}
	}
}

// A user value that happens to be shaped like a marker must revive as itself, not as the thing
// the marker means.
func TestTraceEscapesAValueShapedLikeAMarker(t *testing.T) {
	orig := map[string]any{"__seq__": "not really a sequence"}
	enc := ToTraceJsonable(orig)
	if _, escaped := enc.(map[string]any)["__esc__"]; !escaped {
		t.Fatalf("a marker-shaped map was not escaped: %v", enc)
	}
	back, _ := FromTraceJsonable(enc).(map[string]any)
	if back["__seq__"] != "not really a sequence" {
		t.Errorf("revived as %v, not as itself", back)
	}
}

// Datetimes are data too, and an error is its message — what the code reasoning about it can see.
func TestTraceDatetimesAndErrors(t *testing.T) {
	when := time.Date(2026, 7, 18, 9, 30, 0, 0, time.UTC)
	back, ok := FromTraceJsonable(ToTraceJsonable(when)).(time.Time)
	if !ok || !back.Equal(when) {
		t.Errorf("a datetime round-tripped as %v (%T)", back, back)
	}

	if got := ToTraceJsonable(errString("kaput")); got != "kaput" {
		t.Errorf("an error encoded as %#v; its message is the whole of what the code sees", got)
	}
}

type errString string

func (e errString) Error() string { return string(e) }

// Only exported fields. Reading an unexported one needs unsafe, and a tracer that reached for
// unsafe would be buying a crash inside the observed frame with someone else's money.
func TestTraceStructRecordsExportedFieldsOnly(t *testing.T) {
	type doc struct {
		Name   string
		Count  int
		hidden string
	}
	m, ok := ToTraceJsonable(doc{Name: "alpha", Count: 2, hidden: "x"}).(map[string]any)
	if !ok {
		t.Fatalf("a struct encoded as %T", ToTraceJsonable(doc{}))
	}
	if m["Name"] != "alpha" || m["Count"] != int64(2) {
		t.Errorf("encoded as %v", m)
	}
	if _, present := m["hidden"]; present {
		t.Errorf("an unexported field was read: %v", m)
	}
}

// Render is what a failure message shows a human.
func TestTraceRenderIsReadableAndBounded(t *testing.T) {
	if got := Render([]any{1, 2, 3}, 90); got != "[1,2,3]" {
		t.Errorf("rendered as %q", got)
	}
	if got := Render(strings.Repeat("y", 200), 20); len([]rune(got)) != 20 {
		t.Errorf("a long value rendered %d runes wide, limit was 20: %q", len([]rune(got)), got)
	}
}
