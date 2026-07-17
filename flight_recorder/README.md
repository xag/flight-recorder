# flight-recorder (Python)

Record what the outside world told your code — every database answer, HTTP response, clock read
and random draw — as one small JSONL file per request: a *tape*. Replay that file against your
real code later: same inputs, same execution, bit for bit, with every internal variable
observable. When a replay diverges, the report names the first difference instead of leaving you
to guess.

The Python implementation of [flight-recorder](https://github.com/xag/flight-recorder). It writes
**tape format v1**, the same tapes the Node and .NET implementations write, so one analysis serves
all three.

```bash
pip install flight-recorder
```

## Quickstart

Say one of your functions isn't deterministic:

```python
# app.py
import random

def deal(players: int) -> dict:
    order = random.sample(range(players), k=players)
    return {"first": order[0], "order": order}
```

**Declare where the outside world enters, record, replay.**

```python
import app
import flight_recorder as fr
from pathlib import Path

boundary = fr.Boundary(random_modules=[app])   # the one app-specific artifact

# record
fr.install(boundary, app, directory="flight")
app.deal(players=4)

# replay: the recorded answers are fed back, so the same code path runs again, exactly
class Adapter(fr.ReplayAdapter):
    boundary = boundary
    def resolve(self, fn_name, feed):
        return getattr(app, fn_name)

tape = next(Path("flight").glob("*.jsonl"))
print(fr.format_report(0, fr.replay_call(tape, 0, Adapter())))
# Replayed deal (call 0): MATCH — replay reproduced the recording bit-for-bit
```

That's the loop: a bug report becomes a file you replay, a fix becomes a replay that matches, a
regression suite becomes a directory of recordings (there's a
[pytest plugin](pytest_plugin.py) that turns each recorded call into a test).

## The rest is in the guide

Redaction and the `forbid` tripwire, recording off-box with a sink, editing the tape to visit
worlds that never happened, invariants (**"right?"** to a recording's **"same?"**), variable-level
tracing, and semantic spans — all of it, with runnable examples:

**→ [xag.github.io/flight-recorder](https://xag.github.io/flight-recorder/)**

The tape is a frozen, cross-language standard:
[`spec/tape-v1.md`](https://github.com/xag/flight-recorder/blob/main/spec/tape-v1.md).

## License

Apache-2.0 — see [LICENSE](https://github.com/xag/flight-recorder/blob/main/LICENSE).
