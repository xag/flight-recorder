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
//     redact: { token: null, password: null, encSender: null },
//     constants: { 'config.LIMIT': LIMIT },
//   });
//
//   // 2. Wrap what the app holds. A transparent proxy — never a mock.
//   const redis = fr.wrap(new Redis({ url, token }), ['get', 'set', 'hgetall'], { prefix: 'kv' });
//
//   // 3. Wrap the tools. One recorded line per call; that line IS the execution.
//   export const submit = fr.tool('submit_article', submitImpl);
//
//   fr.install(BOUNDARY, { directory: '.flight', enabled: process.env.FLIGHT === '1' });

export { boundaryOf, install, uninstall, tool, wrap, tapePath, FORMAT_VERSION } from './record.js';
export { toJsonable, fromJsonable, redactJsonable, REDACTED } from './serial.js';
export { validateTape, VERSION } from './spec/validate.js';
export { loadTape, pickCall, replayCall } from './replay.js';
export { ReplayDivergence, ProbeUnanswerable } from './errors.js';
