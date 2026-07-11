# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

flight-recorder records what the database, the API and the clock answered while your app handled a
request, so you can replay it on your machine against the real code and read any variable instead
of guessing what happened.

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
