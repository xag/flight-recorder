# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record what the outside world told your code — every database answer, HTTP response, clock
read and random draw — as one small JSONL file per request: a *tape*. Replay that file against
your real code later: same inputs, same execution, bit for bit, with every internal variable
observable. When a replay diverges, the report names the first difference instead of leaving
you to guess.

A program's execution is fully determined by its code plus its nondeterministic inputs. Record
just those, per call — one cheap line — and that line **is** the execution, compressed. Feed the
answers back and the real code re-runs the original execution exactly: no network, no database,
no waiting for the bug to happen again.

> **The cardinal rule: instrument, never duplicate.** Nothing here evaluates a query,
> reimplements a client, or knows what any value means. Recording is a transparent proxy; replay
> feeds the recorded answers back and verifies the *questions* still match.

## → [Read the guide](https://xag.github.io/flight-recorder/)

The full walkthrough — declare the boundary, record, replay, edit the tape to visit worlds that
never happened, invariants, semantic spans — in Python, Node and .NET, one tab away.
[Slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html).

## The tape is a standard

The recording format is a frozen, documented wire contract: [`spec/tape-v1.md`](spec/tape-v1.md).
**Implementations are welcome** — only *record* and *replay* must be native to a runtime;
everything that *analyzes* a tape works on any tape. Conformance is not the prose: it is
[`spec/fixtures/`](spec/fixtures/) plus the checker in [`spec/validate.py`](spec/validate.py)
(mirrored in JS, .NET, and Go). Every implementation must validate every fixture, and every fixture
must have been produced by an implementation. This repo ships four implementations — Python,
Node, .NET, and Go — reading and writing the same tapes.

## Why

flight-recorder pushes the heavy lifting from human to AI, and from AI to code. As AI takes on
most of the development, scenario testing and debugging become the bottleneck, and the work left
to the human is the tedious kind. Recording at the nondeterminism boundary gives the agent the
missing instruments: it re-runs the exact request against the real code and watches any variable
as the bug happens — root cause by lookup, not by guess; fixes proven by replay; regressions
caught by a directory of files. What is left to the human is the decisions.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Relicensed from MIT deliberately,
before any outside contribution existed: the tape spec is meant to be implemented by others, and
Apache-2.0's explicit patent grant is what makes "implement this freely" a promise rather than a
mood.

© 2026 Xavier Grehant
