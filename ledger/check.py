"""Run the docs ledger's rules. `uv run --group ledger python -m ledger.check`

Exit 1 while any rule is red. The gates measure the real tree at build time, so a per-language
README that grows a walkthrough, or a runtime that ships without a guide tab, turns red here and
cannot be made green by editing this file — only by fixing the docs.
"""

from __future__ import annotations

import sys

from quern import get_node, run_rules

from .tree import build


def main() -> int:
    tree = build()
    results = run_rules(tree)
    red = [r for r in results if not r.ok]

    # ASCII only: cp1252 consoles mangle anything prettier.
    for r in sorted(results, key=lambda r: (r.ok, r.rule, r.node)):
        mark = "ok  " if r.ok else "RED "
        at = f" @ {r.node}" if r.node else ""
        detail = f" - {r.detail}" if r.detail else ""
        print(f"{mark}{r.rule}{at}{detail}")

    print()
    if not red:
        print(f"{len(results)} rule(s), all green.")
        return 0
    print(f"{len(red)} of {len(results)} rule(s) RED.")
    for r in red:
        node = get_node(tree, r.node) if r.node else None
        why = (node.payload.get("note") if node else None) or r.detail or ""
        print(f"  {r.node or r.rule}: {why}")
    print("Discharge a red node by doing the work it names - never by editing the ledger.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
