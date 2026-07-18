# The tape — format v1 (FROZEN)

This is the wire contract of a flight-recorder recording. It is **frozen**: six recorders
emit it — Python, Node, .NET, Go, Java and PHP — and one analysis engine reads them all.

The point of freezing the format is that only *record* and
*replay* must be native to a runtime — replay has to re-run the real code, so JavaScript
must be replayed by JavaScript. But **invariants and mutation consume the tape, and a tape
is only data.** Freeze the data and the analysis engine is written once, for every runtime.

Conformance is not this document; conformance is `spec/fixtures/*.jsonl` plus the checker
in `spec/validate.py` (mirrored in JS, .NET, Go, Java and PHP). Every implementation must
validate every fixture, and every fixture must have been produced by an implementation.
Prose drifts; fixtures do not.

## The file

A recording is **JSONL**: one JSON object per line, UTF-8, `\n`-terminated, appended in
order. The first line is the `session` header. Every subsequent line is a `call`.

Lines are written append-only and each is complete when written, so a truncated final line
(the process died mid-write) is the only corruption possible, and a reader must tolerate it
by discarding that line.

```
{"ev":"session","version":1,...}
{"ev":"call","seq":1,...}
{"ev":"call","seq":2,...}
```

Every object carries `ev`, its discriminator. A reader MUST ignore an `ev` it does not
know, and MUST ignore unknown keys within an object it does know — that is the whole
forward-compatibility story, and it is why new event kinds do not need a version bump.

## `ev: "session"` — the header (exactly one, first line)

| key | type | meaning |
|---|---|---|
| `ev` | `"session"` | |
| `version` | `1` | the format version. A reader MUST refuse a version it does not implement. |
| `started` | string | ISO-8601, **timezone-aware**. |
| `constants` | object | `"module.NAME" → value`, the boundary's declared constants, jsonable. |
| `python` \| `node` \| `dotnet` \| `go` \| `java` \| `php` | string | the runtime version. Exactly one, naming the runtime that produced the tape. Adding a runtime name is additive (a new key readers ignore); the conformance checkers carry the recognized set. |

Additional keys may be added by the boundary (`header_extras`) and MUST be preserved by a
reader that rewrites the tape.

## `ev: "call"` — one tool call at the boundary

One line per call. **This line IS the execution**: the code is deterministic given the
answers the world gave it, so the answers plus the inputs reconstitute the run.

| key | type | meaning |
|---|---|---|
| `ev` | `"call"` | |
| `seq` | int ≥ 1 | 1-based, monotonic within the session. |
| `fn` | string | the tool's name. |
| `kwargs` | object | the call's inputs, jsonable and redacted. |
| `events` | array | every answer the world gave, **in the order it was asked** (see below). |
| `result` | any | the call's return value, jsonable and redacted. |
| `error` | string \| null | the error's rendering if the call raised, else `null`. |
| `ts` | string | ISO-8601, timezone-aware. |
| `ms` | number | wall-clock duration, 2 decimal places. |
| `probe` | bool | *optional.* Present and true when the tape was mutated (see `mutate`). |

`events` order is load-bearing. Replay pops the events in sequence and asserts the code
asks the same questions in the same order; a different question at position *n* is precisely
where behaviour changed.

## The events inside a call

Each event is `{"k": <kind>, ...}`. Six kinds are defined. The first five record what the
*world* answered — they are evidence, and they are what replay feeds back. The last, `sem`,
records what the *app claimed it was doing*, and is never fed back to anything.

### `k: "fx"` — an effect (a module function: HTTP, storage, anything)

| key | type | meaning |
|---|---|---|
| `k` | `"fx"` | |
| `fn` | string | the effect's name. |
| `args` | array | positional args, jsonable. For a method effect, the receiver (`self`/`this`) is **excluded** — it is identity, not input. |
| `kwargs` | object | keyword args, jsonable. JS has no kwargs: emit `{}`. |
| `res` | any | the value returned. Present iff the effect returned. |
| `err` | object | `{type, repr, args}`. Present iff the effect raised. |

Exactly one of `res` / `err` is present.

### `k: "db"` — a chained client (Firestore-style `db.collection(...).where(...).get()`)

| key | type | meaning |
|---|---|---|
| `k` | `"db"` | |
| `op` | string | the terminal operation's name. |
| `sig` | string | the chain that led to it, rendered — e.g. `collection('users').where('age', '>', 3)`. |
| `res` | snapshot \| snapshot[] | present for a terminal **read**. |
| `args` | array | present for a terminal **write**. |

A snapshot is `{"id": string|null, "exists": bool, "data": any|null}` — identity, existence,
data: the only surface a well-behaved consumer reads.

