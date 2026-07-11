# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

flight-recorder pushes the heavy lifting from human to AI, and from AI to code.

As AI takes on most of the development, scenario testing and debugging become the bottleneck, and
the work left to the human is the tedious kind. Not anymore: flight-recorder gives the AI the
missing instruments. It records every answer your code got from the outside world while handling a
request, so the agent can re-run that exact request against the real code and watch any variable
as the bug happens.

With those, the AI takes back the loop: designing the scenarios and running them, finding the root
cause by looking it up rather than paying for a guess, catching the regressions, proving the fix —
continuous improvement, closed. What is left to the human is the decisions, not the moving of
things around.

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
