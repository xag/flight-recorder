# @xag/flight-recorder

Record what the outside world told your code — every store answer, HTTP response, clock read and
random draw — as one small JSONL file per request: a *tape*. Replay that file against your real
code later: same inputs, same execution, and when a replay diverges the report names the first
difference instead of leaving you to guess.

The Node implementation of [flight-recorder](https://github.com/xag/flight-recorder). It writes
**tape format v1**, the same tapes the Python and .NET implementations write, so one analysis
serves all three.

```bash
npm install @xag/flight-recorder
```

## Quickstart

An ES module's namespace is immutable, so — unlike the Python port — the boundary is **the object
the app holds**: wrap it. The clock and RNG are the exception and are shimmed globally, because
there the app holds nothing to wrap.

```js
import * as fr from '@xag/flight-recorder';

const BOUNDARY = fr.boundaryOf({ redact: { password: null } });
export const store = fr.wrap(storeClient, ['get', 'set']);   // a transparent proxy, not a mock
export const greet = fr.tool('greet', greetImpl);            // the call boundary

fr.install(BOUNDARY, { directory: '.flight', enabled: process.env.FLIGHT === '1' });

// replay: the recorded answers are fed back; the real code re-runs exactly
const call = fr.pickCall(fr.loadTape('.flight/flight-….jsonl'), { fn: 'greet' });
const report = await fr.replayCall({ call, fn: greetImpl, boundary: BOUNDARY });
report.ok;          // reproduced the recording bit-for-bit
report.divergence;  // …or the exact point where behaviour changed
```

## The rest is in the guide

Redaction by field and by value, recording off-box with a sink (and `waitUntil` on serverless),
editing the tape to visit worlds that never happened, variable-level tracing via the V8 Inspector,
and semantic spans — all of it, with runnable examples in every language:

**→ [xag.github.io/flight-recorder](https://xag.github.io/flight-recorder/)**

The tape is a frozen, cross-language standard:
[`spec/tape-v1.md`](https://github.com/xag/flight-recorder/blob/main/spec/tape-v1.md).

## License

Apache-2.0 — see [LICENSE](https://github.com/xag/flight-recorder/blob/main/LICENSE).
