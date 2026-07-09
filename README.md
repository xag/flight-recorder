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

None of the underlying ideas are new, and this library stands on a long one:

- **Record nondeterminism, reconstruct everything by re-execution** is how
  [rr](https://rr-project.org/) and [Pernosco](https://pernos.co/) work — at the syscall
  level, for compiled programs, on Linux.
- **Time-travel / omniscient debugging**: WinDbg TTD, [Undo](https://undo.io/),
  [Replay.io](https://replay.io/).
- **Record/replay HTTP cassettes in tests**: [vcrpy](https://github.com/kevin1024/vcrpy)
  and friends; **clock shims**: [freezegun](https://github.com/spulec/freezegun).
- **Replay captured traffic against two builds and diff**: Twitter's
  [Diffy](https://github.com/opendiffy/diffy).
- **Deterministic simulation testing**: FoundationDB's simulator,
  [Antithesis](https://antithesis.com/), TigerBeetle's VOPR.
- **Functional core, imperative shell** (Gary Bernhardt) is the shape that makes any of
  this cheap to adopt.

What this library tries to occupy is the **middle altitude** those tools leave open: an
application-level, language-level, *declaration-driven* recorder — one small object names
the app's entire boundary (effect functions, chained clients, clock, random, constants) —
with tool-call granularity, bit-for-bit replay verification, a divergence taxonomy
(path / result / writes), and a full `sys.settrace` state trace on replay. It was built
for and proven on MCP tool servers, whose small pure cores and narrow boundaries fit this
shape unusually well — and whose operators are often AI agents, for whom a queryable
trace turns diagnosis from inference into lookup.

## License

MIT.
