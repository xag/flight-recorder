# Contributing

Conformance to the tape format means passing the fixtures, not agreeing with the prose:
every implementation must validate every fixture in [`spec/fixtures/`](spec/fixtures/)
with [`spec/validate.py`](spec/validate.py) (JS: `js/src/spec/validate.js`), and every
new fixture must have been produced by an implementation — never written by hand.

Run the tests with `uv run pytest` (Python) and `npm test` in [`js/`](js/) (Node). A
change to the wire format is a spec change first: `spec/tape-v1.md` is frozen, so new
behavior arrives as new event kinds or a `tape-v2`, not as edits to v1's meaning.
