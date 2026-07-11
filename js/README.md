# @xag/flight-recorder

Record an app's tool calls at their **nondeterminism boundary**; replay them
deterministically against the real code.

The Node port of [flight-recorder](https://github.com/xag/flight-recorder). It writes the
**same tape format** as the Python implementation — see
[`spec/tape-v1.md`](https://github.com/xag/flight-recorder/blob/main/spec/tape-v1.md) — so
one analysis engine serves both runtimes.

## The idea

A program is deterministic except where the world leaks in: what the database answered,
what the API returned, what time it was, what the dice rolled. Record just those, per call
— one cheap JSONL line — and that line **is** the execution, compressed. Feed the answers
back and the real code re-runs the original execution exactly: no network, no database, no
waiting for the bug to happen again.

The cardinal rule: **instrument, never duplicate.** Nothing here evaluates a query,
reimplements a client, or knows what any value means. Recording is a transparent proxy;
replay feeds the recorded answers back and verifies the *questions* still match.

## Install

```bash
npm install @xag/flight-recorder
```

## Declare the boundary

The one app-specific artifact. Name the doors the world comes through.

```js
import * as fr from '@xag/flight-recorder';
import { Redis } from '@upstash/redis';

export const BOUNDARY = fr.boundaryOf({
  // Field-name rules. Transforms must be idempotent: replay re-derives the question,
  // scrubs it the same way, and compares — so a value that is already a mask must
  // scrub to itself.
  redact: { token: null, password: null, encSender: null, deleteToken: null },
  constants: { 'config.LIMIT': LIMIT },
  errorRevivers: { NotFound: ([msg]) => new NotFound(msg) },
});

// Wrap what the app HOLDS. A transparent Proxy — never a mock. Everything not named
// passes straight through, untouched and unrecorded.
export const kv = fr.wrap(new Redis({ url, token }), ['get', 'set', 'hgetall', 'zadd']);

// Wrap the tools: this is the call boundary. One line per call.
export const submit = fr.tool('submit_article', submitImpl);

fr.install(BOUNDARY, {
  directory: '.flight',
  enabled: process.env.FLIGHT === '1',
  gate: (fn, args) => fn === 'submit_article',  // optional: record only what matters
});
```

> **Why wrapping, and not module patching?** Because an ES module namespace is immutable —
> there is no way to reach behind an `import` and swap what a caller already bound. The
> Python version patches modules with `setattr`; JavaScript cannot. So the boundary is the
> object the app holds. The clock and the RNG are the exception, and *are* patched globally,
> because there the app holds nothing to wrap.

## Replay

```js
const tape = fr.loadTape('.flight/flight-20260711-1234.jsonl');
const call = fr.pickCall(tape, { fn: 'submit_article' });

const report = await fr.replayCall({ call, fn: submitImpl, boundary: BOUNDARY });

report.ok           // result and error both match the recording
report.divergence   // or: the exact point where behaviour changed
```

Replay does two jobs, and the second matters as much as the first:

1. **Answer** — hand back the recorded answers, in order.
2. **Refuse to answer the wrong question.** If the code asks a different effect, in a
   different order, or with different arguments, that is *caught*. A replay that silently
   answered anyway would look like it worked, which is worse than useless.

Divergence is not a failure of the tool. It **is the finding**: the precise point at which
the code's behaviour changed. Three kinds are caught, and the third is the sneaky one:

- the code asks a **different question**;
- the code asks in a **different order**;
- the code **stops asking** — nothing gives a wrong answer, it just quietly does less work
  than it used to. Unconsumed answers are the only evidence.

## Edit the tape to visit a world that never happened

A recording is data, so hostile states are one edit away. Empty a result, hand back an
absurd number, run the clock backwards — then replay the *real* code against the edited
tape. This finds the bugs no real traffic has triggered yet, without a test database that
can produce impossible states on demand.

```js
const call = structuredClone(fr.pickCall(tape, { fn: 'greet' }));
call.events[0].res = null;   // the store can never actually answer this
call.probe = true;           // a mutated upstream answer changes every downstream question,
                             // so arguments are no longer compared — name and order still gate

const report = await fr.replayCall({ call, fn: greet, boundary: BOUNDARY, probe: true });
```

## Status

**Stage 1**: record, replay, divergence detection, tape mutation.

Not yet ported: **variable-level tracing** (`sys.settrace` gives Python every local on every
executed line for free; Node has no equivalent and needs the V8 Inspector or a source
transform), and the **invariants** and **pytest** integration — which do not need porting,
because they consume the *tape*, and the tape is shared.

## Known limits

- **`undefined` encodes as `null`.** JavaScript has two nothings; the tape has one. A
  marker would fork the format in all but name, and dropping the key changes an object's
  shape. This is the honest lossy choice, and it is written down rather than discovered.
- **`new Date()` and `Math.random()` are not shimmed.** `Date.now()`,
  `crypto.randomBytes()` and `crypto.randomUUID()` are. Code that reaches for the
  un-shimmed ones will re-roll on replay, and the divergence will not tell you why — so
  prefer the shimmed forms at the boundary.
- Only the **synchronous** form of `randomBytes` is recorded; the callback form passes
  straight through.

## License

MIT
