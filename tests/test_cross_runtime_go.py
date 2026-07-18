"""The freeze, proved from a third runtime: a tape RECORDED BY GO, read by Python.

`spec/fixtures/go-sem-toy.jsonl` is produced by the Go recorder's Span/Note (go/fixtures_test.go,
regenerated with FR_REGEN_FIXTURES=1). Its `enrol` scenario is the same one the Node and Python
sem fixtures carry, down to the store.get/set/boom leaf effects — so the Python `Recording.spans()`
reader must recover exactly the tree the Go code described. If this ever disagrees, the runtimes
have forked their understanding of `sem`, which is the single failure the shared format exists to
prevent.

Conformance of the fixture itself (against validate.py, validate.js AND validate.go) rides the
globbed fixture sweep in test_tape_spec.py and the Node/Go checker suites. This file is about the
SHAPE a reader recovers, not the bytes' legality.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import flight_recorder as fr

FIXTURE = Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "go-sem-toy.jsonl"


@pytest.mark.skipif(not FIXTURE.exists(),
                    reason="go-sem-toy.jsonl not generated (run go test with FR_REGEN_FIXTURES=1)")
def test_a_go_recorded_sem_tape_reads_through_python_spans():
    tree = fr.Recording.load(FIXTURE).call(0).spans()

    # The call itself, and the one clock read that happened before the outer span opened.
    assert tree["phase"] == "call" and tree["name"] == "enrol"
    assert [e["k"] for e in tree["events"]] == ["now"]

    enrol = tree["children"][0]
    assert (enrol["name"], enrol["phase"], enrol["outcome"]) == ("enrol", "span", "ok")

    # The same nesting the Go code wrote: two spans and two point notes, in order.
    assert [(c["name"], c["phase"], c["outcome"]) for c in enrol["children"]] == [
        ("load_corpus", "span", "ok"),
        ("corpus_read", "point", None),
        ("register", "span", "error"),
        ("registration_failed", "point", None),
    ]

    # Each span encloses exactly the evidence it claims — recovered from a tape Go wrote.
    load, _, register, _ = enrol["children"]
    assert [e["k"] for e in load["events"]] == ["db"]
    assert [e["fn"] for e in register["events"]] == ["store.set", "store.boom"]


@pytest.mark.skipif(not FIXTURE.exists(), reason="go-sem-toy.jsonl not generated")
def test_the_go_tape_renders_top_down_like_a_python_one():
    rendered = fr.Recording.load(FIXTURE).call(0).render_spans()
    assert "register  ERROR  (2 fx)" in rendered
    assert "load_corpus  ok  (1 db)" in rendered
