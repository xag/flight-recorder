"""The boundary declaration: the one app-specific artifact.

A program's execution is fully determined by its code plus its nondeterministic inputs. A
Boundary names those inputs — nothing more. The recorder can't know about an input it was
never told crosses the boundary; when an app grows a new one (an HTTP call, a clock read, a
new random use), it must be added here. That is the whole maintenance contract.

Four kinds of input are supported:

- **effects**: module-level functions (sync or async) whose (args → result/exception) IS the
  external world — HTTP clients, storage helpers, auth-context readers.
- **chains**: chained-client object graphs (e.g. a Firestore client reached via an attribute
  on a service object), recorded by a transparent proxy that only knows which method names
  terminate a call chain.
- **clock / random**: modules whose `datetime` / `random` names get shimmed.
- **constants**: env-derived module constants, captured in the session header and restored
  on replay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Terminal method names for chain proxies, Firestore-flavored defaults.
DEFAULT_TERMINAL_READS = frozenset({"get", "stream"})
DEFAULT_TERMINAL_WRITES = frozenset({"set", "update", "delete", "add", "commit"})


@dataclass
class ChainTarget:
    """A chained client living at `getattr(holder, attr)` (e.g. holder=svc, attr='db')."""
    holder: Any
    attr: str
    terminal_reads: frozenset = DEFAULT_TERMINAL_READS
    terminal_writes: frozenset = DEFAULT_TERMINAL_WRITES


@dataclass
class Boundary:
    # [(module, [function names])] — wrapped in place; record logs, replay serves.
    effects: list = field(default_factory=list)
    chains: list = field(default_factory=list)  # [ChainTarget]
    clock_modules: list = field(default_factory=list)   # modules whose `datetime` is shimmed
    random_modules: list = field(default_factory=list)  # modules whose `random` is shimmed
    constants: list = field(default_factory=list)       # [(module, name)] header-captured
    # exception revivers for recorded effect errors: type name -> (args list) -> Exception.
    # Unlisted types replay as flight_recorder.replay.ReplayedEffectError.
    error_revivers: dict = field(default_factory=dict)
    # extra key/values for the session header (digests, versions...): name -> () -> value
    header_extras: dict = field(default_factory=dict)
    # field-name redaction, applied to every recorded payload (tool kwargs/results, effect
    # args/kwargs/results/errors, chain reads/writes) before it is written or published,
    # and re-applied to the replayed side of every comparison so a redacted recording
    # still verifies. A set/list of names masks them as serial.REDACTED; a dict maps
    # name -> transform (None = mask), where a transform receives the jsonable value and
    # must be deterministic AND idempotent — replay re-applies it to already-transformed
    # values. Field-name driven: it cannot reach positional values with no name (pass
    # sensitive values as keywords) or chain signatures (which render arguments).
    redact: Any = field(default_factory=dict)
    # The tripwire that backstops `redact`. Redaction is declarative and opt-in, so it
    # protects exactly the fields you thought of, and its failure mode is silent and open:
    # forget `salt` and the tape leaks; add `recovery_token` to the model next month and the
    # tape leaks; rename a field and the rule quietly stops matching. Nothing tells you. And
    # by construction it cannot reach a value with no name at all — a positional argument, a
    # chain signature, an opaque repr.
    #
    # `forbid` states the property those rules cannot: THIS TAPE CARRIES NO CREDENTIAL. Each
    # entry is a regex (a str or a compiled pattern) matched against the fully-redacted JSON
    # line the recorder is about to write; a hit raises record.ForbiddenValue and writes
    # nothing — not to the file, not to the sidecar, not to a sink. It turns "I forgot one"
    # from an invisible leak into a noisy failure at record time.
    #
    # Match shapes, not values: a credential you can enumerate you can already redact. It is
    # the one you cannot name that this is for.
    #
    #     forbid=[r"\b[a-f0-9]{64}\b",          # a scrypt digest survived redaction
    #             r"-----BEGIN [A-Z ]*PRIVATE KEY-----"]
    forbid: Any = field(default_factory=list)

    def redact_rules(self) -> dict:
        """The redact declaration normalized to {field_name: transform_or_None}."""
        if isinstance(self.redact, (set, frozenset, list, tuple)):
            return {name: None for name in self.redact}
        return dict(self.redact or {})

    def forbid_patterns(self) -> list:
        """The forbid declaration normalized to compiled patterns."""
        return [p if hasattr(p, "search") else re.compile(p) for p in (self.forbid or [])]

    def revive_error(self, err: dict) -> BaseException:
        reviver: Optional[Callable] = self.error_revivers.get(err.get("type", ""))
        if reviver is not None:
            try:
                return reviver(err.get("args", []))
            except Exception:
                pass
        from flight_recorder.replay import ReplayedEffectError
        return ReplayedEffectError(f"{err.get('type')}: {err.get('repr', '')}")
