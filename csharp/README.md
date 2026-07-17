# flight-recorder (.NET)

Record what the outside world told your code — every store answer, HTTP response, clock read
and random draw — as one small JSONL file per request: a *tape*. Replay that file against your
real code later: same inputs, same execution, and when a replay diverges the report names the
first difference instead of leaving you to guess.

The .NET implementation of [flight-recorder](https://github.com/xag/flight-recorder). It writes
**tape format v1** — [`spec/tape-v1.md`](https://github.com/xag/flight-recorder/blob/main/spec/tape-v1.md)
— the same format the Python and Node implementations write, so one analysis serves all three.
Targets `netstandard2.0` (works on .NET Framework 4.6.1+, .NET Core, and .NET 5–8+).

```bash
dotnet add package flight-recorder
```

## Declare the boundary

The one app-specific artifact. .NET can't patch a module's functions, so — like the Node port —
the boundary is **the object the app holds**. Wrap it; everything not named passes straight
through, unrecorded.

```csharp
using FlightRecorder;

var boundary = new Boundary().MaskFields("password");   // field-name redaction
boundary.Constants["Config.Limit"] = 3;                 // captured in the tape header
boundary.ErrorRevivers["NotFound"] = args => new NotFoundException((string)args[0]!);

// Wrap what the app holds — a transparent proxy over an interface, never a mock.
IStore store = Recorder.WrapAs(realStore, "store", nameof(IStore.Get), nameof(IStore.Set));
```

The clock and the RNG are the exception — the app holds no object to wrap there, so it holds the
recorder's handles instead: `Recorder.Clock.Now()` / `.UtcNow()` / `.Mono()` and
`Recorder.Random.NextDouble()` / `.Bytes(n)` / `.NextInt(a, b)` / `.Sample(pop, k)`. Under record
they ask the world and write it down; under replay they answer from the tape.

## Record

Wrap each tool call. That line **is** the execution, because the code is deterministic given the
answers the world gave it.

```csharp
Recorder.Install(boundary, directory: ".flight", enabled: Env("FLIGHT") == "1");

object? Greet(string user) => Recorder.Record("greet", new { user }, () =>
{
    var doc = store.Get(user);                 // fx
    var at = Recorder.Clock.Now();             // now
    return new Dictionary<string, object?> { ["name"] = doc.Name, ["at"] = at };
});
```

`RecordAsync` is the `Task` form. A `gate` on `Install` records only the calls that matter; a
`sink` publishes the whole session for hosts with no durable filesystem.

## Replay

```csharp
var tape = Replay.LoadTape(".flight/flight-….jsonl");
var call = Replay.PickCall(tape, fn: "greet");

var report = Replay.Call(call, kw => Greet((string)kw["user"]!), boundary);

report.Ok            // result and error both reproduce the recording
report.Divergence    // or: the exact point where behaviour changed
```

Replay does two jobs: hand the recorded answers back **in order**, and refuse to answer the wrong
question. If the code asks a different effect, in a different order, with different arguments — or
**stops asking** — that is caught. Divergence is not a failure of the tool; it *is* the finding.

## Edit the tape to visit a world that never happened

A recording is data, so hostile states are one edit away. Replay the *real* code against the
edited tape in **probe** mode (arguments no longer policed; name and order still gate), and let an
invariant judge what it did — a mutated recording plus a claim is a property test over the boundary.

```csharp
var rec = Recording.Load(tape);
var call = rec.Call(0);
call.Read("stream").Result = new List<object?>();     // empty corpus — the store can't answer this
call.Clock.Reverse();                                 // time runs backwards

var noDup = Invariants.Invariant("no seat dealt twice", v => { /* assert over v.Result */ });
var report = call.Check(kw => Greet((string)kw["user"]!), new[] { noDup });
rec.Save("flight/empty-corpus.jsonl");                // pin it: a probe suite member
```

## Tapes that carry meaning

A recording answers **"same?"**, an invariant answers **"right?"**; neither answers **"what was
this?"**. So the app can say, in its own words, what a stretch of execution *meant* — recorded
in-stream, around the raw events it produced.

```csharp
Recorder.Span("assign_turn", new { chore }, () => {
    var holder = store.Get($"member:{who}");    // every event inside is inside the span
    Recorder.Note("skipped", new { reason = "absent" });
});
```

Nothing validates or interprets the name — it is testimony, recorded next to the evidence, which
is what makes it checkable by someone else. `Recording.Load(tape).Call(0).RenderSpans()` reads the
call top-down. On replay the code testifies afresh and the two accounts are compared; a changed
claim is a **third signal** (`report.SemDivergence`), reporting-only unless you pass `semStrict`.

## Conformance

`FlightRecorder.Spec.Validate.ValidateTape(text)` is this runtime's mirror of the frozen checker
(`spec/validate.py` / `validate.js`). Every implementation validates every fixture in
[`spec/fixtures/`](https://github.com/xag/flight-recorder/tree/main/spec/fixtures), and the .NET
recorder produces `dotnet-toy.jsonl` and `dotnet-sem-toy.jsonl` there.

## Not here yet

**Variable-level tracing** — every local on every replayed line — is deferred: the CLR has no
`sys.settrace`, so it needs a debugger backend (the Node port drives the V8 Inspector from a
worker), and the spec *reserves* the trace markers for exactly such an addition. Recording and
replay do not need it.

## License

Apache-2.0 — see [LICENSE](https://github.com/xag/flight-recorder/blob/main/LICENSE).
