"""The docs ledger — the documentation architecture as data a check can go red on.

flight-recorder ships one library in five runtimes, and its docs have exactly one failure
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

# Directories that are not source: vendored deps, virtualenvs, build output.
_SKIP = {".git", "node_modules", ".venv", "bin", "obj", ".dotnet", "dist",
         "__pycache__", ".pytest_cache"}

# The runtimes flight-recorder ships, and the guide tab each must have. A runtime that ships
# without a tab here is a privileged-language violation by omission.
_RUNTIME_TABS = {"Python": "py", "Node": "js", ".NET": "cs", "Go": "go", "Java": "java"}

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
                           _PYPI_NAME_DECISION]
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


# --- feature parity: the five runtimes are one library -----------------------------------

_PARITY_DECISION = Node(
    id="feature-parity",
    kind="decision",
    name="Every runtime ships every feature. The five implementations are one library, not a lead "
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
            "A feature is not shipped until it is shipped in all five runtimes; the guide then "
            "gains a tab, never a badge. The disparity is the finding, not an accepted asterisk. "
            "This is the strictest gate in the ledger, and deliberately so — and it has now been "
            "paid once, which is the evidence it works. Variable-level tracing was the hard case: "
            "the gate held red on the reading that it needed a debugger backend where there is no "
            "sys.settrace, and that reading turned out to be WRONG. Neither .NET nor Go got a "
            "debugger. Both got a rewriter — Roslyn over the sources in .NET, go/ast over a copy "
            "of the module in Go — because the gate refused the footnote long enough for someone "
            "to look for the third option. A gap documented honestly would have shipped the "
            "asterisk and never found it. Java later took the same road a third time, with javac's "
            "own com.sun.source, which is now three runtimes the 'it needs a debugger' reading was "
            "wrong about. "
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
    payload={
        "rationale":
            "Three mechanisms had to be chosen, and each was forced by the language rather than "
            "preferred. (1) THE BOUNDARY. Java can patch a loaded class, but only through a "
            "-javaagent — a launch flag, and a library has no business dictating the command line "
            "that starts someone's app. So the boundary is the object the app holds: "
            "java.lang.reflect.Proxy over an interface, as Node and .NET already do. (2) JSON. "
            "Java ships none in the platform, and this library ships no dependencies, because a "
            "recorder is installed into someone else's app and every jar it drags in is a version "
            "conflict it can cause in a codebase it was supposed to observe silently. .NET made "
            "the same call in Json.cs even though it had System.Text.Json, because the thing "
            "actually needed is not a general parser: it is a codec with two disciplines the "
            "general ones get wrong — integer-vs-float preserved on the way in (so the checker can "
            "reject `seq: 1.0`), and comparison by canonical form (so 30 and 30.0 compare equal "
            "across the file/live-object divide). (3) TRACING, the hard one. The JVM exposes no "
            "per-line hook, so the choice was JDI, a bytecode agent, or a source rewriter. JDI was "
            "rejected for the reasons Go rejected Delve: an out-of-process debug agent, a socket "
            "round trip per variable per line, and values arriving as the DEBUGGER's renderings "
            "when trace version 2 exists precisely so values are data an invariant can do "
            "arithmetic on. A bytecode agent was rejected because it needs -javaagent (so a test "
            "cannot start a traced run from inside itself) and reads locals by slot, making a "
            "variable's NAME depend on the consumer having compiled with -g. What is left is "
            ".NET's road, and the JDK happens to ship the parts: com.sun.source is javac's own "
            "parser and position table, exported and supported, so the rewriter is stdlib-only and "
            "the traced copy compiles and runs IN PROCESS — sharing this jar, and therefore "
            "sharing the hook statics and the tape.",
        "consequence":
            "Java is the second runtime to trace in-process rather than out (with .NET), and the "
            "first to do it with no third-party compiler library. The costs, stated plainly: "
            "tracing needs a JDK at run time, not a JRE — a JRE-only deployment loses Tracer and "
            "nothing else. The ambient rides on an InheritableThreadLocal, which does NOT follow "
            "work handed to a pooled executor, so a fan-out needs Recorder.propagate; this is "
            "weaker than .NET's AsyncLocal and is documented at the point of use rather than "
            "hidden, because the failure mode is silent under-recording. And definite assignment "
            "had to be approximated: .NET asks Roslyn's own AnalyzeDataFlow, javac exposes no "
            "equivalent, so the rewriter tracks scope syntactically as Go's does and observes a "
            "local only from the statement after an initialised declaration. Conservative in the "
            "safe direction — it may miss a variable; it can never emit one javac would reject, "
            "and a traced copy that does not compile is not a degraded trace but no trace at all.",
    },
    children=[
        Node(id="alt-java-jdi-tracing", kind="alternative",
             name="Drive variable tracing through JDI/JDWP, the debugger protocol",
             payload={"why":
                      "The structural analogue of what Node does over the V8 Inspector, and the "
                      "same trap Go found with Delve: it needs the traced code launched under a "
                      "debug agent in a separate process, costs a round trip per variable per "
                      "line, and hands back the debugger's own truncated strings — which would "
                      "silently demote trace version 2 back to version 1's reprs, the exact "
                      "regression both readers now refuse outright."}),
        Node(id="alt-java-bytecode-agent", kind="alternative",
             name="Instrument bytecode with a java.lang.instrument agent and ASM",
             payload={"why":
                      "Genuinely the most powerful option, and it would sidestep definite "
                      "assignment entirely since the local variable table carries each slot's live "
                      "range. Rejected on two counts a library cannot pay: it requires -javaagent "
                      "on the launch command, so a test cannot begin a traced run once it is "
                      "already running; and it reads locals by slot, so a variable's name survives "
                      "only if the consumer compiled with -g — making the trace's usefulness "
                      "depend on someone else's build flags."}),
        Node(id="alt-java-json-dependency", kind="alternative",
             name="Depend on Jackson or Gson instead of hand-rolling the codec",
             payload={"why":
                      "Less code, and the wrong trade for this library. A recorder is installed "
                      "into an app that did not ask for it; Jackson is among the most "
                      "version-conflicted jars on the JVM, so the instrument would become a "
                      "cause of the breakages it exists to explain. It also would not give the "
                      "two behaviours actually needed — an integral/fractional distinction the "
                      "checker can reject on, and canonical comparison — both of which would have "
                      "to be built on top regardless."}),
        Node(id="alt-java-explicit-context", kind="alternative",
             name="Thread an explicit context parameter through every boundary call, as Go does",
             payload={"why":
                      "Honest across executors, and what Go had to do because it has no ambient at "
                      "all. Rejected for Java because Java DOES have one, and forcing a context "
                      "parameter through every signature is a change to the app's own API that the "
                      "recorder has no right to demand — the library's promise is that a recorded "
                      "run looks like an unrecorded one. The residual risk (a pooled executor "
                      "silently dropping events) is met with Recorder.propagate and a note in the "
                      "guide, not with a redesign of the caller's code."}),
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
