"""The docs ledger — the documentation architecture as data a check can go red on.

flight-recorder ships one library in six runtimes, and its docs have exactly one failure
mode worth a rule: they drift, and they play favourites. The root README was a Python tutorial
(it doubled as the PyPI page), the guide is bilingual-turned-trilingual, and adding .NET meant
editing the guide and three READMEs at once. So two rules, recorded here and — this is the point
— *checked* here, not merely written down:

  - **no privileged language**: the repo landing is one neutral README, and the guide documents
    every shipped runtime through the same tabs. No language gets a standalone tutorial.
  - **no doc duplication**: the walkthrough lives in exactly one place, the guide. READMEs link
    to it; they do not reproduce it.

Each is a `gate` node carrying a quantity that `build()` can only ground by scanning the real
tree. Compliant → grounded → green. A per-language README with a code walkthrough reappears,
or a runtime ships without a guide tab → the quantity cannot be grounded → the gate goes red
under `nothing-unsound-passes-a-gate`, and `python -m ledger.check` exits 1. The check reads the
files; it does not take this docstring's word for anything.

Bootstrapped 2026-07-18, when the README restructure settled the architecture and it became worth
holding to account.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import quern.grounding  # noqa: F401 -- the grounding natives, for the gate rule
from quern import Node, Quantity, Quern

_ROOT = Path(__file__).resolve().parents[1]

# Directories that are not source: vendored deps, virtualenvs, build output. `vendor` and
# `target` are Composer's and Maven's; both fill with third-party READMEs full of code fences,
# and a gate that read them would go red over documentation nobody here wrote and nobody here
# can fix.
_SKIP = {".git", "node_modules", ".venv", "bin", "obj", ".dotnet", "dist",
         "__pycache__", ".pytest_cache", "vendor", "target"}

# The runtimes flight-recorder ships, and the guide tab each must have. A runtime that ships
# without a tab here is a privileged-language violation by omission.
_RUNTIME_TABS = {"Python": "py", "Node": "js", ".NET": "cs", "Go": "go", "Java": "java",
                 "PHP": "php"}

_GUIDE = _ROOT / "docs" / "index.html"
_ROOT_README = _ROOT / "README.md"


def _readmes() -> list[Path]:
    return [p for p in _ROOT.rglob("README.md")
            if not any(part in _SKIP for part in p.relative_to(_ROOT).parts)]


def _rel(p: Path) -> str:
    return p.relative_to(_ROOT).as_posix()


def _has_code_fence(p: Path) -> bool:
    return "```" in p.read_text(encoding="utf-8")


def _nonblank_lines(p: Path) -> int:
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def build() -> Quern:
    from quern.library import consume
    lib, refs = consume(_ROOT, os.environ.get("QUERN_REGISTRY",
                                              _ROOT.parent / "quern-registry"))
    quern = Quern(packages=[next(r for r in refs if r.name == "ledger")])
    quern = lib.effective(quern)
    quern.root.children = [_DECISION, _no_privileged_language(), _no_doc_duplication(),
                           _PARITY_DECISION, _feature_parity(), _JAVA_DECISION,
                           _PYPI_NAME_DECISION, _PHP_DECISION,
                           _DISTRIBUTION_DECISION, _install_claims_match_reality(),
                           _SLIDES_DECISION, _slides_name_every_runtime(),
                           _CANONICAL_DECISION, _fixture_parity()]
    return quern


# --- the decision -----------------------------------------------------------------------

_DECISION = Node(
    id="docs-single-source",
    kind="decision",
    name="Documentation has one home per job: the guide (docs/index.html) is the single "
         "cross-language walkthrough, the root README is a neutral landing that links to it, and "
         "each package registry page is a link stub",
    payload={
        "rationale":
            "The root README was pinned as the PyPI long-description, so it carried a full Python "
            "tutorial — a Python-specific page at the landing spot of a three-runtime repo, and a "
            "duplicate of the guide. Two harms, one cause: a privileged language, and content that "
            "must be edited in lockstep across four files (adding .NET touched the guide and three "
            "READMEs). Single-source fixes both: the guide is the one place a walkthrough lives and "
            "it treats every runtime through the same tabs; the root README says what the project is "
            "and links onward; registries — which cannot render the tabbed guide anyway — get a "
            "stub with the link. The two rules below are not prose here: they are gates a scan can "
            "fail.",
        "consequence":
            "Adding a runtime is an edit to one file (the guide) plus one table row. Package READMEs "
            "carry no walkthrough to drift. The cost is that a registry visitor who never clicks "
            "through sees only a pointer — accepted: a captive shopfront earns a link, not a copy.",
    },
    children=[
        Node(id="alt-per-language-readmes", kind="alternative",
             name="Give each runtime its own full README beside its package",
             payload={"why":
                      "Symmetric, but it is the duplication itself: three copies of the walkthrough "
                      "to keep in step with the guide and each other, and the drift lands silently. "
                      "The thing the no-doc-duplication gate exists to forbid."}),
        Node(id="alt-readme-is-source-of-truth", kind="alternative",
             name="Make the root README the comprehensive doc; let the site mirror it",
             payload={"why":
                      "Re-privileges whatever language the root README speaks (there is only one, "
                      "and it is captive to PyPI), and a single-language README cannot show the "
                      "side-by-side that a cross-language library's readers actually need. The "
                      "tabbed guide can; the README cannot."}),
    ],
)


# --- the gates: rules with teeth --------------------------------------------------------

def _no_privileged_language() -> Node:
    readmes = _readmes()
    # A non-root README that carries its own tutorial (a code walkthrough, or more than a stub's
    # worth of prose) privileges its language.
    privileged = [_rel(p) for p in readmes
                  if p != _ROOT_README and (_has_code_fence(p) or _nonblank_lines(p) > 20)]
    guide = _GUIDE.read_text(encoding="utf-8") if _GUIDE.exists() else ""
    missing = [name for name, tab in _RUNTIME_TABS.items() if f'data-set="{tab}"' not in guide]

    if not privileged and not missing:
        q = Quantity(
            value=0, unit="finding", provenance="measured", grounded=True,
            source=f"scanned {len(readmes)} README(s): only the root landing carries content, and "
                   f"the guide has a tab for every shipped runtime "
                   f"({', '.join(_RUNTIME_TABS)})")
    else:
        problems = []
        if privileged:
            problems.append("per-language READMEs carry their own tutorial: " + ", ".join(privileged))
        if missing:
            problems.append("the guide has no tab for shipped runtime(s): " + ", ".join(missing))
        q = Quantity(
            value=len(privileged) + len(missing), unit="finding", provenance="measured",
            grounded=False,
            source="; ".join(problems) + " — the guide is the single home; document every runtime "
                   "there, through the tabs, and keep the READMEs neutral")

    return Node(
        id="docs-name-no-privileged-language",
        kind="gate",
        name="No language gets a privileged doc: the repo landing is one neutral README, and the "
             "guide documents every runtime through the same tabs",
        params={"privileged_or_missing": q},
        # The gate is fitted against its own measurement: untrusted_via('admits') reads the param
        # on the node it links to, so the gate admits itself. Ungrounded (a violation the scan
        # could not clear) → the gate goes red.
        links={"admits": ["docs-name-no-privileged-language"]},
        payload={"note": q.source},
    )


def _no_doc_duplication() -> Node:
    readmes = _readmes()
    # The walkthrough is code. A README carrying a fenced code block is reproducing the guide.
    duplicated = [_rel(p) for p in readmes if _has_code_fence(p)]

    if not duplicated:
        q = Quantity(
            value=0, unit="readme", provenance="measured", grounded=True,
            source=f"scanned {len(readmes)} README(s): none reproduces the walkthrough — no README "
                   f"carries a fenced code block, so the tutorial lives once, in docs/index.html")
    else:
        q = Quantity(
            value=len(duplicated), unit="readme", provenance="measured", grounded=False,
            source="these READMEs reproduce the guide (they carry a code walkthrough): "
                   + ", ".join(duplicated) + " — keep the walkthrough only in docs/index.html and "
                   "link to it")

    return Node(
        id="docs-carry-no-duplicated-tutorial",
        kind="gate",
        name="The walkthrough lives in exactly one place — the guide. READMEs point to it; they do "
             "not reproduce it",
        params={"tutorial_in_readmes": q},
        links={"admits": ["docs-carry-no-duplicated-tutorial"]},
        payload={"note": q.source},
    )


# --- feature parity: the six runtimes are one library -----------------------------------

_PARITY_DECISION = Node(
    id="feature-parity",
    kind="decision",
    name="Every runtime ships every feature. The six implementations are one library, not a lead "
         "implementation with ports trailing it: the guide shows the same feature set in all tabs, "
         "no badge restricts a feature to some languages, and no 'not yet' note stands in for a "
         "feature a runtime is missing.",
    payload={
        "rationale":
            "A shared tape promises that a program's behaviour is portable across runtimes. A "
            "feature gap breaks that promise unevenly: a tape recorded where invariants exist "
            "cannot be judged where they do not, and a user who picks a runtime silently inherits "
            "less library than the next person. So parity is the rule and 'implement it "
            "everywhere' is the only discharge — never a footnote documenting the gap. The gate "
            "reads the guide, because the guide is where a disparity becomes visible to a reader: "
            "a badge that names only some runtimes, or a 'not yet' note, IS a feature that has not "
            "reached parity, and it goes red until the feature lands in every runtime.",
        "consequence":
            "A feature is not shipped until it is shipped in all six runtimes; the guide then "
            "gains a tab, never a badge. The disparity is the finding, not an accepted asterisk. "
            "This is the strictest gate in the ledger, and deliberately so — and it has now been "
            "paid once, which is the evidence it works. Variable-level tracing was the hard case: "
            "the gate held red on the reading that it needed a debugger backend where there is no "
            "sys.settrace, and that reading turned out to be WRONG. Neither .NET nor Go got a "
            "debugger. Both got a rewriter — Roslyn over the sources in .NET, go/ast over a copy "
            "of the module in Go — because the gate refused the footnote long enough for someone "
            "to look for the third option. A gap documented honestly would have shipped the "
            "asterisk and never found it. Java later took the same road a third time, with javac's "
            "own com.sun.source, and PHP a fourth with token_get_all, which is now FOUR runtimes the "
            "'it needs a debugger' reading was wrong about — at which point it is not a lucky run of "
            "third options, it is the answer. "
            "AND THE GATE ITSELF WAS CAUGHT: the note claiming .NET lacked tracing sat in the guide "
            "through the whole of the Go port, reporting green, because the gap-phrase pattern was "
            "written [^<.] and so could never match the one runtime name containing a dot. A guard "
            "with a hole shaped like the thing it guards is worse than no guard, because it is "
            "believed. Found by reading the guide rather than by trusting the gate, which is the "
            "uncomfortable lesson: a check is evidence about what it can see, never about what it "
            "cannot.",
    },
    children=[
        Node(id="alt-lead-and-ports", kind="alternative",
             name="A lead runtime (Python) with the others as ports that catch up over time",
             payload={"why":
                      "Where the project started, and exactly the drift this forbids: the guide "
                      "fills with per-language badges and 'not yet' notes, and the shared-tape "
                      "promise decays to 'portable, except for whatever your runtime has not caught "
                      "up on'. A gap with no deadline is a gap forever."}),
        Node(id="alt-document-gaps-honestly", kind="alternative",
             name="Allow gaps, but document them honestly, per language",
             payload={"why":
                      "Honest, and still wrong: a documented gap is a gap nobody is required to "
                      "close, so it normalises the disparity and makes it permanent. The gate makes "
                      "the gap fail a build instead of fill a footnote."}),
    ],
)

# Feature badges in the guide look like <span class="badge">Python · .NET</span>. A badge that
# does not name every runtime restricts that feature to the ones it lists.
_BADGE = re.compile(r'<span class="badge">([^<]*)</span>')
# Notes that stand in for a missing feature. These are the shapes a "this runtime lacks X" note
# takes; each is a parity violation to be discharged by implementing the feature, not reworded.
_GAP_PHRASES = [
    # `[^<]`, not `[^<.]`: the character class used to exclude the dot, which meant this pattern
    # could never match ".NET" — the one runtime name that contains one. A stale "not in the .NET
    # port yet" note sat in the guide, through the whole of the Go port, invisible to the gate that
    # exists to forbid exactly it. A guard with a hole shaped like one of the things it guards is
    # worse than no guard, because it reports green.
    r'not in the [^<]{0,30}?port',
    r'not yet',
    r'does not have[^.<]{0,40}?yet',
    r'does not ship an?[^.<]{0,40}?runner',
    r'waits on variable-level tracing',
]


def _feature_parity() -> Node:
    guide = _GUIDE.read_text(encoding="utf-8") if _GUIDE.exists() else ""
    findings: list[str] = []

    for m in _BADGE.finditer(guide):
        text = m.group(1)
        missing = [name for name in _RUNTIME_TABS if name not in text]
        if missing:
            findings.append(f'badge "{text.strip()}" excludes {", ".join(missing)}')

    for pat in _GAP_PHRASES:
        for m in re.finditer(pat, guide, re.IGNORECASE):
            findings.append(f'"{m.group(0).strip()}" — a feature a runtime lacks')

    runtimes = ", ".join(_RUNTIME_TABS)
    if not findings:
        q = Quantity(
            value=0, unit="disparity", provenance="measured", grounded=True,
            source=f"the guide restricts no feature: every feature badge names all of {runtimes}, "
                   f"and no 'not yet' note stands in for a missing one")
    else:
        q = Quantity(
            value=len(findings), unit="disparity", provenance="measured", grounded=False,
            source="the guide documents features that some runtimes lack, instead of shipping them "
                   "everywhere: " + "; ".join(findings) + f" — bring every runtime ({runtimes}) to "
                   "the same feature set and remove the badge/note, do not reword it")

    return Node(
        id="all-runtimes-same-features",
        kind="gate",
        name="Every runtime ships every feature: no guide badge restricts a feature to some "
             "languages, and no 'not yet' note stands in for a missing one",
        params={"disparities": q},
        links={"admits": ["all-runtimes-same-features"]},
        payload={"note": q.source},
    )


# --- the Java port: three forced choices, and what each cost -----------------------------

_JAVA_DECISION = Node(
    id="java-port-mechanisms",
    kind="decision",
    name="Java reaches parity with a reflective proxy at the boundary, a hand-rolled JSON codec, "
         "and variable-level tracing by rewriting sources with javac's own parser and compiling "
         "them to memory in-process",
    meta={"amended": "5d3affa924a7 tightened under the 600-word budget; every "
                     "mechanism, cost and rejection kept"},
    payload={
        "rationale":
            "Three mechanisms, each forced by the language. (1) THE BOUNDARY. Java patches a "
            "loaded class only through -javaagent — a launch flag, and a library has no business "
            "dictating the command line that starts someone's app. So the boundary is the object "
            "the app holds: java.lang.reflect.Proxy over an interface, as Node and .NET do. "
            "(2) JSON. The platform ships none and this library ships no dependencies — every "
            "jar a recorder drags in is a version conflict in a codebase it was supposed to "
            "observe silently. "
            "What is needed is not a general parser but a codec with two disciplines the general "
            "ones get wrong: integer-vs-float preserved on the way in (the checker can reject "
            "`seq: 1.0`) and comparison by canonical form (30 equals 30.0 across the "
            "file/live-object divide) — the call .NET already made in Json.cs. (3) TRACING, the "
            "hard one. The JVM has no per-line hook: JDI, a bytecode agent, or a source "
            "rewriter. JDI fails as Delve did for Go — out-of-process, a round trip per "
            "variable per line, values as the DEBUGGER's renderings where trace v2 wants data "
            "an invariant can do arithmetic on. A bytecode agent needs -javaagent and reads "
            "locals by slot. What is left is .NET's road, and the JDK ships the parts: "
            "com.sun.source is javac's own parser and position table, so the rewriter is "
            "stdlib-only and the traced copy compiles and runs IN PROCESS — sharing this jar, "
            "the hook statics, the tape.",
        "consequence":
            "Second runtime to trace in-process (with .NET), first with no third-party "
            "compiler. Costs, plainly: tracing needs a JDK, not a JRE — JRE-only loses Tracer "
            "and nothing else. The ambient is an InheritableThreadLocal, which does NOT follow "
            "pooled-executor work: fan-outs need Recorder.propagate — weaker than .NET's "
            "AsyncLocal, documented at the point of use; the failure mode is silent "
            "under-recording. Definite assignment is approximated: javac exposes no "
            "AnalyzeDataFlow, so the rewriter tracks scope syntactically as Go's does, "
            "observing a local only after an initialised declaration — it may miss a variable, "
            "it can never emit one javac rejects, and a traced copy that does not compile is "
            "no trace at all.",
    },
    children=[
        Node(id="alt-java-jdi-tracing", kind="alternative",
             meta={"amended": "3e1c1d4ba97a tightened with its entry; claim kept"},
             name="Drive variable tracing through JDI/JDWP, the debugger protocol",
             payload={"why":
                      "Node's V8-Inspector analogue and Go's Delve trap: a separate debug-agent "
                      "process, a round trip per variable per line, and the debugger's truncated "
                      "strings — silently demoting trace v2 back to v1 reprs, the regression "
                      "both readers refuse outright."}),
        Node(id="alt-java-bytecode-agent", kind="alternative",
             meta={"amended": "ba1b66eec888 tightened with its entry; claim kept"},
             name="Instrument bytecode with a java.lang.instrument agent and ASM",
             payload={"why":
                      "The most powerful option — the local variable table even solves "
                      "definite assignment. But it needs -javaagent, so a test cannot begin a "
                      "traced run from inside itself, and it reads locals by slot, so names "
                      "survive only under the consumer's -g."}),
        Node(id="alt-java-json-dependency", kind="alternative",
             meta={"amended": "0789f7b9e25a tightened with its entry; claim kept"},
             name="Depend on Jackson or Gson instead of hand-rolling the codec",
             payload={"why":
                      "A recorder is installed into an app that did not ask for it, and Jackson "
                      "is among the most version-conflicted jars on the JVM — the instrument "
                      "would cause the breakages it exists to explain. And the two behaviours "
                      "actually needed would have to be built on top regardless."}),
        Node(id="alt-java-explicit-context", kind="alternative",
             meta={"amended": "e89224c42c74 tightened with its entry; claim kept"},
             name="Thread an explicit context parameter through every boundary call, as Go does",
             payload={"why":
                      "What Go had to do, having no ambient at all. Java has one, and forcing a "
                      "context parameter through every signature redesigns the caller's API — "
                      "the promise is that a recorded run looks like an unrecorded one. The "
                      "pooled-executor risk is met with Recorder.propagate and a note in the "
                      "guide."}),
    ],
)


_PYPI_NAME_DECISION = Node(
    id="pypi-distribution-name",
    kind="decision",
    name="The Python distribution is published as `xag-flight-recorder`, carrying npm's scope "
         "down as a prefix, while the import name stays `flight_recorder` on every runtime",
    payload={
        "rationale":
            "The bare name was not available and never will be: PyPI normalises a distribution "
            "name by collapsing separators, so `flight-recorder` and `flightrecorder` are the same "
            "name, and `flightrecorder` was taken in 2014 by an unrelated project (Tom Payne, "
            "utilities for paragliding GPS loggers, last release 2014-06-16). The upload is "
            "rejected at the registry with a 400, not a warning. npm had already answered the same "
            "question by publishing under the `@xag` scope — the unscoped `flight-recorder` there "
            "is one of npm's reserved placeholders — so the estate already had a namespace, and "
            "PyPI, which has no scopes, takes it as a prefix. What is deliberately NOT renamed is "
            "the import: `import flight_recorder` is what the guide teaches in five runtimes, and "
            "a registry's namespace collision is a distribution fact that has no business reaching "
            "into the source of a program.",
        "consequence":
            "The install line and the import line disagree — `pip install xag-flight-recorder` "
            "then `import flight_recorder` — which is a real papercut, mitigated only by being the "
            "same shape npm users already see. The guide's install block is the one place that "
            "must say the distribution name, so it is the one place that can drift. The prefix is "
            "also now load-bearing for the estate: a second Python package from here inherits it "
            "by precedent rather than by rule.",
    },
    children=[
        Node(id="alt-pypi-claim-flightrecorder", kind="alternative",
             name="File a PEP 541 name-transfer request to claim `flightrecorder`",
             payload={"why":
                      "The name has been dormant eleven years, which is the case PEP 541 exists "
                      "for. Rejected as the path, not as impossible: it has five real releases and "
                      "a living author, so it is abandonment rather than squatting and the outcome "
                      "is genuinely uncertain; the request takes weeks of a volunteer's attention; "
                      "and nothing ships in the meantime. Taking someone's name is also a poor "
                      "trade for a prefix that costs nine characters."}),
        Node(id="alt-pypi-rename-import", kind="alternative",
             name="Rename the Python package itself to `xag_flight_recorder` so install and import "
                  "agree",
             payload={"why":
                      "Removes the papercut and pays for it in the wrong currency. The import name "
                      "is the one identifier shared verbatim across all six runtimes, and it is "
                      "quoted throughout the guide, the spec, and every tape's own tooling; "
                      "bending it to fit one registry's namespace would privilege that registry's "
                      "accident over the cross-language symmetry the docs ledger's other rules "
                      "exist to protect."}),
        Node(id="alt-pypi-new-name", kind="alternative",
             name="Coin a fresh unclaimed name (`flight-tape`, `flightrec`) and use it everywhere",
             payload={"why":
                      "Both were verified free on PyPI and npm, so this was available. Rejected "
                      "because the project is already published as @xag/flight-recorder, lives at "
                      "github.com/xag/flight-recorder, and is called the flight recorder in every "
                      "document that describes the practice — a rename to dodge one registry would "
                      "cost the identity everywhere to buy consistency in one place."}),
    ],
)


# --- the PHP port: what the language forced, and what it made free -----------------------

_PHP_DECISION = Node(
    id="php-port-mechanisms",
    kind="decision",
    name="PHP reaches parity: a __call decorator at the boundary, two JSON defaults "
         "overridden, and tracing by source rewrite with PHP's own tokenizer, included "
         "in-process",
    meta={"amended": "c8854134b1d2 tightened under the 600-word budget; every "
                     "mechanism, cost and rejection kept"},
    payload={
        "rationale":
            "Three forced choices, one only PHP faces. "
            "(1) THE BOUNDARY. Repointing a PHP function needs runkit or uopz, and a library "
            "needing an extension in someone's php.ini has dictated their deployment. So the "
            "boundary is the OBJECT, as in the other five runtimes, and PHP makes it "
            "cheapest: __call intercepts undefined methods — no interface, no code "
            "generation. "
            "(2) JSON. PHP ships a codec, but the two disciplines the other ports hand-rolled "
            "still had to be CHOSEN — the defaults get both wrong: json_encode(1.0) is '1' "
            "(JSON_PRESERVE_ZERO_FRACTION; a float seq must still fail the checker), and "
            "serialize_precision = -1, the shortest exactly-reversible float that makes a PHP "
            "tape compare equal to another runtime's — an ini setting a host can change, so "
            "the suite ASSERTS it. "
            "(3) TRACING. Xdebug is an extension, not a library's to require; a tick handler "
            "cannot read the triggering frame's locals — a profiler, not a trace. That leaves "
            "rewriting, where .NET, Go and Java already are: token_get_all is the engine's "
            "own lexer, in core, and the copy is included in-process, sharing this package, "
            "the boundary and the tape. "
            "(4) THE EMPTY ARRAY, PHP's own: one array type is both sequence and map, so an "
            "empty one is ambiguous, and the tape distinguishes (fx.kwargs an object, fx.args "
            "an array). The encoder follows PHP's convention — array_is_list([]) is true, so "
            "a JSON array; where the tape REQUIRES an object, an explicit empty stdClass. "
            "Guessing 'map' silently turns every empty list an app returns into an object.",
        "consequence":
            "The costs, plainly. __call satisfies no type declaration, so typed parameters "
            "need unwrap() — Java's proxy IS the interface, no gap. A traced class must not "
            "already be loaded (no class-loader isolation to hide a second definition); the "
            "tracer says so instead of letting a redeclaration fatal, and the suite keeps "
            "its subject in a namespace no PSR-4 rule maps. A sink runs on the triggering "
            "request — weaker than Python's queue or Node's waitUntil, documented at the "
            "point of use. "
            "AND ONE PLACE PHP IS EASIER, worth a line after four runtimes of the opposite: "
            "get_defined_vars() returns every local, so the rewriter never names a variable — "
            "the definite-assignment problem the other ports solve or approximate has no "
            "counterpart here.",
    },
    children=[
        Node(id="alt-php-xdebug-tracing", kind="alternative",
             meta={"amended": "22e85c14c3de tightened with its entry; claim kept"},
             name="Drive variable tracing through Xdebug, which has per-line hooks and full "
                  "locals access",
             payload={"why":
                      "The -javaagent mistake in another costume: an extension the host must "
                      "enable in php.ini, which a library cannot require — a tracer nobody can "
                      "switch on is not a tracer. And a debugger's renderings, where trace v2 "
                      "wants data invariants can do arithmetic on."}),
        Node(id="alt-php-tick-functions", kind="alternative",
             meta={"amended": "802d974d40df tightened with its entry; claim kept"},
             name="Use declare(ticks=1) with register_tick_function for a per-statement hook",
             payload={"why":
                      "Needs no extension — its only virtue: a tick handler cannot read the "
                      "triggering frame's locals, so it reports a statement ran and nothing "
                      "more. And declare() in every traced file: rewriting's cost, a "
                      "profiler's yield."}),
        Node(id="alt-php-parser-dependency", kind="alternative",
             meta={"amended": "ff5b27319ed2 tightened with its entry; claim kept"},
             name="Rewrite with nikic/php-parser, a real AST rather than a token stream",
             payload={"why":
                      "A better parser, and a runtime dependency — the same reason Java "
                      "hand-rolled JSON over Jackson. token_get_all is core, and the rewriter "
                      "needs statement boundaries and enclosing bodies: a token-stream "
                      "question, not an AST one."}),
        Node(id="alt-php-empty-array-is-map", kind="alternative",
             meta={"amended": "e4d506adfbf7 tightened with its entry; claim kept"},
             name="Encode an empty PHP array as an object, since object positions most often "
                  "turn up empty",
             payload={"why":
                      "Fixes the recorder's own empty-kwargs case and silently breaks every "
                      "app's empty-list case. The recorder knows which of ITS positions are "
                      "objects (explicit stdClass); it cannot know that of a handed value. "
                      "Guess where you have knowledge, not where the caller does."}),
    ],
)


# --- distribution: what the guide promises vs what a registry will actually serve -------

# The audited state of every shipped runtime's package, as of 2026-07-19. `status` is the
# claim; the guide's install block is checked against it below. A runtime moves to
# "published" here only after its release is real on the registry — a name reservation is
# not a release, which is exactly the trap .NET fell into (a 0.0.0 placeholder sitting under
# the very id the guide told people to install).
_DISTRIBUTIONS = {
    "py":   {"registry": "PyPI",       "id": "xag-flight-recorder",
             "status": "published",   "version": "0.8.0"},
    "js":   {"registry": "npm",        "id": "@xag/flight-recorder",
             "status": "published",   "version": "0.10.2"},
    "go":   {"registry": "Go modules", "id": "github.com/xag/flight-recorder/go",
             "status": "published",   "version": "v0.8.0"},
    "cs":   {"registry": "NuGet",      "id": "flight-recorder",
             "status": "published",   "version": "0.1.0"},
    "java": {"registry": "Maven Central", "id": "io.github.xag:flight-recorder",
             "status": "published",   "version": "0.1.0"},
    "php":  {"registry": "Packagist",  "id": "poietic/flight-recorder",
             "status": "published",   "version": "v0.1.0"},
}


def _install_block() -> str:
    """The Install section of the guide - from its heading to the next one."""
    guide = _GUIDE.read_text(encoding="utf-8") if _GUIDE.exists() else ""
    m = re.search(r'<h2 id="install">.*?(?=<h2 )', guide, re.S)
    return m.group(0) if m else ""


def _install_claims_match_reality() -> Node:
    block = _install_block()
    # Every install snippet in the block, with the attributes it carries.
    advertised = {tab: attrs for tab, attrs in
                  re.findall(r'<pre data-lang="(\w+)"([^>]*)>', block)}

    findings = []
    for tab, dist in _DISTRIBUTIONS.items():
        if tab not in advertised:
            findings.append(f"{dist['registry']}: the guide shows no install snippet for {tab}")
            continue
        flagged = 'data-status="unpublished"' in advertised[tab]
        if dist["status"] == "unpublished" and not flagged:
            findings.append(
                f"{dist['registry']}: the guide advertises {dist['id']} as installable, but it is "
                f"not published there - mark the snippet data-status=\"unpublished\", or ship it")
        elif dist["status"] == "published" and flagged:
            findings.append(
                f"{dist['registry']}: {dist['id']} {dist['version']} IS published, but the guide "
                f"still warns readers away from it - drop the data-status attribute")

    # A runtime the guide shows but the manifest has never heard of: unaudited, so unproven.
    for tab in advertised:
        if tab not in _DISTRIBUTIONS:
            findings.append(f"the guide has an install snippet for {tab}, which no entry in "
                            f"_DISTRIBUTIONS accounts for - audit its registry and record it")

    if not findings:
        live = [f"{d['registry']} {d['version']}" for d in _DISTRIBUTIONS.values()
                if d["status"] == "published"]
        pending = [d["registry"] for d in _DISTRIBUTIONS.values()
                   if d["status"] == "unpublished"]
        q = Quantity(
            value=0, unit="finding", provenance="measured", grounded=True,
            source=f"install block agrees with the audited manifest: live on {', '.join(live)}"
                   + (f"; named as pending on {', '.join(pending)}" if pending else ""))
    else:
        q = Quantity(value=len(findings), unit="finding", provenance="measured", grounded=False,
                     source="; ".join(findings))

    return Node(
        id="install-claims-match-registries",
        kind="gate",
        name="Every install command the guide prints either works, or says plainly that it does "
             "not yet - no runtime is advertised as installable before its package is real",
        params={"mismatches": q},
        # Same fitting as the other gates: the gate admits its own measurement, so an
        # ungrounded quantity (a mismatch the scan could not clear) turns it red. Without
        # this link the gate admits nothing, and a gate that admits nothing can never fail.
        links={"admits": ["install-claims-match-registries"]},
        payload={"note":
                 "The guide told readers to `dotnet add package flight-recorder`. That command "
                 "succeeded and installed a 0.0.0 name-reservation stub containing no code - the "
                 "worst kind of wrong, because it does not fail. Maven Central and Packagist "
                 "simply 404'd. Six runtimes at feature parity shipped as three. Discharge this "
                 "by publishing the package and flipping its manifest entry - never by deleting "
                 "the warning from the guide."},
    )


_DISTRIBUTION_DECISION = Node(
    id="distribution-parity-is-checked-offline",
    kind="decision",
    name="Distribution parity is a gate over a hand-audited manifest checked against the guide's "
         "own install block, not a live query against six package registries",
    payload={
        "rationale":
            "Feature parity had a gate; distribution parity had nothing, and the gap was not "
            "theoretical: the guide advertised installs for .NET, Java and PHP that no registry "
            "would honour, and the .NET one resolved to a placeholder rather than failing. So the "
            "claim needs teeth. But the obvious implementation - ask NuGet, Maven Central, "
            "Packagist, npm, PyPI and the Go proxy on every run - would make ledger.check require "
            "the network, fail on a plane, and go red on someone else's outage; a rule that cries "
            "wolf is one people learn to skip, and this ledger's whole premise is that a red gate "
            "means something. The manifest splits the difference: a human audits the registries, "
            "records what they saw, and the gate enforces the thing that actually drifted - the "
            "DOC disagreeing with what was shipped. The audit is the expensive part and it is "
            "rare (it changes when you publish); the disagreement is the frequent part and it is "
            "now mechanical.",
        "consequence":
            "The gate is only as honest as its last audit: a stale manifest claiming something is "
            "published keeps the guide's install line unmarked and the check green. That is a "
            "real hole, bounded by the fact that publishing is the only thing that changes an "
            "entry, and whoever publishes is the person editing it. The cost is one more place to "
            "touch at release time; the benefit is that the check stays deterministic and "
            "offline, like every other gate here.",
    },
    children=[
        Node(id="alt-dist-live-registry-query", kind="alternative",
             name="Query each registry's API during the check and ground the gate on the response",
             payload={"why":
                      "Strictly more truthful, and unusable as a gate: six network calls on every "
                      "run, so the check fails offline and flakes on any registry's bad day. It "
                      "would also be measuring someone else's uptime and reporting it as this "
                      "repo's doc being wrong. Kept as an idea for a scheduled CI job, where a "
                      "red is a signal to re-audit rather than a blocked commit."}),
        Node(id="alt-dist-trust-the-guide", kind="alternative",
             name="Treat the guide's install block as the source of truth and check nothing",
             payload={"why":
                      "The status quo that produced the bug. A doc is a claim, and an unchecked "
                      "claim about a system that changes underneath it (a package published, a "
                      "name reserved, a runtime added) drifts silently - the same argument the "
                      "no-doc-duplication gate already won."}),
        Node(id="alt-dist-drop-unshipped-runtimes", kind="alternative",
             name="Delete the install snippets for runtimes that are not published yet",
             payload={"why":
                      "It would make the guide true, and it would hide the state of the project "
                      "from the person best placed to care - a reader evaluating whether the .NET "
                      "port exists at all. The snippet plus an honest 'not yet' says more than "
                      "silence, and it leaves something for the gate to hold to account: silence "
                      "cannot go red."}),
    ],
)


# --- fixture parity: the six tapes tell the same story -----------------------------------

_FIXTURES = _ROOT / "spec" / "fixtures"


def _fixture_kinds() -> dict:
    """Which event kinds each runtime's fixtures actually exercise."""
    import json

    out: dict[str, set] = {}
    if not _FIXTURES.exists():
        return out
    for p in sorted(_FIXTURES.glob("*.jsonl")):
        runtime = p.name.split("-", 1)[0]
        kinds = out.setdefault(runtime, set())
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            for e in obj.get("events") or []:
                if isinstance(e, dict) and e.get("k"):
                    kinds.add(e["k"])
    return out


