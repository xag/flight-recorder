# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record an app's tool calls at their **nondeterminism boundary**; replay them
deterministically with **every internal variable observable**.

**[The slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html)**
([source](docs/slides.html)): the approach and the philosophy in fifteen slides, with the
production case study that shaped it.

A program's execution is fully determined by its code plus its nondeterministic inputs —
storage results, HTTP responses, the clock, random draws. Record just those, per call
(cheap: one JSONL line), and any surface behavior can later be promoted into a complete
simulation of the execution: replayed bit-for-bit on the real code, under a tracer that
captures every variable change, without touching production data or re-hitting any service.

The cardinal rule — for this lib and for every boundary declaration it consumes — is
**instrument, never duplicate**. Nothing here evaluates a query, reimplements a client, or
knows what any value means. Recording is a transparent proxy; replay feeds recorded answers
back and verifies the questions match. The only structural knowledge anywhere is *names*.

## The approach, in plain words

Your program is deterministic except where the world leaks in: what the database answered,
what the API returned, what time it was, what the dice rolled. Everything else follows
mechanically from those. So:

1. **Name the doors.** Declare, once per app, the handful of places where the outside world
   enters — that declaration is the *boundary*, and it is the only app-specific artifact.
   Nothing behind the doors is ever imitated or mocked; real code runs everywhere.

2. **Record what came through.** With recording on, each tool call writes one line: its
   inputs, every answer the world gave it, in order, and its result. That line is cheap to
   capture in production — and it is *complete*: since the code is deterministic given
   those answers, the line IS the execution, compressed.

3. **Replay is resurrection, not re-enactment.** Feed the recorded answers back and the
   real code re-runs the original execution exactly — no network, no database, no waiting
   for the bug to happen again. A tracer watches every variable of the resurrected run, so
   "what was `level` when it went wrong?" is a lookup, not an inference. If the replay asks
   the world a different question than the recording holds, you're told precisely where
   the code's behavior changed.