`res` and `args` are mutually exclusive: a read has answers, a write has questions.

### `k: "now"` — the wall clock

| key | type | meaning |
|---|---|---|
| `k` | `"now"` | |
| `v` | string | ISO-8601. **May be naive.** Round-tripped exactly as the app received it. |

Unlike `session.started` and `call.ts` — which are recorder metadata and are always
timezone-aware — `now.v` is a value the *application* was handed, and replay must hand back
something indistinguishable from it. Python's `datetime.now()` is naive, and comparing a
naive datetime with an aware one raises `TypeError`; a replay that "helpfully" normalised
to aware would therefore change behaviour, which is the one thing replay may never do. So
the awareness of this value is part of the value. Preserve the string verbatim.

### `k: "rand"` — a random draw

`m` names the method. Two are defined, because the two runtimes draw randomness in
genuinely different shapes and flattening one onto the other would lose the property that
makes each replayable.

**`m: "sample"`** — drawing members from a population (Python's `random.sample`).

| key | type | meaning |
|---|---|---|
| `m` | `"sample"` | |
| `n` | int | the population size drawn from. |
| `kk` | int | how many were drawn. (`k` is taken by the discriminator.) |
| `idx` | int[] | the **positions** drawn — `kk` of them, each in `[0, n)`. |

Recording positions, not members, is what lets replay pick the same members from a
*mutated* population without re-rolling the RNG.

**`m: "bytes"`** — drawing raw entropy (Node's `crypto.randomBytes`, `randomUUID`).

| key | type | meaning |
|---|---|---|
| `m` | `"bytes"` | |
| `n` | int | how many bytes were drawn. |
| `hex` | string | the bytes, lowercase hex — exactly `2n` characters. |

There is no population to index into here: the draw *is* the value, and replay hands the
same bytes back. Recording positions would be meaningless, and recording a seed would not
survive a mutated tape.

**`m: "float"`** — a uniform draw in `[0, 1)` (JavaScript's `Math.random`).

| key | type | meaning |
|---|---|---|
| `m` | `"float"` | |
| `v` | number | the value drawn, `0 <= v < 1`. |

**`m: "int"`** — a uniform integer draw (Node's `crypto.randomInt`).

| key | type | meaning |
|---|---|---|
| `m` | `"int"` | |
| `v` | int | the value drawn. |

The methods are not interchangeable, and none is a special case of another: each records the
shape of the draw that actually happened, because that is what makes each one replayable
against an *edited* tape. A reader that understands only some of them MUST ignore the rest
rather than guess.

### `k: "perf"` — the monotonic clock

| key | type | meaning |
|---|---|---|
| `k` | `"perf"` | |
| `v` | number | milliseconds, as `performance.now()` returned them. |

A separate kind from `now` because it is a separate clock: monotonic, arbitrary origin, not
a wall time. Feeding a wall time back into it would be a category error.

### `k: "sem"` — a semantic event (the app's own testimony)

Every other event kind records what the *world* answered. This one records what the *app said
it was doing* — a domain-level act, in the app's own free-text vocabulary, written in-stream
next to the raw events it encloses.

It is testimony, not evidence, and the distinction is the whole point of putting it here. The
raw events are ground truth: they are what crossed the boundary. A `sem` event is a **claim**,
made by the code, about what that stretch of execution *meant*. The recorder writes both down
and judges neither. Whether a claim is licensed by the evidence beneath it is a question for a
reader, and it is a question with teeth only because both are on the same tape, in order.

| key | type | meaning |
|---|---|---|
| `k` | `"sem"` | |
| `name` | string | the act, in the app's vocabulary. Opaque: no implementation interprets it. |
| `phase` | `"begin"` \| `"end"` \| `"point"` | a span opens, a span closes, or a moment is marked. |
| `sid` | int | unique within the call. An `end` repeats its `begin`'s `sid`; a `point` has its own. |
| `data` | object | *optional.* Payload, value-encoded and redacted exactly like a boundary value. |
| `outcome` | `"ok"` \| `"error"` | *optional, `end` only.* `error` iff the span's body raised. |

**Order is the meaning.** Enclosure is derived from the sequence — a span encloses every event
between its `begin` and its `end` — so there are no parent pointers, and none are needed. A
`begin`/`end` pair is **well-nested** with respect to every other pair in the same call: spans
nest, they never straddle. A recorder that cannot guarantee that must not emit `sem` at all.

**Spans are call-scoped.** A span never crosses a call boundary. Meaning that spans a session
is a reader's composition, not a recorder's, and a tape that claimed otherwise would be making
a structural promise the format cannot keep.

An `end` with no `begin`, a `begin` with no `end`, or a crossed pair is a malformed tape. A
span left open by a process that died mid-call is not malformed — it is a call that never
finished, and it lives in the `inflight` sidecar, where an unclosed span is exactly the
information wanted.

`sem` events are **not boundary answers**. Replay never feeds one back: the replayed code
re-runs its own `note()`/`span()` calls and makes its claims afresh, and a reader compares the
two sequences. Changed testimony is a third signal, independent of a boundary divergence
(the recording is stale) and of an invariant violation (the code is wrong): it means the code's
account of what it was doing has changed, which may be a refactor or may be a bug, and the tape
does not presume to say which.

## Value encoding

Boundary values are JSON with revivable single-key markers. A value is one of: `null`, a
string, a number, a bool, an array, an object, or exactly one marker.

| marker | payload | revives to |
|---|---|---|
| `{"__dt__": s}` | ISO-8601 | a datetime |
| `{"__date__": s}` | ISO-8601 date | a date |
| `{"__undef__": true}` | `true` | JS: `undefined`. Python: `None`. |
| `{"__opaque__": s}` | a repr, ≤200 chars | the string (it cannot be revived faithfully — by design) |

Nesting is capped at depth **16**; deeper values degrade to `__opaque__`.

#### `__undef__`, and why a runtime with one nothing still needs it

JavaScript has two nothings, `null` and `undefined`, and they are not interchangeable: a
key that is present-and-undefined is not the same object as a key that is absent, and a
function returning `undefined` is not one returning `null`. Encoding both as `null` loses
information that a replay may depend on.

Python has one nothing. So `__undef__` revives to `None` there — the same thing `null`
revives to — and a Python recorder never emits it. The marker costs Python nothing and buys
JavaScript exact fidelity, which is the whole reason it exists rather than being waved away
as "close enough".

An `__opaque__` value is a one-way door: it exists so that an exotic object cannot break a
recording, not so it can be restored. A well-factored app reads plain JSON-ish data plus
datetimes back from its stores, so this should be rare — and its presence in a tape is a
smell worth reading as one.

### Redaction

Masking has two keyings, and an implementation MUST provide both. They are complements: one
keys on where a value sits, the other on what it looks like, and a secret that has no field
name is invisible to the first.

**By field name.** A rule keyed by field name, applied to the jsonable tree before it is
written: a bare rule replaces the value with `"[REDACTED]"`, a transform rule replaces it
with the transform's output.

**By value (`scrub`).** A sweep applied to every leaf **string**, wherever it sits — a
positional argument, a value baked into a key, a string mid-sentence in a body. Object keys
are NOT swept, so that tapes stay comparable across implementations. A field rule's own
output meets the sweep too.

Either kind that raises degrades to `"[REDACTED]"` — the failure direction is *masked*,
never *leaked*, and never *broke the recorded call*.

Both MUST be **idempotent**: replay re-derives the question it is about to ask, scrubs it
the same way, and compares against the tape — so a value that is already a mask must scrub
to itself. This is the contract that makes a redacted recording replayable at all, and it is
why a mask must not itself match the pattern that produced it.

### Refusing to record (`forbid`)

Not a format rule — it adds no key and no event — but **required recorder behaviour**, and
recorded here because a tape's readers are entitled to what it guarantees.

Masking is declarative and opt-in, so it can only mask what it was told about: the field
someone forgot, the one added to the model next month, the one whose rule stopped matching
after a rename. `forbid` states the property the masking rules cannot: *this tape carries no
credential*. Patterns are matched against the fully-masked line about to be written; a hit
means the recorder writes **nothing** — no call line, and no session file if the hit is in
the header — and raises instead.

Patterns match **shapes, not values**: a credential you can enumerate you can already
redact. The raised error names the pattern and not the match, since an error carrying the
secret would defeat its own purpose. A recorder is otherwise built never to break the app it
instruments; this is the one failure it must not swallow, because a warning in a production
log is not how anyone should discover a credential was about to be written down.

## Reserved

`ev: "inflight"` (the crash-capture sidecar) and the trace encoding (`__snap__`, `__seq__`,
`__str__`, `__esc__`) are **reserved in v1** and out of scope for a recorder. A tape reader
MUST tolerate them. The trace markers belong to variable-level tracing, which Node does not
have — but they are reserved here so that an implementation adding it cannot choose a
conflicting encoding.

## Changing this

Add a key, add an event kind, add a marker: no version bump, because readers ignore what
they do not know. Change the meaning of an existing key, remove one, or alter the ordering
guarantee: bump `version`, and no implementation may read a version it does not implement.