def _fixture_parity() -> Node:
    """Every runtime's fixtures exercise the same event kinds.

    The gate that catches what the badge scan structurally cannot. A feature can be missing from
    a runtime without any badge saying so and without any note admitting it — Node shipped for
    five releases with no chained-client support at all, so it could not emit a `db` event, and
    nothing anywhere went red. The guide never claimed per-runtime `db` support, so there was no
    claim to falsify; the absence was invisible precisely because it was total.

    Fixtures cannot hide it. A tape either carries a `db` event or it does not, and the whole
    point of six runtimes recording one scenario is that the six tapes should differ only in the
    runtime key and the timestamps. So: compare the event kinds, and go red on a runtime that
    cannot produce one its peers can.
    """
    kinds = _fixture_kinds()
    runtimes = sorted(kinds)
    union: set = set()
    for ks in kinds.values():
        union |= ks

    missing = {r: sorted(union - kinds[r]) for r in runtimes if union - kinds[r]}

    if runtimes and not missing:
        q = Quantity(
            value=0, unit="disparity", provenance="measured", grounded=True,
            source=f"all {len(runtimes)} runtimes' fixtures exercise the same event kinds "
                   f"({', '.join(sorted(union))})")
    elif not runtimes:
        q = Quantity(
            value=1, unit="disparity", provenance="measured", grounded=False,
            source="no fixtures found — the sweep would pass vacuously, which is not the same "
                   "thing as passing")
    else:
        detail = "; ".join(f"{r} emits no {', '.join(ks)}" for r, ks in sorted(missing.items()))
        q = Quantity(
            value=len(missing), unit="disparity", provenance="measured", grounded=False,
            source="a runtime cannot record an event kind its peers can: " + detail
                   + " — implement the primitive, then record the canonical scenario again")

    return Node(
        id="all-runtimes-record-the-same-kinds",
        kind="gate",
        name="Every runtime's fixtures exercise the same event kinds: no implementation is "
             "missing a door the others have",
        params={"disparities": q},
        links={"admits": ["all-runtimes-record-the-same-kinds"]},
        payload={"note":
                 "Discharge this by implementing the missing primitive in the runtime that "
                 "lacks it and regenerating its fixtures - never by trimming the canonical "
                 "scenario until every runtime can record it."},
    )