4. **Recordings answer "same?", invariants answer "right?".** A pinned recording is a
   regression test: the code still does what it did — but a bug records as faithfully as a
   fix, so no recording can call the first observation of a bug wrong. For that you write
   an *invariant*: a claim that must hold on every execution ("never claims the corpus is
   finished while words remain"). Claims are checked against any recording — output,
   internal variables, and writes alike — and they condemn a bug the first time it is ever
   seen.

5. **Edit the tape to visit worlds that never happened.** A recording is data, so hostile
   states are one edit away: empty the query result, run the clock backwards, hand back an
   absurd number. Replay the real code against the edited tape and let the invariants
   judge. That finds the bugs no real traffic has triggered yet — cheaply, and without a
   test database that can produce impossible states on demand.

The sections below are the reference for each step: boundary, record (with per-call gating
and off-box sinks), replay, the pinned-recording pytest suite, invariants, and mutation.

## Declare the boundary (the one app-specific artifact)

```python
import flight_recorder as fr
from app import http_client, storage_client, tools_core

BOUNDARY = fr.Boundary(
    effects=[(http_client, ["fetch", "post"]),          # module functions, sync or async
             (storage_client, ["read", "write"])],
    chains=[fr.ChainTarget(svc, "db")],                  # chained clients (Firestore-style)
    clock_modules=[tools_core],                          # modules whose datetime.now matters
    random_modules=[storage_client],                     # modules whose random matters
    constants=[(tools_core, "SOME_ENV_CAP")],            # env-derived, header-captured
    error_revivers={"ApiError": lambda args: http_client.ApiError(*args)},
)
```

The recorder cannot know about an input it was never told crosses the boundary. When the
app grows a new one, add it here — that's the whole maintenance contract.

## Record

```python
fr.install(BOUNDARY, tools_core, directory="flight",
           enabled=bool(os.getenv("MYAPP_FLIGHT_RECORDER")))
```

Off by default; when enabled, every public function in `tools_core` writes one record per
call — bound args, ordered boundary events, result — to a session file.

### Record one call, not one deployment

`enabled` is a gate. A bool decides once for the whole process — falsy is a total no-op,
nothing is patched. A **callable `(tool_name, kwargs) -> bool` is consulted on every tool
call**, so a running server can record a single user's request while leaving the rest of
its traffic untouched, with no env flip and no redeploy:

```python
RECORDING_FOR = contextvars.ContextVar("recording_for", default=None)

fr.install(BOUNDARY, tools_core, directory="flight",
           enabled=lambda tool, kwargs: kwargs.get("email") == RECORDING_FOR.get())
```

The session file is opened by the first call the gate admits — a gate that never fires
leaves no file at all, and `session_path()` stays `None`. A gate that raises is treated as
a "no": it can never break the call it was asked about.

The **outermost tool call decides for the whole tree**. The gate is asked once, about the
call that entered the process; a tool invoked by another tool is never gated again, and
folds into its caller's record — so a declined outer call cannot leave a fragmentary
recording of some inner tool that starts mid-request. The name the gate is asked about is
the name clients call, which under `install_mcp` is the registered tool name, not the
Python function's.

### Retrieve without touching the box

Pass a `sink` and the session is published as it grows — after the header, then after every
completed call — so recordings are retrievable from a machine you have no shell on. The
protocol is one method, and the library stays dependency-free:

```python
class S3Sink:
    def __init__(self):
        self.q = queue.SimpleQueue()
        threading.Thread(target=self._drain, daemon=True).start()

    def publish(self, name: str, data: bytes) -> None:
        self.q.put((name, data))          # hand off and return; never block the caller

    def _drain(self):
        while True:
            name, data = self.q.get()
            boto3.client("s3").put_object(Bucket="flight", Key=name, Body=data)

fr.install(BOUNDARY, tools_core, directory="flight", sink=S3Sink())
```

**`publish` runs synchronously, under the recorder's write lock, on the thread that finished
the call** — in an async server, the event-loop thread. A sink that blocks on network I/O
therefore stalls every concurrent request, not just the recorded one. Hand the bytes off and
return, as above. Raising `Exception` is ignored: like the crash sidecars, publication is
best-effort and will not break the call. Only *completed* calls are published; a call that
dies mid-flight leaves its last words in a local `.inflight` sidecar, readable only on the
box.

### Redact before anything leaves the process

Recordings hold what crossed the boundary — verbatim, which for boundaries that carry PII
or credentials is the problem. `redact` names the fields that must never reach disk or a
sink:

```python
BOUNDARY = fr.Boundary(
    effects=[...],
    redact={"password", "ssn"},     # masked as fr.REDACTED wherever these keys appear
)

# or map a field to a deterministic tokenizer, keeping distinctness without the value:
redact={"email": lambda v: v if str(v).startswith("tok:") else "tok:" + hmac_hex(v)}
```

Rules are field-name driven and applied to every recorded payload — tool kwargs and
results, effect args/kwargs/results/errors, chain reads and writes — *before* the value is
serialized into an event, so neither the session file, the `.inflight` sidecars, nor a
sink ever holds the raw value. The gate still sees raw kwargs: you can admit a call by the
very field the recording then masks.

**Replay re-applies the same rules to its side of every comparison** (effect arguments,
writes, the result), so a redacted recording still verifies bit-for-bit. Two consequences:

- A transform must be deterministic and **idempotent** — on replay it runs again over
  values that are already transformed. A transform that raises degrades to the mask: the
  failure direction is "masked", never "leaked" and never "broke the recorded call".
- A redacted field replays *as its mask*. Code that merely carries the value through is
  fine; code that computes with it — branches on it, folds it into a string — will
  legitimately diverge on replay, because the recording holds no answer for what the value
  was. Redact identifiers and payloads that pass through unchanged; what feeds control
  flow or output has to stay recorded (tokenize it instead of masking if it must not
  appear raw).

What redaction cannot reach, because the lib knows only names: a sensitive value passed
*positionally* to an effect (pass it as a keyword), values rendered into a chain's
signature (`document(x@y.z)` — the signature is the matching key), the verbatim `repr` in
a recorded effect error (add `"repr"` to the rules to mask all of them) and in a failed
call's `error` field, and header `constants` (they are restored on replay; don't declare
secrets as constants).

