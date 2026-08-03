# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record what the outside world told your code — every database answer, HTTP response, clock read and random draw — as one small JSONL file per request: a *tape*. Replay that file against your real code later: same inputs, same execution, bit for bit, with every internal variable observable. When a replay diverges, the report names the first difference instead of leaving you to guess.

A program's execution is fully determined by its code plus its nondeterministic inputs. Record just those, per call — one cheap line — and that line **is** the execution, compressed. Feed the answers back and the real code re-runs the original execution exactly: no network, no database, no waiting for the bug to happen again.

> **The cardinal rule: instrument, never duplicate.** Nothing here evaluates a query, reimplements a client, or knows what any value means. Recording is a transparent proxy; replay feeds the recorded answers back and verifies the *questions* still match.

## → [Read the guide](https://xag.github.io/flight-recorder/)

The full walkthrough — declare the boundary, record, replay, edit the tape to visit worlds that never happened, invariants, semantic spans — in Python, Node, .NET, Go, Java and PHP, one tab away. [Slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html).

## What a pile of tapes says that one tape cannot

A tape is one execution. A directory of them is a record of how the software is actually used — and `flight_recorder.episodes` reads that: the recurring **act-sequences**, mined from each tape's call envelopes.

```python
from flight_recorder.episodes import story, mine

stories = {p.name: story(p.read_text().splitlines()) for p in tapes}
for e in mine(stories, noise=frozenset({"authenticate"})):
    print(e["support"], "×", " → ".join(e["acts"]))
```

Counting what each call touched answers *what did this do*; it destroys the order, which is usually the part worth having. `open → act → open → read` is a conversation; `{open: 2, act: 1, read: 1}` is not. Deliberately pre-semantic — it reads each envelope's `fn` and nothing else, no spans and no model — because a workflow is visible before anyone has drawn a model of it, and a mined workflow is often what reveals a model missing an act.

Three shaping rules, each a bug before it was a rule. Consecutive repeats **collapse**: twelve of the same act in a row are one act, and left alone they flood the n-grams with eleven identical pairs. Stopwords are **named by the caller**, never inferred: a session handshake preceding nearly every call appears in every window and distinguishes nothing, and a frequency threshold would silently eat the busiest real act in a heavy week. And **one line per ritual**: `a→b`, `b→a` and `a→b→a` are one conversation told three ways, and dealing all three makes a deck nobody reads.

`merge(scripted, live)` keeps rehearsal and performance apart. Recordings driven by an authored scenario mine correctly and are not fake, but their counts mean something else — and a reader who cannot tell them apart reads a script's repetitions as usage.

## The tape is a standard

The recording format is a frozen, documented wire contract: [`spec/tape-v1.md`](spec/tape-v1.md). **Implementations are welcome** — only *record* and *replay* must be native to a runtime; everything that *analyzes* a tape works on any tape. Conformance is not the prose: it is [`spec/fixtures/`](spec/fixtures/) plus the checker in [`spec/validate.py`](spec/validate.py) (mirrored in JS, .NET, Go, Java, and PHP). Every implementation must validate every fixture, and every fixture must have been produced by an implementation. This repo ships six implementations — Python, Node, .NET, Go, Java, and PHP — reading and writing the same tapes.

## Why

flight-recorder pushes the heavy lifting from human to AI, and from AI to code. As AI takes on most of the development, scenario testing and debugging become the bottleneck, and the work left to the human is the tedious kind. Recording at the nondeterminism boundary gives the agent the missing instruments: it re-runs the exact request against the real code and watches any variable as the bug happens — root cause by lookup, not by guess; fixes proven by replay; regressions caught by a directory of files. What is left to the human is the decisions.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Relicensed from MIT deliberately, before any outside contribution existed: the tape spec is meant to be implemented by others, and Apache-2.0's explicit patent grant is what makes "implement this freely" a promise rather than a mood.

© 2026 Xavier Grehant
