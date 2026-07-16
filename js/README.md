# @xag/flight-recorder

Record an app's tool calls at their **nondeterminism boundary**; replay them
deterministically against the real code.

The Node implementation of [flight-recorder](https://github.com/xag/flight-recorder). It writes
tape format v1 — see
[`spec/tape-v1.md`](https://github.com/xag/flight-recorder/blob/main/spec/tape-v1.md) — the same
format the Python implementation writes, so one analysis serves both.

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

## Recording production: the off-box sink

A serverless host has no filesystem worth the name — a tape written during an invocation
dies with it. So hand the recorder a **sink**: anything with `publish(name, text)`.

```js
fr.install(BOUNDARY, {
  directory: null,          // nothing to write to; the sink IS the tape
  enabled: process.env.FLIGHT === '1',
  gate: (fn) => WRITES.has(fn),   // record what matters, not everything
  sink: {
    async publish(name, text) {
      await store.set(`flight:${name}`, text, { ex: 7 * 24 * 3600 });
    },
  },
});
```

It is handed the **whole session** each time — after the header, and after every completed
call — so a sink that overwrites is enough and a tape is never half-published.

**It is awaited before the call returns.** That inversion is the whole point: the instant a
serverless response goes out, the instance is frozen or destroyed, and a publish left in
flight is not slow, it is *lost*. The cost is that recording adds the sink's latency to the
recorded call — which is what `gate` is for.

A sink that throws is swallowed. Recording must never be the reason a call fails.

Use a client the recorder does **not** wrap, or the sink records itself.

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

## The doors it closes for you

Two kinds of nondeterminism are *global* — the app holds no object you could wrap — so the
library shims them itself. All of them, not the convenient ones:

| door | shimmed |
|---|---|
| wall clock | `Date.now()`, `new Date()` |
| monotonic clock | `performance.now()` |
| randomness | `Math.random()`, `crypto.randomBytes()` (sync **and** callback), `crypto.randomUUID()`, `crypto.randomInt()`, `crypto.randomFillSync()`, `crypto.getRandomValues()` |

A half-shimmed door is worse than an open one, because it *looks* shut: code reaching for
the form you skipped re-rolls on replay, silently, and the resulting divergence points at a
value instead of at the door it came through. So none are skipped.

`new Date(2020, 0, 1)` is **not** recorded — building a date from arguments is arithmetic,
not a question to the world.

Everything else — network, storage, queues, the filesystem — is not guessed at. You declare
it with `wrap()`. That is the boundary, and it is the one app-specific artifact.

## Fidelity

**`undefined` is preserved**, not flattened onto `null`. JavaScript has two nothings and
they are not interchangeable: a key that is present-and-undefined is not a key that is
absent, and a function returning `undefined` is not one returning `null`. The tape keeps
them apart with a `__undef__` marker. Python — which has one nothing — revives it as `None`
and never emits it, so the marker costs that runtime nothing and buys this one exactness.

A call that **raised** records `result: null`, which is not the same as a call that returned
`undefined`. Both runtimes agree on that.

## Tapes that carry meaning

A recording answers **"same?"**. An invariant answers **"right?"**. Neither answers **"what was
this?"** — which is the question anyone actually opens a tape with. So an app can say, in its own
words, what a stretch of execution *meant*, and have the claim recorded in-stream, wrapped around
the raw events it produced:

```js
await fr.span('assign_turn', { chore }, async () => {   // every boundary event inside is inside the span
  const holder = await kv.hgetall(`member:${who}`);
  fr.note('skipped', { reason: 'absent' });             // a moment worth marking, no span
});
```

`span(name, data, fn)` runs `fn` (sync or async), returns its result, and writes a `begin`/`end`
pair around it; the `end` carries `outcome: "error"` when `fn` throws, and the error propagates
untouched. `data` is optional (`span(name, fn)`). Both cost **nothing** when the recorder is off,
where the block is just `fn()`.

The library gains no semantics from this — the name is free text, nothing validates it, nothing
interprets it. A semantic event is the app's **testimony**, recorded next to the **evidence**;
writing both down, in order, and judging neither is what makes the testimony checkable by someone
else. The tape becomes something you **read** rather than search — and because the format is
shared, the skeleton is legible from the Python side too:

```
>>> print(fr.Recording.load(tape).call(0).render_spans())
enrol  ok  (1 now)
  enrol  ok
    load_corpus  ok  (1 fx)
    - corpus_read  found=true
    register  ERROR  (2 fx)
    - registration_failed  why="no such key: alice"
```

Replay never feeds a recorded claim back: the replayed code testifies afresh, and the two accounts
are compared. Changed testimony is a **third signal** — `report.semDivergence`, independent of a
boundary divergence (the recording is stale) and a wrong result (the code is wrong). It says the
code's account of what it was doing has changed, which may be a refactor, so it does not fail a
replay unless you ask it to (`semStrict: true`).

## What is here, and what is not

Record, replay, divergence detection, tape mutation.

**Variable-level tracing works.** Pass `trace` to `replayCall` and you get every local, on every
executed line, of the files you name — including across an `await`:

```js
const report = await fr.replayCall({ call, fn: studyStatus, boundary, trace: ['tools/'] });

report.trace.values('level');   // [{ value: 0, at: 'tools.js:12', fn: 'studyStatus' }]
report.trace.render('deck');    // a readable timeline
```

Node has no `sys.settrace`, so this drives the V8 Inspector from a worker thread — the same place a
debugger gets it. It pauses the isolate on every traced line, which costs milliseconds per line: it
is for **replay**, never a request path. Recording stays cheap; understanding is where you spend.

Invariants and pinned-recording suites are not duplicated here because they do not need to be: they
consume the *tape*, and the tape is shared.

## License

MIT
