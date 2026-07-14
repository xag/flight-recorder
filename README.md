# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

flight-recorder pushes the heavy lifting from human to AI, and from AI to code.

As AI takes on most of the development, scenario testing and debugging become the bottleneck, and
the work left to the human is the tedious kind. Not anymore: flight-recorder gives the AI the
missing instruments. It records the answers your code got from the outside world while handling a
request, so the agent can re-run that exact request against the real code and watch any variable
as the bug happens.

With those, the AI takes back the loop: designing the scenarios and running them, finding the root
cause by looking it up rather than paying for a guess, catching the regressions, proving the fix —
continuous improvement, closed. What is left to the human is the decisions, not the moving of
things around.

## → [Documentation](https://xag.github.io/flight-recorder/)

[Slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html)

## Tapes that carry meaning

A recording answers **“same?”**. An invariant answers **“right?”**. Neither answers **“what was
this?”** — which is the question anyone actually opens a tape with.

So an app can say, in its own words, what a stretch of execution *meant*, and have the claim recorded
in-stream, wrapped around the raw events it produced:

```python
with fr.span("assign_turn", chore=chore_id):   # every boundary event inside is inside the span
    holder = db.collection("members").document(who).get()
    fr.note("skipped", reason="absent")        # a moment worth marking, no span
```

The library gains no semantics from this — the name is free text, nothing validates it, nothing
interprets it. A semantic event is the app's **testimony**, recorded next to the **evidence**. Writing
both down, in order, and judging neither is exactly what makes the testimony checkable by someone else:
a span claiming to have charged a card, with no call to the thing that charges cards beneath it, is now
a claim a reader can refute.

So a tape becomes something you **read** rather than search — the skeleton first, the raw JSONL only
inside the span that looks wrong:

```
>>> print(fr.Recording.load(tape).call(0).render_spans())
enrol  ok  (1 now)
  enrol  ok
    load_corpus  ok  (1 db)
    - corpus_read  rows=3
    register  ERROR  (2 fx)
    - registration_failed  why="kaput"
```

It costs nothing when the recorder is off, and a span whose body raises is still closed on the tape,
marked `error` — a span that vanished when the code inside it failed would hide precisely the execution
somebody came to the tape to read.

Replay never feeds a recorded claim back: the replayed code testifies afresh, and the two accounts are
compared. Changed testimony is a **third signal**, independent of a boundary divergence (the recording
is stale) and an invariant violation (the code is wrong) — it says the code's account of what it was
doing has changed, which may be a refactor. It does not fail a replay unless you ask it to
(`sem_strict=True`).

## Install

```bash
pip install flight-recorder          # Python
npm install @xag/flight-recorder     # Node
```

| Language | Package | Source |
|---|---|---|
| Python | `flight-recorder` (PyPI) | [`flight_recorder/`](flight_recorder/) |
| Node | [`@xag/flight-recorder`](https://www.npmjs.com/package/@xag/flight-recorder) (npm) | [`js/`](js/) |

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
