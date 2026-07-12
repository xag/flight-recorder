// flight-recorder — record at the nondeterminism boundary, replay against the real code.
//
// Emits tape format v1 (spec/tape-v1.md), the format both implementations share. Invariants
// and mutation consume the tape; only record and replay are language-bound, because replaying
// JavaScript means re-running JavaScript.
//
//   import * as fr from '@xag/flight-recorder';
//
//   // 1. Name the doors. The one app-specific artifact.
//   export const BOUNDARY = fr.boundaryOf({
//     redact: { token: null, password: null },
//     constants: { 'config.LIMIT': LIMIT },
//   });
//
//   // 2. Wrap what the app holds. A transparent proxy — never a mock.
//   export const store = fr.wrap(storeClient, ['read', 'write'], { prefix: 'store' });
//
//   // 3. Wrap the tools. One recorded line per call; that line IS the execution.
//   export const doThing = fr.tool('do_thing', doThingImpl);
//
//   fr.install(BOUNDARY, { directory: '.flight', enabled: process.env.FLIGHT === '1' });

export { boundaryOf, install, uninstall, tool, wrap, tapePath, FORMAT_VERSION } from './record.js';
export { toJsonable, fromJsonable, redactJsonable, REDACTED } from './serial.js';
export { validateTape, VERSION } from './spec/validate.js';
export { loadTape, pickCall, replayCall } from './replay.js';
export { traced, Trace } from './trace.js';
export { ReplayDivergence, ProbeUnanswerable } from './errors.js';
// The render boundary. Also its own entry point (`@xag/flight-recorder/render`); it imports no
// browser automation library, so nothing that installs this package pays for one.
export { captureLayout, renderCall, RenderTape } from './render.js';
