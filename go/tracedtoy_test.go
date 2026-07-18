package flightrecorder

import (
	"context"
	"errors"
)

// The traced toy: the bug the whole library was built for, in nine lines.
//
// studyStatus reports how much of a corpus is left to study. Its output — corpus 3, deck 0, done
// true — is entirely self-consistent: "done, with an empty deck" is exactly what the numbers say,
// and no assertion on the RESULT can call it wrong. The wrongness is that `level` excluded every
// word in the corpus, and `level` is a local. It is visible from the inside and nowhere else.
//
// This is the Go counterpart of js/test/traced-app.mjs and it is deliberately, permanently buggy:
// it is the fixture that proves an internal claim catches what an output claim cannot.

func toyWords() []Snapshot {
	return []Snapshot{
		{ID: sp("w1"), Exists: true, Data: map[string]any{"x": 3}},
		{ID: sp("w2"), Exists: true, Data: map[string]any{"x": 1}},
		{ID: sp("w3"), Exists: true, Data: map[string]any{"x": 2}},
	}
}

func studyStatus(ctx context.Context, email string, level int) (map[string]any, error) {
	rows, err := Query(ctx, "get", `collection("words")`, func() ([]Snapshot, error) {
		return toyWords(), nil
	})
	if err != nil {
		return nil, err
	}
	corpus := []int{}
	for _, r := range rows {
		d, _ := r.Data.(map[string]any)
		corpus = append(corpus, int(toFloat(d["x"])))
	}
	deck := []int{}
	for _, x := range corpus {
		if x < level { // THE BUG: a level of 0 admits nothing, and nothing is what it admits
			deck = append(deck, x)
		}
	}
	done := len(deck) == 0
	return map[string]any{"corpus": len(corpus), "deck": len(deck), "done": done}, nil
}

// toyBoom exists to be traced on its way down: `stage` is set, then the function panics, and the
// trace must carry `stage` and the panic both.
func toyBoom(n int) int {
	stage := "about to fail"
	if n > 0 {
		panic("gave up: " + stage)
	}
	return n
}

// toyLeak is the fixture for the tripwire on the trace: a credential that lives ONLY in a local.
// It is never returned, never recorded on a tape, and never printed — so the only artifact that
// can ever carry it is the trace, which is the whole point. Nothing masks it, because nothing
// masks a local: a trace sees values before any redaction is anywhere near them.
func toyLeak() int {
	token := "AKIAABCDEFGHIJKLMNOP"
	return len(token)
}

// toyStudyResolver re-executes studyStatus on replay against a store that would scream if it were
// touched — the recorded answers are the only world the replayed code gets.
func toyStudyResolver(fn string, kwargs map[string]any) (func(context.Context) (any, error), error) {
	if fn != "study_status" {
		return nil, errors.New("unknown fn: " + fn)
	}
	email, _ := kwargs["email"].(string)
	level := int(toFloat(kwargs["level"]))
	return func(ctx context.Context) (any, error) {
		return studyStatus(ctx, email, level)
	}, nil
}
