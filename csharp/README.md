# flight-recorder (.NET)

Record what the outside world told your code — every store answer, HTTP response, clock read and
random draw — as one small JSONL file per request: a *tape*. Replay that file against your real
code later: same inputs, same execution, and when a replay diverges the report names the first
difference instead of leaving you to guess.

The .NET implementation of [flight-recorder](https://github.com/xag/flight-recorder). It writes
**tape format v1**, the same tapes the Python and Node implementations write, so one analysis
serves all three. Targets `netstandard2.0` (.NET Framework 4.6.1+, .NET Core, .NET 5–8+).

```bash
dotnet add package flight-recorder
```

## Quickstart

.NET can't patch modules or globals, so — like the Node port — the boundary is **the object the
app holds**: wrap it, and hold the recorder's `Clock`/`Random` handles where there's nothing to
wrap.

```csharp
using FlightRecorder;

var boundary = new Boundary().MaskFields("password");
IStore store = Recorder.WrapAs(realStore, "store", nameof(IStore.Get));   // a transparent proxy, not a mock

// record
Recorder.Install(boundary, directory: ".flight");
object? Greet(string user) => Recorder.Record("greet", new { user }, () =>
{
    var doc = store.Get(user);            // fx
    var at = Recorder.Clock.Now();        // now
    return new Dictionary<string, object?> { ["name"] = doc.Name, ["at"] = at };
});
Greet("alice");

// replay: the recorded answers are fed back; the real code re-runs exactly
var call = Replay.PickCall(Replay.LoadTape(Recorder.TapePath!), fn: "greet");
var report = Replay.Call(call, kw => Greet((string)kw["user"]!), boundary);
report.Ok;          // reproduced the recording bit-for-bit
report.Divergence;  // …or the exact point where behaviour changed
```

## The rest is in the guide

Redaction and the `forbid` tripwire, recording off-box with an `ISink`, editing the tape to visit
worlds that never happened, invariants (**"right?"** to a recording's **"same?"**), and semantic
spans — all of it, with runnable examples in every language:

**→ [xag.github.io/flight-recorder](https://xag.github.io/flight-recorder/)**

The tape is a frozen, cross-language standard:
[`spec/tape-v1.md`](https://github.com/xag/flight-recorder/blob/main/spec/tape-v1.md). The .NET
recorder ships its conformance checker (`FlightRecorder.Spec.Validate`) and produces
`spec/fixtures/dotnet-*.jsonl`, which Python, Node and .NET each validate.

**Not here yet:** variable-level tracing — the CLR has no `sys.settrace`, so it needs a debugger
backend, and the spec reserves the trace markers for exactly such an addition. Recording and
replay do not need it.

## License

Apache-2.0 — see [LICENSE](https://github.com/xag/flight-recorder/blob/main/LICENSE).
