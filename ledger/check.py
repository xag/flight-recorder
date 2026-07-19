"""Run the docs ledger's rules. `uv run --group ledger python -m ledger.check`

Exit 1 while any rule is red. The gates measure the real tree at build time, so a per-language
README that grows a walkthrough, or a runtime that ships without a guide tab, turns red here and
cannot be made green by editing this file — only by fixing the docs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from quern import get_node, run_rules
from quern.roll import audit, write

from .tree import build


_ROOT = Path(__file__).resolve().parents[1]
_ROLL = "ledger/roll.json"

# WHICH revision's roll to compare against, and it is not a detail. Locally the
# working tree holds the edit under judgement and HEAD is the last good state, so
# HEAD is right. In CI the commit under judgement IS HEAD - and carries the roll
# written beside it - so comparing against HEAD compares the tree with itself and
# passes whatever it is handed. CI names the base it is diffing from instead.
_REV = os.environ.get("LEDGER_ROLL_REV", "HEAD")


def main() -> int:
    tree = build()
    results = run_rules(tree)
    red = [r for r in results if not r.ok]
    # A tombstone with no `was` excuses nothing - the right way round, because
    # forgetting it leaves the check red, never green.
    excused = {n.payload["was"] for _, n in tree.walk("")
               if n.kind == "tombstone" and n.payload.get("was")}
    removals, looked = audit(tree, _ROOT, _ROLL, _REV, excused)

    # ASCII only: cp1252 consoles mangle anything prettier.
    for r in sorted(results, key=lambda r: (r.ok, r.rule, r.node)):
        mark = "ok  " if r.ok else "RED "
        at = f" @ {r.node}" if r.node else ""
        detail = f" - {r.detail}" if r.detail else ""
        print(f"{mark}{r.rule}{at}{detail}")

    for line in removals:
        print(f"GONE {line}")
    if not looked:
        print(f"note: no roll at {_REV} - nothing was compared, so nothing was")
        print("      checked for removal. Honest on the first run of this check,")
        print("      and a problem on any other.")

    print()
    # The roll is written on a red run too, and that is deliberate. A red rule is a
    # debt carried on purpose - some of these ledgers ship red by decision - while
    # the roll only records WHAT EXISTS. Gating it on `not red` would deny a
    # permanently-red ledger the one protection it most needs. Only an unexplained
    # removal makes the roll unsafe to rewrite, because rewriting it then would
    # launder the very thing the check just caught.
    if not removals:
        write(tree, _ROOT / _ROLL)
    if not red and not removals:
        print(f"{len(results)} rule(s), all green; roll written.")
        return 0
    if red:
        print(f"{len(red)} of {len(results)} rule(s) RED.")
    if removals:
        print(f"{len(removals)} entr(y/ies) left the record without saying so.")
    for r in red:
        node = get_node(tree, r.node) if r.node else None
        why = (node.payload.get("note") if node else None) or r.detail or ""
        print(f"  {r.node or r.rule}: {why}")
    print("Discharge a red node by doing the work it names - never by editing the ledger.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
