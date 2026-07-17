# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record what the outside world told your code — every database answer, HTTP response,
clock read and random draw — as one small JSONL file per request. Replay that file
against your real code later: same inputs, same execution, bit for bit, with every
internal variable observable. When a replay diverges, the report names the first
difference instead of leaving you to guess.

## Quickstart

```bash
pip install git+https://github.com/xag/flight-recorder
```

Say your app is a module of functions, and one of them isn't deterministic:

```python
# app.py
import random

def deal(players: int) -> dict:
    order = random.sample(range(players), k=players)
    return {"first": order[0], "order": order}
```

**1. Declare where the outside world enters.** Here it's just `random`; real apps list
their storage and HTTP functions, clock and env constants too:

```python
# record_once.py
import app
import flight_recorder as fr

boundary = fr.Boundary(random_modules=[app])
```

**2. Record.** Install the recorder over your module and use the app normally — each
call becomes one line in `flight/<session>.jsonl`:

```python
fr.install(boundary, app, directory="flight")
print(app.deal(players=4))
```

**3. Replay.** Point an adapter at your functions and re-run the recorded call — the
recorded answers are fed back in, so the same code path runs again, exactly:

```python
# replay_it.py
from pathlib import Path

import app
import flight_recorder as fr

boundary = fr.Boundary(random_modules=[app])

class Adapter(fr.ReplayAdapter):
    boundary = boundary
    def resolve(self, fn_name, feed):
        return getattr(app, fn_name)

tape = next(Path("flight").glob("*.jsonl"))
report = fr.replay_call(tape, 0, Adapter())
print(fr.format_report(0, report))
```

```
Replayed deal (call 0): MATCH — replay reproduced the recording bit-for-bit
  boundary events: 1/1 consumed
```

**4. Write one invariant.** A recording asserts "same as before"; an invariant asserts
"right" — a property that must hold on every execution, checked during replay:

```python
@fr.invariant("no seat is dealt twice")
def no_repeats(t):
    order = t.result["order"]
    assert len(order) == len(set(order))

verdict = fr.check_invariants(tape, 0, Adapter(), [no_repeats])
print(fr.format_invariant_report(verdict))   # deal: 1 invariant(s) held
```

That's the loop: a bug report becomes a file you replay, a fix becomes a replay that
matches, a regression suite becomes a directory of recordings (there's a
[pytest plugin](flight_recorder/pytest_plugin.py) that turns each recorded call into a
test). Debugging becomes looking the variable up in a re-run you control, not
reconstructing what must have happened.

## → [Documentation](https://xag.github.io/flight-recorder/)

[Slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html)

## The tape is a standard

The recording format is a frozen, documented wire contract:
[`spec/tape-v1.md`](spec/tape-v1.md). **Implementations are welcome** — only record and
replay must be native to a runtime; everything that *analyzes* a tape works on any tape.
Conformance is not the prose: it is [`spec/fixtures/`](spec/fixtures/) plus the checker
in [`spec/validate.py`](spec/validate.py) (mirrored in JS). Every implementation must
validate every fixture, and every fixture must have been produced by an implementation.
This repo ships two implementations of the spec — Python and Node — reading and writing
the same tapes.

## Tapes that carry meaning

A recording answers **"same?"**. An invariant answers **"right?"**. Neither answers
**"what was this?"** — the question anyone actually opens a tape with. So an app can say,
in its own words, what a stretch of execution *meant*, recorded in-stream around the raw
events it produced:

```python
with fr.span("assign_turn", chore=chore_id):   # every boundary event inside is inside the span
    holder = db.collection("members").document(who).get()
    fr.note("skipped", reason="absent")        # a moment worth marking, no span
```

The library gains no semantics from this — the name is free text, nothing validates it,
nothing interprets it. The claim is recorded next to its evidence, which is exactly what
makes it checkable by someone else: a span claiming to have charged a card, with no call
to the thing that charges cards beneath it, is a claim a reader can refute. A tape
becomes something you **read** rather than search:

```
>>> print(fr.Recording.load(tape).call(0).render_spans())
enrol  ok  (1 now)
  enrol  ok
    load_corpus  ok  (1 db)
    - corpus_read  rows=3
    register  ERROR  (2 fx)
    - registration_failed  why="kaput"
```

Replay never feeds a recorded claim back: the replayed code speaks afresh, and the two
accounts are compared. Changed claims are a third signal, independent of a boundary
divergence (the recording is stale) and an invariant violation (the code is wrong) — off
by default (`sem_strict=True` to enforce), so instrumenting an app cannot turn an
existing pinned suite red. See the sem section of [the spec](spec/tape-v1.md).

## Why

flight-recorder pushes the heavy lifting from human to AI, and from AI to code. As AI
takes on most of the development, scenario testing and debugging become the bottleneck,
and the work left to the human is the tedious kind. Recording at the nondeterminism
boundary gives the agent the missing instruments: it re-runs the exact request against
the real code and watches any variable as the bug happens — root cause by lookup, not by
guess; fixes proven by replay; regressions caught by a directory of files. What is left
to the human is the decisions.

## Install

| Language | Package | Source |
|---|---|---|
| Python | `flight-recorder` — PyPI pending; until then `pip install git+https://github.com/xag/flight-recorder` | [`flight_recorder/`](flight_recorder/) |
| Node | [`@xag/flight-recorder`](https://www.npmjs.com/package/@xag/flight-recorder) (npm) | [`js/`](js/) |

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Relicensed from MIT
deliberately, before any outside contribution existed: the tape spec is meant to be
implemented by others, and Apache-2.0's explicit patent grant is what makes "implement
this freely" a promise rather than a mood.

© 2026 Xavier Grehant
