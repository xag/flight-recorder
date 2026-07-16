"""The freeze, proved from the other direction: a tape RECORDED BY NODE, read by Python.

A tape's meaning cannot depend on which runtime wrote it. `spec/fixtures/node-sem-toy.jsonl` is
produced by the Node recorder's `note()`/`span()` (js/test/sem.test.mjs, FR_REGEN_FIXTURES=1);
here the Python `Recording.spans()` reads its semantic skeleton and must find exactly the tree the
Node code described. If this ever disagrees with the fixture, the two runtimes have forked their
understanding of `sem`, which is the single failure the shared format exists to prevent.

The conformance of the fixture itself (against both `spec/validate.py` and the Node mirror) is
covered by `test_tape_spec.py`'s fixture sweep and the Node `tape-spec` suite. This file is about
the SHAPE a reader recovers, not the bytes' legality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import flight_recorder as fr

FIXTURE = Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "node-sem-toy.jsonl"


@pytest.mark.skipif(not FIXTURE.exists(),
                    reason="node-sem-toy.jsonl not generated (run js sem.test.mjs with "
                           "FR_REGEN_FIXTURES=1)")
def test_a_node_recorded_sem_tape_reads_through_python_spans():
    tree = fr.Recording.load(FIXTURE).call(0).spans()

    # The call itself, and the one clock read that happened before the outer span opened.
    assert tree["phase"] == "call" and tree["name"] == "enrol"
    assert [e["k"] for e in tree["events"]] == ["now"]

    enrol = tree["children"][0]
    assert (enrol["name"], enrol["phase"], enrol["outcome"]) == ("enrol", "span", "ok")

    # The same nesting the Node code wrote: two spans and two point notes, in order.
    assert [(c["name"], c["phase"], c["outcome"]) for c in enrol["children"]] == [
        ("load_corpus", "span", "ok"),
        ("corpus_read", "point", None),
        ("register", "span", "error"),
        ("registration_failed", "point", None),
    ]

    # Each span encloses exactly the evidence it claims — the property the whole event kind
    # exists for, recovered from a tape the other runtime wrote.
    load, _, register, _ = enrol["children"]
    assert [e["k"] for e in load["events"]] == ["fx"]
    assert [e["fn"] for e in register["events"]] == ["store.set", "store.boom"]


@pytest.mark.skipif(not FIXTURE.exists(), reason="node-sem-toy.jsonl not generated")
def test_the_node_tape_renders_top_down_like_a_python_one():
    rendered = fr.Recording.load(FIXTURE).call(0).render_spans()
    assert "register  ERROR  (2 fx)" in rendered
    assert "load_corpus  ok  (1 fx)" in rendered
