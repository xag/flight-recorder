"""The docs ledger — the documentation architecture as data a check can go red on.

flight-recorder ships one library in three runtimes, and its docs have exactly one failure
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
from pathlib import Path

import quern.grounding  # noqa: F401 -- the grounding natives, for the gate rule
from quern import Node, Quantity, Quern

_ROOT = Path(__file__).resolve().parents[1]

# Directories that are not source: vendored deps, virtualenvs, build output.
_SKIP = {".git", "node_modules", ".venv", "bin", "obj", ".dotnet", "dist",
         "__pycache__", ".pytest_cache"}

# The runtimes flight-recorder ships, and the guide tab each must have. A runtime that ships
# without a tab here is a privileged-language violation by omission.
_RUNTIME_TABS = {"Python": "py", "Node": "js", ".NET": "cs"}

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
    quern.root.children = [_DECISION, _no_privileged_language(), _no_doc_duplication()]
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