## Replay

```python
class Adapter(fr.ReplayAdapter):
    boundary = BOUNDARY
    trace_root = os.path.dirname(tools_core.__file__)
    def resolve(self, fn_name, feed):
        return getattr(tools_core, fn_name)

sys.exit(fr.run_cli(Adapter()))   # in the app's `python -m app.replay`
```

```
python -m app.replay flight/<session>.jsonl              # list recorded calls
python -m app.replay flight/<session>.jsonl --call 2     # replay + full state trace
python -m app.replay ... --call 2 --watch level,total    # variable timeline
```

Exit 0: the replay reproduced the recording bit-for-bit. Exit 2: divergence — in the code
path (the first differing boundary question is named), the result, or the writes (compared,
never executed). Pin recordings as fixtures: record once, replay against every build.

## Non-regression testing

Pinning is the point, so the pytest plugin ships with the library. Point it at a directory
of pinned sessions; every recorded call becomes its own test:

```toml
# pyproject.toml
[tool.pytest.ini_options]
flight_recordings = "tests/recordings"          # a directory of pinned .jsonl sessions
flight_adapter = "app.replay:Adapter"           # your ReplayAdapter
flight_trace = "build/traces"                   # optional: state traces per replay
```

```
$ pytest
tests/recordings/login-bug.jsonl::call0::authenticate PASSED
tests/recordings/login-bug.jsonl::call1::fetch_profile FAILED
```

A failure prints the same divergence report the CLI does — which boundary question changed,
or how the result differs. The plugin is inert until `flight_recordings` is set, so it costs
nothing to projects that merely depend on this library. It also exposes a `flight_replay`
fixture for assertions the collector can't express. Replay patches the boundary
process-wide, so don't run these under `pytest-xdist`.

**A pinned recording is a regression oracle, not a correctness one.** It asserts that the
code still behaves as it behaved when you pinned it — never that the recorded behavior was
right. Deciding *that* needs a spec — which is what invariants are.

## Invariants

An invariant is a claim about **every** execution, written once and checked against any
recording — so it can condemn the very first observation of a bug, which no recording can.
A bug replays bit-for-bit forever; only a spec can call it wrong.

```python
@fr.invariant("never claims end-of-corpus while words remain")
def _(t: fr.Trajectory):
    assert not (t.result["done"] and t.result["corpus"] - t.result["deck"] > 0)

@fr.invariant("level never excludes the whole corpus")
def _(t: fr.Trajectory):
    for obs in t.trace.values("level"):
        assert obs.value > 0, f"level={obs.value} at {obs.at}"
```

The second claim is the reason this exists: the production bug that shaped this library was
an internal variable (`level=0`) silently emptying a whole corpus, with a perfectly
self-consistent output. `t.result` is what the replayed code produced; `t.trace` is its
internal trajectory, queryable — `values(name)` is the `--watch` timeline as data,
`calls`/`returns`/`raised` cover the control flow. Traced values are recorded as **data**,
not reprs: numbers compare, documents read as dicts, and long collections carry a prefix
that still reports its true `len()`.

Check by hand:

```python
report = fr.check_invariants(session, 0, Adapter(), INVARIANTS)
assert report.ok, fr.format_invariant_report(report)
```

`report.ok` demands both verdicts: the replay reproduced the recording AND every invariant
held. They stay separately readable (`report.reproduced`, `report.outcome`) because they
impeach different things. For a tool that legitimately raises, `t.result` is None on the
error path — guard result-reading invariants with `t.raised`.

or wire them into the pytest plugin, where every pinned recording then answers both
questions — does the code still do what it did (replay), and is what it does right
(invariants):

```toml
flight_invariants = "myapp.claims"          # a module of @invariant defs, or "module:LIST"
```

A failed invariant is reported as `reproduces, but the code is wrong` — a different finding
from a divergence, because it is one: a divergence impeaches the recording, a violation
impeaches the code. A replay that diverged checks nothing (its trajectory is fiction), and
an invariant that itself raises is reported as a broken invariant, not as a bug in the code.

