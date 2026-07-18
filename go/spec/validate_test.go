package spec

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Every fixture in spec/fixtures/ must validate clean under this checker — the same fixtures
// the Python and JS checkers must also pass. This is what "the Go checker agrees" means.
func TestFixturesConform(t *testing.T) {
	dir := filepath.Join("..", "..", "spec", "fixtures")
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("reading fixtures dir: %v", err)
	}
	n := 0
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".jsonl" {
			continue
		}
		n++
		b, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatalf("reading %s: %v", e.Name(), err)
		}
		if v := ValidateTape(string(b)); len(v) > 0 {
			t.Errorf("%s: %d violation(s):\n  %s", e.Name(), len(v), strings.Join(v, "\n  "))
		}
	}
	if n == 0 {
		t.Fatal("no .jsonl fixtures found — nothing was actually checked")
	}
	t.Logf("validated %d fixtures clean", n)
}

// A green fixtures run is only meaningful if the checker can also fail. Each broken tape
// must produce at least one violation; otherwise the checker is a rubber stamp.
func TestCheckerBites(t *testing.T) {
	good := `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800+02:00","python":"3.14.6","constants":{}}
{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[],"result":null,"error":null,"ts":"2026-07-11T15:48:09.996587+02:00","ms":0.67}`

	// Sanity: the hand-written good tape is itself clean, so the failures below isolate the defect.
	if v := ValidateTape(good); len(v) > 0 {
		t.Fatalf("baseline tape should be clean, got: %v", v)
	}

	cases := map[string]string{
		"wrong version": `{"ev":"session","version":2,"started":"2026-07-11T15:48:09.994800+02:00","python":"3.14.6","constants":{}}`,
		"naive started": `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800","python":"3.14.6","constants":{}}`,
		"no runtime":     `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800+02:00","constants":{}}`,
		"two runtimes":   `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800+02:00","python":"3.14.6","node":"24","constants":{}}`,
		"first not session": `{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[],"error":null,"ts":"2026-07-11T15:48:09.996587+02:00","ms":0.1}`,
		"seq not monotonic": good + "\n" + `{"ev":"call","seq":3,"fn":"g","kwargs":{},"events":[],"error":null,"ts":"2026-07-11T15:48:09.996587+02:00","ms":0.1}`,
		"fx res and err": `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800+02:00","python":"3.14.6","constants":{}}
{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[{"k":"fx","fn":"e","args":[],"kwargs":{},"res":1,"err":{"type":"X"}}],"error":null,"ts":"2026-07-11T15:48:09.996587+02:00","ms":0.1}`,
		"straddled spans": `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800+02:00","python":"3.14.6","constants":{}}
{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[{"k":"sem","name":"a","phase":"begin","sid":1},{"k":"sem","name":"b","phase":"begin","sid":2},{"k":"sem","name":"a","phase":"end","sid":1},{"k":"sem","name":"b","phase":"end","sid":2}],"error":null,"ts":"2026-07-11T15:48:09.996587+02:00","ms":0.1}`,
		"rand idx out of range": `{"ev":"session","version":1,"started":"2026-07-11T15:48:09.994800+02:00","python":"3.14.6","constants":{}}
{"ev":"call","seq":1,"fn":"f","kwargs":{},"events":[{"k":"rand","m":"sample","n":3,"kk":2,"idx":[2,5]}],"error":null,"ts":"2026-07-11T15:48:09.996587+02:00","ms":0.1}`,
	}
	for name, tape := range cases {
		if v := ValidateTape(tape); len(v) == 0 {
			t.Errorf("%q: expected a violation, checker accepted it", name)
		}
	}
}
