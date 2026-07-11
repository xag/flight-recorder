# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

flight-recorder records what the database, the API and the clock answered while your app handled a
request, so the request can be replayed against the real code with every variable readable.

That is the missing link for letting an AI run the quality loop: without instruments it can only
read the source and theorise, and a human has to run the app, reproduce the bug and re-run every
scenario after each fix.

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