## Mutation replay

Recordings make impossible states cheap to construct — they're data, not database setup.
Edit a recording's boundary answers and replay the real code against the hostile world:

```python
rec = fr.Recording.load(path)
call = rec.call(0)
call.read(op="stream").result = []                 # empty corpus
call.effect("fetch_remote").result = {"v": 10**9}  # absurd remote answer
call.clock.reverse()                               # time runs backwards
call.set_kwargs(level=0)                           # hostile input

report = call.check(Adapter(), INVARIANTS)
assert report.ok, fr.format_invariant_report(report)

rec.save(recordings_dir / "empty-corpus.jsonl")    # pin it — now it's a suite member
```

A mutated call replays in **probe mode**: the tape answers the code's boundary questions
(matched by name, in order, skipping events the new path no longer asks) but stops policing
arguments, writes, and outputs — under mutation those comparisons are meaningless, so the
verdict belongs entirely to the invariants. A mutated recording plus a declared claim is a
property test over the boundary. Saved mutations carry `"probe": true` and the pytest
plugin checks them in probe mode automatically (they require `flight_invariants`).

Semantics to hold on to:

- **Mutation edits answers; it never re-executes effects.** Emptying a corpus changes what
  the code *asks* downstream, but each downstream effect still answers from the tape. To
  make an effect fail, inject its failure: `call.effect("x").error = ("ApiError", [...])`.
  Same-name effects are answered in recorded order — if a mutation drops one of several
  calls to the same effect, the remaining calls receive the earliest answers; mutate those
  events too (each skip is named in `report.replay.warnings`).
- **Writes are trajectory.** Never executed, always captured: `t.writes` holds every write
  the replayed code performed (op, chain signature, args), so "never writes when the
  corpus is empty" is an assertable claim.
- **A crash cannot pass silently.** The strict-mode guard style (`if t.raised: return`)
  would let a suite of polite claims wave a crash through. Under probe, a raise that no
  claim judges is its own outcome, `raised` — never ok. If raising is the *correct*
  hostile-input behavior, say so: `@invariant("rejects an empty corpus", judges_raise=True)`
  with `assert t.raised and "ValueError" in t.error`.
- **The tape only reaches so far.** A mutation that redirects the code onto a path asking a
  question the recording holds no answer for is the outcome `unanswerable` — impeaching
  neither the code nor the claim, only this recording's reach. Edit the events the new path
  needs (e.g. `call.rand().idx = [0]` after shrinking a sampled collection), or record a
  closer execution. Chain reads are matched by *shape* (`collection.where.stream`), so one
  collection's recorded rows can never answer a different collection's query.

## What it can and cannot see

Replay finds logic bugs as lookups instead of inferences: replay a production recording
locally, `--watch` the suspicious variable, read the answer. It cannot see below the
process: memory, latency, and concurrency interleavings belong to logs and measurement,
not to this instrument. Hard crashes leave their last words — each call's events stream
to an `.inflight` sidecar, so a SIGKILLed call's partial record survives and the CLI
lists it as `INCOMPLETE` — but the crash's *cause* still lives in the machine layer.

The boundary is also bounded by the *process*: an input is recordable exactly when it
enters the server as a Python-level call. That draws a clean line through MCP Apps'
client-side UI round-trips. A round-trip the tool **awaits** — elicitation, sampling, a
`ui/*` response surfaced as a method on the session/context object — is an effect like any
other; declare that method and it records and replays like an HTTP call
(`effects=[(Context, ["elicit"], {"method": True})]` — the `method` opt skips `self` in
matching). But UI state that lives and dies in the client — rendering, interaction that
never re-enters the tool's execution — never crosses into the process, so it is invisible
here, like any remote peer's internals. A session driven by such interaction is
reproducible only from the server's side of the conversation: every tool call it caused,
bit-for-bit; the clicks between them, not at all.

## Lineage and positioning

**None of the underlying ideas are new.** This library is a small recombination of old,
well-studied ones, and the honest way to describe it is by naming what it descends from.

