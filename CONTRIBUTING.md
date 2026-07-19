# Contributing

Conformance to the tape format means passing the fixtures, not agreeing with the prose:
every implementation must validate every fixture in [`spec/fixtures/`](spec/fixtures/)
with [`spec/validate.py`](spec/validate.py) (JS: `js/src/spec/validate.js`), and every
new fixture must have been produced by an implementation — never written by hand.

A change to the wire format is a spec change first: `spec/tape-v1.md` is frozen, so new
behavior arrives as new event kinds or a `tape-v2`, not as edits to v1's meaning.

## Running the tests

Each runtime tests from its own directory. All six run in CI on every push; there is no
combined command.

| Runtime | From      | Command                                                            |
| ------- | --------- | ------------------------------------------------------------------ |
| Python  | root      | `uv run pytest tests/ -q`                                          |
| Node    | `js/`     | `npm test`                                                         |
| .NET    | `csharp/` | `dotnet test tests/FlightRecorder.Tests/FlightRecorder.Tests.csproj -c Release` |
| Go      | `go/`     | `go vet ./... && go test ./...`                                    |
| Java    | `java/`   | `mvn -B test`                                                      |
| PHP     | `php/`    | `composer install && composer exec phpunit`                        |

The docs ledger is a test too: `uv run --group ledger python -m ledger.check` exits 1
while any rule is red. It needs the quern registry — a sibling `../quern-registry`
checkout, or `QUERN_REGISTRY` pointing at one.

## Releasing

Six runtimes, six registries, and no two work alike. Version numbers are **independent
per runtime** — there is no repo-wide version, and no runtime waits for another.

| Runtime | Registry      | Package                         | How                                                        |
| ------- | ------------- | ------------------------------- | ---------------------------------------------------------- |
| Python  | PyPI          | `xag-flight-recorder`           | `uv build && uv publish` (local; needs a PyPI token)        |
| Node    | npm           | `@xag/flight-recorder`          | `npm publish` from `js/`                                    |
| .NET    | NuGet         | `flight-recorder`               | Actions → **nuget-publish** → `mode: release`                |
| Go      | Go modules    | `github.com/xag/flight-recorder/go` | push a tag `go/vX.Y.Z`                                  |
| Java    | Maven Central | `io.github.xag:flight-recorder` | Actions → **maven-release** → `dry_run: false`               |
| PHP     | Packagist     | `poietic/flight-recorder`       | push a tag `php/vX.Y.Z` (splits automatically)               |

Five things here are load-bearing and none of them are guessable:

- **The distribution name is not the project name.** PyPI collapses separators, so
  `flight-recorder` is `flightrecorder` — taken since 2014 by an unrelated project. npm
  uses the `@xag` scope; PyPI takes it as a prefix. Packagist's `xag` vendor belongs to
  someone else entirely, so PHP publishes under `poietic`. The *import* name is
  `flight_recorder` everywhere regardless; only the distribution names differ.
- **The .NET release job must stay in `nuget-publish.yml`.** A NuGet trusted-publishing
  policy is bound to a workflow *file name*, and the `estate-publish` policy names that
  file. The same job in any other file gets a 401 at token exchange. That file also still
  holds the 0.0.0 name-reservation job under `mode: reserve` — never publish those as a
  release; there is a guard, do not remove it.
- **Go and PHP tags need their prefix.** A module in `go/` takes its version from a tag
  named `go/vX.Y.Z`; a plain `vX.Y.Z` tag does not apply to it and you get a
  pseudo-version instead. `php/vX.Y.Z` triggers the split to
  [xag/flight-recorder-php](https://github.com/xag/flight-recorder-php), the read-only
  mirror Packagist installs from — Packagist cannot install a package that is not at a
  repository root. Never commit to that mirror; it is overwritten from here.
- **Maven Central is immutable and needs real secrets.** It is the only registry here
  without OIDC, so a Portal token and a GPG signing key live in repo secrets. Run
  **maven-release** with `dry_run: true` first — it builds, signs and verifies the exact
  bundle that would be uploaded, and stops. A published version can never be replaced.
- **The guide's install block is gated.** After publishing, update `_DISTRIBUTIONS` in
  `ledger/tree.py` and drop the `data-status="unpublished"` marker from that runtime's
  snippet in `docs/index.html`. The `install-claims-match-registries` gate goes red while
  the two disagree — discharge it by shipping the package, never by editing the ledger.
