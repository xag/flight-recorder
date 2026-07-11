# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

While your app handles a request, flight-recorder writes down what the database, the API, the
clock and the random number generator answered. Replay that recording and your real code runs the
request again — same answers, on your machine, no network.

So a bug that happened once, in production, is one you can now re-run as often as you like and
read variable by variable, instead of reconstructing what must have happened. Keep the recording
and it doubles as a test: replay it against a later build and you are told where the behaviour
changed.

## → [Documentation](https://xag.github.io/flight-recorder/)

[Slides — Testing as Simulation](https://xag.github.io/flight-recorder/slides.html)

## Install

```bash
pip install flight-recorder          # Python
npm install @xag/flight-recorder     # Node
```

| Language | Package | Source |
|---|---|---|
| Python | `flight-recorder` (PyPI) | [`flight_recorder/`](flight_recorder/) |
| Node | [`@xag/flight-recorder`](https://www.npmjs.com/package/@xag/flight-recorder) (npm) | [`js/`](js/) |

## License

MIT