### The direct ancestor: R2 (OSDI'08)

Microsoft Research's [**R2: An Application-Level Kernel for Record and
Replay**](https://static.usenix.org/events/osdi08/tech/full_papers/guo/guo_html/) (Guo et
al., OSDI 2008) is where the central idea comes from. R2 "allows developers to choose at
what interface the interactions between the application and its environment are recorded
and replayed," with developers *annotating* the chosen functions — explicitly rejecting
the fixed low-level (syscall) boundary of predecessors like liblog and Jockey. This
library's `Boundary` object is R2's choose-your-own-interface idea, expressed in Python.
If you cite one thing here, cite R2.

### The closest functional neighbour: Keploy

[**Keploy**](https://github.com/keploy/keploy) already does multi-effect record/replay —
incoming HTTP plus outgoing dependency calls (SQL queries, external APIs, message
queues) — captured at the eBPF syscall/socket layer, code-less and language-agnostic
(Python included), replaying with dependencies virtualized and verifying by response
diff. **Do not read "declares a multi-effect boundary" as "first to record multiple
effect kinds."** Keploy got there, at a lower altitude. The differences that remain:
Keploy mocks the boundary *by kind* (network and DB wire protocols, Linux-only via eBPF)
and its regression signal is a response diff; this library declares the boundary *by
name* at the application level, replays the real code under `sys.settrace`, and reports
where a divergence happened (code path / result / writes) rather than only that one did.

### Neighbours at other altitudes

| | What it does | Where it differs |
|---|---|---|
| [rr](https://rr-project.org/), [Pernosco](https://pernos.co/), WinDbg TTD, [Undo](https://undo.io/) | record nondeterminism, reconstruct by re-execution | syscall/machine level; the runtime fixes the boundary |
| [PyPy RevDB](https://pypy.org/posts/2016/07/reverse-debugging-for-python-8854823774141612670.html) | logs non-deterministic op results, replays from the log | the *interpreter* draws the line, not the author |
| [vcrpy](https://github.com/kevin1024/vcrpy), betamax, responses, [freezegun](https://github.com/spulec/freezegun), [time-machine](https://github.com/adamchainz/time-machine) | record/shim one effect kind | single-effect; no tracer, no divergence taxonomy |
| [Temporal](https://python.temporal.io/temporalio.worker.Replayer.html), [DBOS](https://docs.dbos.dev/), Restate, Inngest | genuinely replay side-effect outputs against a determinism boundary | an authoring model you write *into* (decorators, workflows/activities), not instrumentation of existing code |
| [snoop](https://github.com/alexmojaki/snoop), [hunter](https://github.com/ionelmc/python-hunter), [VizTracer](https://github.com/gaogaotiantian/viztracer) | trace execution, log variables | observe only — no external-input capture, so no deterministic replay |
| [Diffy](https://github.com/opendiffy/diffy), Keploy, [MCPSpec](https://www.npmjs.com/package/mcpspec) | replay captured traffic against a new build, diff responses | outside-in at the protocol boundary; the internals stay opaque |
| [Antithesis](https://antithesis.com/), FoundationDB simulator, TigerBeetle VOPR | deterministic simulation of whole systems | platform/runtime-scale, not a library you add to an app |

Also upstream: **functional core, imperative shell** (Gary Bernhardt) is the code shape
that makes any of this cheap to adopt.

### So what is actually new here?

As of July 2026, no maintained Python library appears to combine all four of:
a developer-**declared** multi-effect boundary (I/O + clock + randomness + identity);
record-then-replay of application-level calls **against the real code**; full
variable-state tracing on replay (`sys.settrace`); and **bit-for-bit** verification with
a code-path / result / writes divergence taxonomy. That is the gap this fills — a
narrow one, in a crowded neighbourhood. It was built for and proven on MCP tool servers,
whose small pure cores and narrow boundaries fit this shape unusually well, and whose
operators are increasingly AI agents — for whom a queryable trace turns diagnosis from
inference into lookup.

If you know of prior art that lands in that intersection, please open an issue; the claim
above is a survey result, not a proof.

## License

MIT.
