# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record an app's tool calls at their **nondeterminism boundary**; replay them deterministically
against the real code.

**[Documentation](https://xag.github.io/flight-recorder/)** ·
[Slides](https://xag.github.io/flight-recorder/slides.html) ·
[Tape format](spec/tape-v1.md)

```bash
pip install flight-recorder          # Python
npm install @xag/flight-recorder     # Node
```

## What it does

A program's execution is fully determined by its code plus its nondeterministic inputs — what the
store answered, what the API returned, what time it was, what the dice rolled. Record just those,
per call: one cheap JSONL line. **That line *is* the execution, compressed.**

Feed the answers back and the real code re-runs the original execution exactly — no network, no
database, no waiting for the bug to happen again.

**The cardinal rule: instrument, never duplicate.** Nothing here evaluates a query, reimplements a
client, or knows what any value means. Recording is a transparent proxy; replay feeds the recorded
answers back and verifies the *questions* still match. The only structural knowledge anywhere is
*names*.

1. **Name the doors** — the handful of places the world enters. That declaration is the *boundary*,
   and it is the only app-specific artifact. Nothing behind it is ever mocked; real code runs
   everywhere.
2. **Record what came through** — the inputs, every answer the world gave *in the order it was
   asked*, and the result.
3. **Replay is resurrection, not re-enactment** — and if the code asks a *different question* than
   the recording holds, you are told precisely where behaviour changed.
4. **Recordings answer "same?", invariants answer "right?"** — a bug records as faithfully as a fix,
   so only a claim about *every* execution can condemn the first sighting of one.
5. **Edit the tape to visit worlds that never happened** — a recording is data, so hostile states are
   one edit away.

## Two implementations, one tape

| Language | Package | Source |
|---|---|---|
| Python | `flight-recorder` (PyPI) | [`flight_recorder/`](flight_recorder/) |
| Node | [`@xag/flight-recorder`](https://www.npmjs.com/package/@xag/flight-recorder) (npm) | [`js/`](js/) |

Both write the same tape — format v1, frozen in [`spec/tape-v1.md`](spec/tape-v1.md). Only *record*
and *replay* are language-bound: replaying JavaScript means running JavaScript. Everything downstream
consumes the tape, and a tape is only data.

The format's conformance checker is written **twice, independently** — neither importing any
recorder, both run against the same fixtures, each language validating the other's. A disagreement
means the tape has forked, which is the one failure the arrangement exists to prevent.

One difference is worth knowing before you choose: **variable-level tracing exists only in Python.**
`sys.settrace` hands it every local on every executed line; Node has no equivalent. A tape gives you
the *boundary* in both — only Python gives you the *interior*.
[The rest of the differences, and why each is forced.](https://xag.github.io/flight-recorder/#differ)

## License

MIT
