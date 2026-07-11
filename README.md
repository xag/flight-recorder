# flight-recorder

[![tests](https://github.com/xag/flight-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/xag/flight-recorder/actions/workflows/test.yml)

Write down what the outside world told your app, then run the same request again on your own
machine — your real code, the recorded answers, no network.

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

## What it does

Your code talks to things you don't control: a database, an HTTP API, the system clock, a random
number generator. flight-recorder watches those calls and writes down what each one answered —
one line of JSON per request your app handles.

Later, you run that request again. Your real code executes, but every time it asks the outside
world a question, it gets the recorded answer instead. So the run happens a second time, on your
laptop, exactly as it happened in production: no network, no database, no waiting for the bug to
occur again.

That gives you two things:

- **You can look instead of guess.** The replay runs under a tracer, so the value of any variable,
  at any line, is something you look up — not something you infer by reading the code and reasoning
  about what must have happened.
- **You can change what the world said.** The answers are just data in a file. Edit them, and the
  real code runs against an empty result, a clock that goes backwards, a half-corrupt record —
  states that are painful to set up in a real database.

Keep the recording and you can replay it against every future build: if the code starts asking the
outside world different questions, you are told exactly where it changed.

**It records; it does not stand in.** Every call is forwarded to the real database and the real
API, and what comes back is written down on the way through. The library never evaluates a query
or reimplements a client — so there is no mock to fall out of step with the thing it imitates.

## License

MIT
