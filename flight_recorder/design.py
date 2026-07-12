"""Design invariants: claims about every render, asserted over the layout the browser computed.

A pinned screenshot is a **regression** oracle and can never be anything else. It asserts that the
page looks as it looked, and it can never say the way it looked was right — an ugly bug screenshots
as faithfully as a beautiful fix. It is also the wrong substrate: a bitmap holds no claim, names no
cause, and diffs into a coloured rectangle.

A design invariant is a **correctness** oracle. It is a claim about every render — every state,
every viewport, every theme — written once and checked against any render tape, so it can condemn
the very first observation of a layout nobody has ever looked at. Which is the whole point, because
nobody ever looks at the 320px dark-mode empty state.

    @design_invariant("every text is legible on the backdrop it actually paints on")
    def _(r: Render):
        for n in r.text_nodes():
            assert contrast(n.ink, n.backdrop) >= 4.5, f"{n.p}: {n.text!r}"

    report = check_design(render, standard_invariants())
    assert report.ok, format_design_report(report)

WHY THIS IS NOT A STYLESHEET LINTER

A linter reads `color: var(--mut)` on `background: var(--card)`, computes a contrast ratio, and is
confidently wrong: the card never painted because a media query dropped it, an ancestor is 55%
opaque, a fixed header sits over the top. The cascade is a computation, and the only thing that has
run it is the browser. So the claims here are asserted over the browser's ANSWER — the used value,
the painted backdrop, the box that resulted, the focus style under a real Tab.

The failure of an invariant is a claim about the design. The failure of the CAPTURE (fonts never
loaded, the page never reached its state) is a claim about the tape. They are different findings,
and a render that never settled has no trustworthy layout to assert over.

A tape is only data, so reading one needs no browser: this module has no dependencies, runs in CI
with nothing installed, and reads a tape from either runtime. Only the CAPTURE needs a browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# A violation is a violation: same shape, same fields, whether the claim was about a trajectory or
# about a layout. There is no second one of these.
from flight_recorder.invariants import Violation

# --- colour: the arithmetic the eye does ---------------------------------------------------


def _channel(v: float) -> float:
    c = v / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(color: str) -> tuple:
    s = color.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def luminance(color: str) -> float:
    """WCAG relative luminance of an opaque #rrggbb."""
    r, g, b = (_channel(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio. Both colours must already be composited to opaque — which the
    capture does, because the alpha and the ancestors' opacity are part of what the eye gets."""
    a, b = luminance(fg), luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


# --- the layout a claim sees ---------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One element, as the browser computed it."""

    p: str                       # a path that survives a re-render: what a claim pins to
    tag: str
    box: tuple                   # x, y, w, h — CSS px, in the viewport
    disp: str
    fs: float                    # used font-size, px (a clamp() has already been resolved)
    fw: int
    pad: tuple
    out: str                     # outline, unfocused
    shadow: str
    backdrop: str                # "#rrggbb" — or "image", meaning: do not trust a ratio here
    text: Optional[str] = None   # the text THIS element paints (not its children's)
    role: Optional[str] = None
    name: Optional[str] = None   # the accessible name, approximated
    act: bool = False            # operable
    hid: bool = False            # hidden from the accessibility tree
    col: Optional[str] = None    # the colour the stylesheet asked for
    ink: Optional[str] = None    # the colour the eye receives, after alpha and opacity
    alpha: Optional[float] = None
    ff: Optional[str] = None
    lh: Optional[float] = None
    gap: Optional[str] = None
    rad: Optional[float] = None
    ov: Optional[tuple] = None
    over: Optional[tuple] = None  # [dx, dy] the content exceeded its clipping box by
    ell: bool = False             # text-overflow: ellipsis — clipping that was ASKED for

    @classmethod
    def of(cls, d: dict) -> "Node":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def w(self) -> float:
        return self.box[2]

    @property
    def h(self) -> float:
        return self.box[3]

    @property
    def visible(self) -> bool:
        return self.w > 0 and self.h > 0

    @property
    def large_text(self) -> bool:
        """WCAG's threshold: large text is legible at a lower ratio."""
        return self.fs >= 24 or (self.fs >= 18.66 and self.fw >= 700)

    @property
    def parent(self) -> str:
        return self.p.rsplit(">", 1)[0] if ">" in self.p else ""

    def __repr__(self) -> str:
        t = f" {self.text!r}" if self.text else ""
        return f"<{self.p}{t}>"


@dataclass(frozen=True)
class Render:
    """One cell of the state matrix, rendered: a state, in a world, and what came out."""

    seq: int
    state: str
    viewport: tuple
    theme: str
    reduced_motion: bool
    url: Optional[str]
    doc: dict
    ambient: dict
    nodes: list
    focus: list = field(default_factory=list)   # in Tab order
    probe: bool = False                         # a MUTATED render: never a regression pin

    @property
    def cell(self) -> str:
        w, h = self.viewport
        # ASCII: this is printed to a terminal, and a Windows console is cp1252.
        tag = f"{self.state} @ {w}x{h} {self.theme}"
        return f"{tag} [probe]" if self.probe else tag

    def text_nodes(self) -> list:
        return [n for n in self.nodes if n.text and n.visible]

    def interactive(self) -> list:
        return [n for n in self.nodes if n.act and n.visible]

    def find(self, path: str) -> Optional[Node]:
        return next((n for n in self.nodes if n.p == path), None)


def load_renders(tape) -> list:
    """Every render on a tape. Tolerates a torn final line, as every tape reader must."""
    out = []
    with Path(tape).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # the process died mid-write; the spec requires discarding it
            if d.get("ev") != "call" or d.get("fn") != "render":
                continue
            k, res = d.get("kwargs", {}), d.get("result") or {}
            vp = k.get("viewport") or [0, 0]
            out.append(
                Render(
                    seq=d.get("seq", 0),
                    state=k.get("state", "?"),
                    viewport=(vp[0], vp[1]),
                    theme=k.get("theme", "light"),
                    reduced_motion=bool(k.get("reducedMotion")),
                    url=k.get("url"),
                    doc=res.get("doc", {}),
                    ambient=res.get("ambient", {}),
                    nodes=[Node.of(n) for n in res.get("nodes", [])],
                    focus=res.get("focus", []) or [],
                    probe=bool(d.get("probe")),
                )
            )
    return out


# --- declaring claims ----------------------------------------------------------------------


@dataclass(frozen=True)
class DesignInvariant:
    description: str
    check: Callable[[Render], None]

    def __call__(self, r: Render) -> None:
        self.check(r)


def design_invariant(description: str) -> Callable[[Callable[[Render], None]], DesignInvariant]:
    """Declare a claim about every render. The body asserts; the description is what a failure is
    reported as, so write it as the property, not as the check."""

    def wrap(fn: Callable[[Render], None]) -> DesignInvariant:
        return DesignInvariant(description=description, check=fn)

    return wrap


def collect(source: Any) -> list:
    """Every DesignInvariant in a module, or listed in a sequence."""
    if isinstance(source, DesignInvariant):
        return [source]
    if isinstance(source, (list, tuple, set)):
        for i in source:
            if not isinstance(i, DesignInvariant):
                raise TypeError(f'{i!r} is not a DesignInvariant — decorate it with @design_invariant("…")')
        return list(source)
    return [v for v in vars(source).values() if isinstance(v, DesignInvariant)]


@dataclass
class DesignReport:
    cell: str
    outcome: str  # held | violated | unsettled
    violations: list = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == "held"


def _message(e: AssertionError) -> str:
    """The claim's own message, without pytest's autopsy. Under pytest, assertions are rewritten
    and `str(e)` carries the message AND a dump of the failed expression — the same findings again,
    as a wrapped Python list. The report IS this library's product; it must read the same whether it
    was asked for from a test or from a terminal."""
    return str(e).split("\nassert ")[0].strip() or "assertion failed"


def check_design(render: Render, invariants: Any) -> DesignReport:
    """Assert every claim against one render.

    A render whose fonts never loaded, or whose body never laid out, is not evidence about the
    design — it is a broken capture. Asserting over it would be asserting over a fiction, so it is
    reported as `unsettled` and no claim is run: the tape, not the design, needs attention.
    """
    checks = collect(invariants)

    if render.ambient.get("fontsReady") not in (None, "loaded", "unknown"):
        return DesignReport(render.cell, "unsettled",
                            [Violation("the capture settled",
                                       f"document.fonts.status was {render.ambient['fontsReady']!r} — "
                                       "the layout was measured in a fallback font")])
    if not render.nodes:
        return DesignReport(render.cell, "unsettled",
                            [Violation("the capture settled", "no nodes: the page never rendered")])

    violations = []
    for inv in checks:
        try:
            inv(render)
        except AssertionError as e:
            violations.append(Violation(inv.description, _message(e)))
        except Exception as e:  # the claim is broken, not the design
            violations.append(Violation(inv.description, f"{type(e).__name__}: {e}", broke=True))

    return DesignReport(render.cell, "violated" if violations else "held", violations, len(checks))


def format_design_report(report: DesignReport) -> str:
    mark = {"held": "ok", "violated": "VIOLATED", "unsettled": "UNSETTLED"}[report.outcome]
    lines = [f"[{mark}] {report.cell}  ({report.checked} claims)"]
    for v in report.violations:
        lines.append(f"  x  {v.invariant}" + ("  (the claim itself broke)" if v.broke else ""))
        for ln in v.detail.splitlines():
            lines.append(f"      {ln}")
    return "\n".join(lines)


# --- the standard claims -------------------------------------------------------------------
#
# Not a style guide. Every one of these is FALSE OF A BROKEN PAGE and true of every good one,
# whatever its taste — and not one of them is decidable from the source.


def standard_invariants(*, min_contrast: float = 4.5, min_target: float = 24.0) -> list:
    """The claims that hold for any competent interface, at any viewport, in any theme."""

    @design_invariant("every text is legible on the backdrop it actually paints on")
    def _contrast(r: Render):
        bad = []
        for n in r.text_nodes():
            if n.hid or n.ink is None or n.backdrop == "image":
                continue
            need = 3.0 if n.large_text else min_contrast
            ratio = contrast(n.ink, n.backdrop)
            if ratio < need:
                fade = f", faded to {n.alpha:g} opacity" if n.alpha else ""
                bad.append(
                    f"{ratio:4.2f}:1 (needs {need}) — {n.ink} on {n.backdrop}, "
                    f"{n.fs:g}px/{n.fw}{fade}\n        {n.p}\n        {n.text!r}"
                )
        assert not bad, "\n      ".join(bad)

    @design_invariant("the page never scrolls sideways")
    def _no_h_overflow(r: Render):
        d = r.doc
        over = d.get("scrollWidth", 0) - d.get("clientWidth", 0)
        assert over <= 1, (
            f"{over}px of horizontal overflow at {r.viewport[0]}px "
            f"(scrollWidth {d.get('scrollWidth')} > clientWidth {d.get('clientWidth')})"
        )

    @design_invariant("no text is cut off by the box it sits in")
    def _no_clipping(r: Render):
        bad = [
            f"{n.p}: overflows by {n.over[0]}x{n.over[1]}px inside overflow:{n.ov[0]}/{n.ov[1]}"
            f"\n        {n.text!r}"
            for n in r.nodes
            if n.over and n.text and not n.ell and n.visible
        ]
        assert not bad, "\n      ".join(bad)

    @design_invariant(f"every target is at least {min_target:g}px on its short side")
    def _target_size(r: Render):
        bad = []
        for n in r.interactive():
            if n.hid:
                continue
            # WCAG exempts a link INSIDE a sentence: the target is the text, and the text is the
            # size it is. A standalone control has no such excuse.
            parent = r.find(n.parent)
            if n.disp.startswith("inline") and parent and parent.text:
                continue
            if min(n.w, n.h) < min_target:
                bad.append(f"{n.p}: {n.w:g}x{n.h:g}px  {(n.name or n.text or '')!r}")
        assert not bad, "\n      ".join(bad)

    @design_invariant("every focusable shows a focus ring under a real Tab")
    def _focus_ring(r: Render):
        if not r.focus:
            return
        bad = [
            f"tab stop {f['i']}: {f['p']}  (outline {f['out']!r}, box-shadow {f['shadow']!r})"
            for f in r.focus
            if not f.get("ring")
        ]
        assert not bad, "\n      ".join(bad)

    @design_invariant("everything clickable is reachable by keyboard")
    def _keyboard_reachable(r: Render):
        if not r.focus:
            return
        reached = {f["p"] for f in r.focus}
        bad = [f"{n.p}  {(n.name or n.text or '')!r}"
               for n in r.interactive() if not n.hid and n.p not in reached]
        assert not bad, "\n      ".join(bad)

    @design_invariant("every control says what it is")
    def _accessible_name(r: Render):
        bad = [f"{n.p} ({n.tag})"
               for n in r.interactive() if not n.hid and not (n.name or "").strip()]
        assert not bad, "\n      ".join(bad)

    return [_contrast, _no_h_overflow, _no_clipping, _target_size,
            _focus_ring, _keyboard_reachable, _accessible_name]


def token_invariants(tokens: dict) -> list:
    """The design system, as a rule over what actually painted.

    `{"colors": ["#16181c", …], "type": [13, 15, 16, …], "space": [4, 8, 12, …]}` — any key may be
    omitted, and only the claims you supply tokens for are made. A system that uses fluid type
    (`clamp()`) has consciously left the discrete type scale; do not declare `type` for it, rather
    than declaring it and living with the noise.

    This cannot be a stylesheet lint. `var(--acc)` conforms trivially in the source and can still
    paint a colour that is in no palette, because a translucent ancestor composited it into one.
    """
    out = []
    norm = lambda c: (c or "").lower()  # noqa: E731

    if tokens.get("colors"):
        palette = {norm(c) for c in tokens["colors"]}

        @design_invariant("every colour comes from the palette")
        def _palette(r: Render):
            bad = []
            for n in r.nodes:
                for what, c in (("text", n.col), ("backdrop", n.backdrop)):
                    if c and c != "image" and norm(c) not in palette:
                        bad.append(f"{n.p}: {what} {c} is not a token")
            assert not bad, "\n      ".join(sorted(set(bad)))

        out.append(_palette)

    if tokens.get("type"):
        scale = {float(v) for v in tokens["type"]}

        @design_invariant("every font size is on the type scale")
        def _type(r: Render):
            bad = sorted({f"{n.fs:g}px  {n.p}" for n in r.text_nodes() if n.fs not in scale})
            assert not bad, "\n      ".join(bad)

        out.append(_type)

    if tokens.get("space"):
        scale = {float(v) for v in tokens["space"]} | {0.0}

        @design_invariant("every space is on the spacing scale")
        def _space(r: Render):
            bad = {f"{v:g}px padding  {n.p}"
                   for n in r.nodes for v in n.pad if float(v) not in scale}
            assert not bad, "\n      ".join(sorted(bad))

        out.append(_space)

    return out


# --- the CLI -------------------------------------------------------------------------------


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="flight-design",
        description="Check design invariants over a render tape.",
    )
    ap.add_argument("tape", type=Path)
    ap.add_argument("--tokens", type=Path, help="JSON: {colors:[…], type:[…], space:[…]}")
    ap.add_argument("--min-contrast", type=float, default=4.5)
    ap.add_argument("--min-target", type=float, default=24.0)
    ap.add_argument("--quiet", action="store_true", help="only print cells that violated")
    a = ap.parse_args(argv)

    checks = standard_invariants(min_contrast=a.min_contrast, min_target=a.min_target)
    if a.tokens:
        checks += token_invariants(json.loads(a.tokens.read_text(encoding="utf-8")))

    renders = load_renders(a.tape)
    if not renders:
        print(f"no renders on {a.tape}")
        return 1

    reports = [check_design(r, checks) for r in renders]
    for rep in reports:
        if a.quiet and rep.ok:
            continue
        print(format_design_report(rep))

    broken = [r for r in reports if not r.ok]
    n_v = sum(len(r.violations) for r in broken)
    print(f"\n{len(renders) - len(broken)}/{len(renders)} cells held; "
          f"{n_v} violation(s) over {len(checks)} claims")
    return 1 if broken else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_cli())
