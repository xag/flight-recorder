

def test_a_tape_is_parsed_once_per_identity_and_a_rewrite_is_reread(tmp_path):
    """The reparse debt's discharge condition, both halves: the second load of an
    unchanged tape answers from the cache, and a tape rewritten under the same name
    is re-read rather than served stale - the one failure a cache can introduce
    here, and the one that would be silent."""
    import json as _json
    import os

    from flight_recorder.replay import load_session

    tape = tmp_path / "one.jsonl"

    def write(fn_name, mtime_ns):
        lines = [_json.dumps({"ev": "session", "started": "t"}),
                 _json.dumps({"ev": "call", "fn": fn_name, "events": []})]
        tape.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(tape, ns=(mtime_ns, mtime_ns))

    write("first", 1_000_000_000)
    h1, c1 = load_session(tape)
    h2, c2 = load_session(tape)
    assert h1 is h2 and c1 is c2, "the unchanged tape was parsed twice"

    write("second", 2_000_000_000)
    h3, c3 = load_session(tape)
    assert c3 is not c1
    assert c3[0]["fn"] == "second", "a rewritten tape was served stale"
