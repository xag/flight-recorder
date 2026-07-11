# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record an app's tool calls at their **nondeterminism boundary**; replay them deterministically
against the real code.

## → [Documentation](https://xag.github.io/flight-recorder/)

[Slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html) ·
[The tape format](spec/tape-v1.md)

## Install

```bash
pip install flight-recorder          # Python
npm install @xag/flight-recorder     # Node
```

| Language | Package | Source |
|---|---|---|
| Python | `flight-recorder` (PyPI) | [`flight_recorder/`](flight_recorder/) |
| Node | [`@xag/flight-recorder`](https://www.npmjs.com/package/@xag/flight-recorder) (npm) | [`js/`](js/) |

Both write the same tape — format v1, frozen in [`spec/tape-v1.md`](spec/tape-v1.md).

## What it does

A program's execution is fully determined by its code plus its nondeterministic inputs — what the
store answered, what the API returned, what time it was, what the dice rolled. Record just those,
per call: one cheap JSONL line. **That line *is* the execution, compressed.**

Feed the answers back and the real code re-runs the original execution exactly — no network, no
database, no waiting for the bug to happen again. Trace it while it runs and every local, on every
executed line, is a lookup rather than an inference.

**The cardinal rule: instrument, never duplicate.** Nothing here evaluates a query, reimplements a
client, or knows what any value means. Recording is a transparent proxy; replay feeds the recorded
answers back and verifies the *questions* still match.

## License

MIT