_CANONICAL_DECISION = Node(
    id="one-canonical-fixture-scenario",
    kind="decision",
    name="All six runtimes record ONE canonical scenario into spec/fixtures, and each suite "
         "checks the others' tapes render character for character alike",
    meta={"amended": "f7370dd8072d tightened under the 600-word budget; every "
                     "finding kept, including the leak and the remaining disparity"},
    payload={
        "rationale":
            "The fixtures existed to prove the tape is one format, and they proved it - while "
            "quietly splitting into two families: Python and .NET recorded a chained read, an "
            "email user and a 'kaput' failure; Node, Go, Java and PHP an effect, a named user "
            "and 'no such key: alice'. Both were conformant, so nothing was red, and Java's Toy "
            "javadoc asserted the property the fixtures no longer had (differ only in runtime "
            "key and timestamps) - an unchecked claim drifting exactly as the "
            "no-doc-duplication gate predicted. The scenario is now one shape, the UNION not "
            "the intersection: canonical `enrol` keeps family B's naming and takes family A's "
            "chained read, because a `db` event enclosed by a span is the enclosure a reader "
            "most wants and an fx-only span never demonstrates it. Converging on the simpler "
            "family would have bought agreement by deleting coverage from the evidence every "
            "suite trusts.",
        "consequence":
            "The cost was not the fixture edit: porting one scenario to six runtimes meant "
            "IMPLEMENTING what a runtime could not express. Node could not express most of it - "
            "no chained client (query, queryOne, exec, snapshot), so no `db` event; no "
            "`sampleIndices`, so no `sample` draw. Nothing was red, because the parity gate "
            "reads the guide for badges, and a feature nobody claimed is a feature nobody can "
            "catch you lacking - the [^<.] hole's shape again: a check is evidence about what "
            "it can see. The fixture-parity gate closes it from the other side; a tape cannot "
            "omit a `db` event tactfully. Python was worse: no `perf` clock, and RandomShim "
            "passed every draw but `sample` through to the real random module UNRECORDED - not "
            "a missing feature, a nondeterminism LEAK: replay re-rolled what the tape never "
            "held. Both now shimmed. The uncomfortable finding: the lead implementation was "
            "the least complete, and only a fixture nobody could satisfy made it visible. One "
            "disparity remains, recorded: Python qualifies effect names with the defining "
            "module (`tests.canonical.store_set` vs `store.set`; the qualname has no prefix "
            "option), so the six tapes agree on structure, kinds and rendering - not yet byte "
            "for byte.",
    },
    children=[
        Node(id="alt-canon-converge-on-the-simpler-family", kind="alternative",
             name="Converge on the four-runtime family, changing only Python and .NET",
             payload={"why":
                      "Half the work and it loses the one thing the other family uniquely "
                      "proves: a chained read enclosed by a span. The fixtures are the evidence "
                      "every runtime's suite trusts, and buying convergence by deleting coverage "
                      "from the evidence is exactly backwards - it would make the tapes agree by "
                      "making them say less."}),
        Node(id="alt-canon-leave-the-families-and-correct-the-claim", kind="alternative",
             name="Leave both families and reword the comment that overclaims",
             payload={"why":
                      "Honest, cheap, and it would have left Node's missing db and sample "
                      "undiscovered - they surfaced only because porting one scenario to six "
                      "runtimes forced each one to actually express it. A scenario that every "
                      "runtime must record is a parity test the badge scan cannot be; softening "
                      "the claim would have retired the only instrument that found the gap."}),
        Node(id="alt-canon-live-registry-of-features", kind="alternative",
             name="Declare a per-runtime feature matrix in the ledger and gate on that",
             payload={"why":
                      "A hand-maintained matrix is a claim about the code, and this ledger's "
                      "whole premise is that claims drift while measurements do not. The matrix "
                      "would have said Node supports db for as long as somebody believed it. The "
                      "fixtures are already the measurement; gate on those."}),
    ],
)


