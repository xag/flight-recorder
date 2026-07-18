# flight-recorder (Go)

The Go implementation of **flight-recorder** — record what the outside world told your code
(HTTP, storage, clock, randomness) as one JSONL *tape* per call, then replay that tape against the
real code so a past run reproduces exactly, with every internal value observable.

```
go get github.com/xag/flight-recorder/go
```

The tape is a frozen, cross-runtime standard ([`spec/tape-v1.md`](../spec/tape-v1.md)): a Go tape
is read by the Python and .NET tools too, and vice versa. The conceptual guide — declare the
boundary, record, replay, edit the tape, semantic spans — is one tab away, shown in Python, Node
and .NET; the Go API below mirrors it.

**→ [xag.github.io/flight-recorder](https://xag.github.io/flight-recorder/)**

## Record

Go can't monkeypatch a package's functions the way the Python recorder shims `datetime`/`random`,
so instrumentation is explicit and idiomatic: the active call rides on the `context.Context`, and
every boundary read goes through a primitive that does the real thing *and* records what crossed.

```go
import (
	"context"

	fr "github.com/xag/flight-recorder/go"
	"github.com/xag/flight-recorder/go/serial"
)

rec, err := fr.New("flight", fr.Boundary{
	Redact: serial.Rules{"password": nil},        // field-name redaction (nil rule → [REDACTED])
	Forbid: []string{`\bAKIA[0-9A-Z]{16}\b`},      // a tripwire for what redaction can't name
})
if err != nil {
	panic(err)
}
defer rec.Close()

result, err := rec.Call(ctx, "greet", map[string]any{"user": "alice"},
	func(ctx context.Context) (any, error) {
		row, err := fr.Effect(ctx, "store.get", []any{"alice"},
			func() (map[string]any, error) { return store.Get("alice") })
		if err != nil {
			return nil, err
		}
		token, _ := fr.RandBytes(ctx, 16)
		at := fr.Now(ctx)
		_ = fr.Exec(ctx, "set", "store.set(greeted:alice)", []any{at},
			func() error { return store.Set("greeted:alice", at) })
		return map[string]any{"name": row["name"], "token": token}, nil
	})
```

Every read goes through a primitive: `Effect` (a function call at the boundary), `Query`/`QueryOne`
(a document read), `Exec` (a write), `Now`/`Perf` (the wall and monotonic clocks),
`SampleIndices`/`RandBytes`/`RandFloat`/`RandIntn` (randomness). The cardinal rule holds:
**instrument, never duplicate** — a primitive forwards to your real code and records the answer; it
never reimplements it.

## Replay

Feed the recorded answers back to the same code. The primitives serve from the tape instead of
doing the real thing, and playback checks that the code asks the same questions in the same order —
anything else is a divergence naming the first difference.

```go
report, err := fr.Replay("flight/flight-20260718-083100-1234.jsonl", 0,
	func(fn string, kwargs map[string]any) (func(context.Context) (any, error), error) {
		return greet, nil // the same function; its effects come off the tape, not the network
	})
if err != nil {
	panic(err)
}
if !report.OK() {
	// report.Divergence, report.ResultMatch, report.ErrorMatch, report.SemDivergence...
	log.Fatalf("replay diverged: %s", report.Divergence)
}
```

`ReplayReport` carries three independent signals: a boundary **divergence** (the recording is
stale), a result/error mismatch (the code produces something else), and a **semantic** divergence
(the code's own account of what it was doing changed — reported, not gating).

## Semantic spans

Say what a stretch of execution *meant*, recorded in-stream next to the raw evidence it encloses. A
span is well-nested by construction; a `Note` marks a point. Both are strict no-ops when nothing is
recording, so they belong in production code paths.

```go
err := fr.Span(ctx, "charge_card", map[string]any{"amount": amount}, func(ctx context.Context) error {
	_, err := fr.Effect(ctx, "stripe.charge", []any{amount},
		func() (string, error) { return stripe.Charge(amount) })
	return err // if the body fails, the span still closes, with outcome "error"
})
fr.Note(ctx, "receipt_sent", map[string]any{"to": email})
```

The library judges neither: a span claiming to have charged a card, with no call beneath it to the
thing that charges cards, is a claim a *reader* can refute — because both are on the same tape, in
order.

---

Source, the frozen tape spec, and license:
[github.com/xag/flight-recorder](https://github.com/xag/flight-recorder).
