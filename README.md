# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Record an app's tool calls at their **nondeterminism boundary**; replay them
deterministically with **every internal variable observable**.

**[The slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html)**
([source](docs/slides.html)): the approach and the philosophy in twelve slides, with the
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

## What it can and cannot see

Replay finds logic bugs as lookups instead of inferences: replay a production recording
locally, `--watch` the suspicious variable, read the answer. It cannot see below the
process: memory, latency, and concurrency interleavings belong to logs and measurement,
not to this instrument. Hard crashes leave their last words — each call's events stream
to an `.inflight` sidecar, so a SIGKILLed call's partial record survives and the CLI
lists it as `INCOMPLETE` — but the crash's *cause* still lives in the machine layer.

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