# --- the deck: the one doc surface nothing was reading ------------------------------------

_SLIDES = _ROOT / "docs" / "slides.html"

_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
                6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _slides_name_every_runtime() -> Node:
    raw = _SLIDES.read_text(encoding="utf-8") if _SLIDES.exists() else ""
    n = len(_RUNTIME_TABS)
    # Read the deck as a reader does: tags stripped, entities left alone. The claims below
    # are split across <em> spans ("written <em>six times</em>"), so matching the markup
    # would silently match nothing — a gate that scans the wrong text is a gate that passes.
    deck = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw))

    # Not \b: a word boundary before "." requires a word character before it, so \b\.NET\b
    # can never match ".NET" written after a space. These bounds mean "not glued to more
    # name-ish characters", which is what was meant.
    unnamed = [name for name in _RUNTIME_TABS
               if not re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", deck)]

    # A claim that counts implementations has exactly one right answer. "two languages" is
    # left alone deliberately: the deck contrasts a PAIR in detail, and saying so is true.
    # What cannot be true is miscounting the implementations themselves.
    miscounts = [f"'{m} implementations' but there are {n}"
                 for m in re.findall(r"\b(\w+) implementations\b", deck)
                 if m.lower() in _COUNT_WORDS.values() and m.lower() != _COUNT_WORDS[n]]
    # Same for the checker-written-N-times claim, which drifted the same way. Scoped to the
    # CHECKER: "the analysis is written once, for every language" is a different sentence
    # making a true point, and an earlier draft of this gate went red on it.
    miscounts += [f"checker 'written {m}' but there are {n} implementations"
                  for m in re.findall(r"checker written ([a-z]+(?: times)?|twice|once)", deck)
                  if m.lower() != f"{_COUNT_WORDS[n]} times"]

    problems = []
    if unnamed:
        problems.append("the deck never names shipped runtime(s): " + ", ".join(unnamed))
    if miscounts:
        problems.append("; ".join(miscounts))

    if not problems:
        q = Quantity(
            value=0, unit="finding", provenance="measured", grounded=True,
            source=f"docs/slides.html names all {n} shipped runtimes "
                   f"({', '.join(_RUNTIME_TABS)}) and counts them correctly")
    else:
        q = Quantity(
            value=len(unnamed) + len(miscounts), unit="finding", provenance="measured",
            grounded=False,
            source="; ".join(problems) + " — the deck may contrast two runtimes in detail, "
                   "but it may not claim the project is smaller than it is")

    return Node(
        id="slides-count-every-runtime",
        kind="gate",
        name="The deck names every shipped runtime and counts them correctly — it may show a "
             "pair in detail, it may not present that pair as the whole project",
        params={"stale_claims": q},
        links={"admits": ["slides-count-every-runtime"]},
        payload={"note":
                 "docs/slides.html sat at 'Two runtimes / One tape, two languages' for a week "
                 "after Java and PHP shipped, claiming a checker 'written twice' when six exist. "
                 "Nothing caught it: every doc gate here read docs/index.html or a README, and "
                 "the deck is neither. An unread doc is an unchecked claim, and it drifts."},
    )


_SLIDES_DECISION = Node(
    id="the-deck-is-a-doc",
    kind="decision",
    name="The slide deck is held to the same counting rule as the guide, but is allowed to "
         "teach with two runtimes rather than exhibit all six",
    payload={
        "rationale":
            "Adding Java and PHP updated the guide, the READMEs and the ledger, and left the deck "
            "saying 'Two implementations'. It drifted for a week in public because no gate read "
            "it — every doc rule here scans docs/index.html or a README, and slides.html is "
            "neither. The obvious repair was to widen the comparison table to six columns and be "
            "done. Rejected: that table is the argument, not a reference — five rows contrasting "
            "how each language FORCES a different mechanism, and the force of it comes from being "
            "able to hold two designs in your head at once. Six columns of dense text is a table "
            "nobody reads from the back of a room, and a slide that cannot be read has lost more "
            "than it gained in completeness. So the gate polices the CLAIM, not the layout: name "
            "every runtime, count them correctly, and show whichever pair teaches best.",
        "consequence":
            "The deck can stay legible while runtime seven arrives, and the check will still go "
            "red until the deck admits that seven exists. The cost is that the comparison table "
            "will keep showing Python and Node while other runtimes make choices just as "
            "interesting — a real loss, priced deliberately, and the note above the table now "
            "says out loud that it is a sample rather than the set.",
    },
    children=[
        Node(id="alt-slides-six-columns", kind="alternative",
             name="Widen the comparison table to one column per runtime",
             payload={"why":
                      "Complete, and unreadable: six columns times five rows is thirty cells of "
                      "prose at projection size. It also flattens the point — the rows exist to "
                      "show that a difference was FORCED, which lands as a contrast between two "
                      "and dissolves into a matrix at six."}),
        Node(id="alt-slides-drop-the-slide", kind="alternative",
             name="Delete the slide rather than maintain it",
             payload={"why":
                      "It is the only place the talk shows that the tape is a real standard and "
                      "not one library's file format — the whole 'implementations are welcome' "
                      "claim rests on it. Deleting the evidence to avoid maintaining it is the "
                      "same move as deleting a failing test."}),
        Node(id="alt-slides-ungated", kind="alternative",
             name="Fix the wording now and leave the deck unchecked, as before",
             payload={"why":
                      "This is what was already in place, and it is exactly how the deck got a "
                      "week out of date. The wording is fixed either way; the gate is what makes "
                      "the fix hold when the seventh runtime lands and the author is in a hurry."}),
    ],
)
