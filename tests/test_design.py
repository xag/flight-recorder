"""The design invariants, against a tape produced by a real browser.

`tests/fixtures/defects.html` holds seven deliberate defects, each chosen because the source cannot
see it. `tests/fixtures/design-defects.jsonl` is that page, rendered — regenerate it with
`RECORD=1 node --test test/render.test.mjs` in js/ (playwright is opt-in; see `npm run browser`).
Prose drifts; fixtures do not.

These tests need no browser, because a tape is only data. That is the whole point of freezing the
format: the CAPTURE is language-bound and needs a real cascade to have been run, and the ANALYSIS
is written once and runs anywhere, on a machine with nothing installed.

The claim under test is not "the checker runs". It is that each defect is found, that it is found
for the RIGHT reason, and that the good claim next door is left alone — an instrument that fires on
everything is as useless as one that fires on nothing.
"""

from pathlib import Path

import pytest

from flight_recorder.design import (
    check_design, contrast, load_renders, standard_invariants, token_invariants,
)

TAPE = Path(__file__).parent / "fixtures" / "design-defects.jsonl"


@pytest.fixture(scope="module")
def render():
    renders = load_renders(TAPE)
    assert renders, f"{TAPE.name} holds no renders — regenerate it (RECORD=1 node --test)"
    return renders[0]


@pytest.fixture(scope="module")
def report(render):
    return check_design(render, standard_invariants())


def violated(report) -> dict:
    return {v.invariant: v.detail for v in report.violations}


# --- every defect is found, and only the defects ------------------------------------------


def test_finds_every_defect(report):
    assert report.outcome == "violated"
    assert not any(v.broke for v in report.violations), "a claim raised instead of asserting"
    assert set(violated(report)) == {
        "every text is legible on the backdrop it actually paints on",
        "no text is cut off by the box it sits in",
        "every focusable shows a focus ring under a real Tab",
        "every target is at least 24px on its short side",
        "everything clickable is reachable by keyboard",
        "every control says what it is",
    }
    # The page does not scroll sideways, and the checker does not pretend it does.
    assert "the page never scrolls sideways" not in violated(report)


def test_contrast_sees_through_opacity(report):
    """The defect a stylesheet linter cannot have: #1a1c20 on #ffffff is 15:1, and the eye gets
    2:1, because an ancestor is 30% opaque. The colour is not wrong. The pixel is."""
    detail = violated(report)["every text is legible on the backdrop it actually paints on"]
    assert "p.faded" in detail
    assert "faded to 0.3 opacity" in detail
    assert "#babbbc" in detail  # what actually painted, not what was asked for


def test_contrast_sees_a_backdrop_that_never_painted(report):
    """The panel's background is inside a media query that does not match, so the chip is on white
    — and the ratio the author reasoned about was against a dark panel."""
    detail = violated(report)["every text is legible on the backdrop it actually paints on"]
    assert "span.chip" in detail
    assert "#8a8a8a on #ffffff" in detail


def test_finds_the_cut_sentence(report):
    detail = violated(report)["no text is cut off by the box it sits in"]
    assert "div.card" in detail
    assert "overflow:hidden" in detail


def test_finds_the_missing_focus_ring(report):
    detail = violated(report)["every focusable shows a focus ring under a real Tab"]
    assert "button.ghost" in detail
    # The other two buttons keep the UA's ring, and the claim leaves them alone.
    assert "button.tiny" not in detail


def test_finds_the_small_target(report):
    detail = violated(report)["every target is at least 24px on its short side"]
    assert "button.tiny" in detail and "16x16px" in detail


def test_finds_the_unreachable_button(report):
    """role=button + tabindex=-1: operable by mouse, invisible to Tab. The instrument must not
    define this away by excluding tabindex=-1 from what counts as a control."""
    detail = violated(report)["everything clickable is reachable by keyboard"]
    assert "fake-button" in detail and "'Submit'" in detail


def test_finds_the_nameless_icon(report):
    detail = violated(report)["every control says what it is"]
    assert "button.icon" in detail


# --- the parts a claim is built from -------------------------------------------------------


def test_contrast_is_wcag():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)  # the AA borderline


def test_a_palette_claim_is_about_what_painted(render):
    """`background:none` on a <button> still paints #f0f0f0 — the UA sheet is in the cascade, and a
    palette that never declared that colour is being violated whatever the stylesheet says."""
    report = check_design(render, token_invariants({"colors": ["#1a1c20", "#ffffff", "#8a8a8a"]}))
    detail = violated(report)["every colour comes from the palette"]
    assert "#f0f0f0" in detail  # the browser's default button, in nobody's design system


def test_a_render_that_never_settled_is_not_evidence(render):
    """A capture taken before the fonts loaded measured a fallback font. Asserting over it would be
    asserting over a fiction — that impeaches the tape, not the design."""
    from dataclasses import replace

    broken = replace(render, ambient={**render.ambient, "fontsReady": "loading"})
    report = check_design(broken, standard_invariants())
    assert report.outcome == "unsettled"
    assert report.checked == 0
