// The boundary's masks reach the trace, not only the tape.
//
// A trace holds every local of every traced line, which means it holds values before they reach
// any masking. The boundary's `redact` and `scrub` mask a trace observation as they mask a tape
// event, on the same part of it: the payload (`vars`, keyed by local name as a tape's kwargs are
// keyed by parameter name), never the envelope (`file`, `line`, `fn`). Then `forbid` reads the
// masked observation, and a hit refuses the whole trace.

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import * as fr from '../src/index.js';
import { makeTools } from './traced-secret-app.mjs';

// Built by concatenation, as in the fixture, so no file here carries a live-looking key.
const KEY = 'sk-live-' + '9f8e7d6c5b4a39281706';
const KEY_SHAPE = 'sk-live-[0-9a-f]+';
const APP = 'traced-secret-app.mjs';

let dir;

afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
  dir = null;
});

/** Record one call of `name` with nothing declared; the tape is clean by construction. */
async function recordCall(name) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const store = fr.wrap({ get: async () => ({ v: 1 }) }, ['get'], { prefix: 'kv' });
  fr.install(fr.boundaryOf({}), { directory: dir });
  await fr.tool(name, makeTools(store)[name])({ email: 't@x.com' });
  const tape = fr.tapePath();
  const call = fr.pickCall(fr.loadTape(tape), { fn: name });
  fr.uninstall();
  assert.ok(!fs.readFileSync(tape, 'utf8').includes(KEY), 'the tape itself never held the key');
  return call;
}

/** Replay it against a store that must not be touched, tracing the app. */
function replay(call, name, boundary) {
  const dead = fr.wrap({ get: () => { throw new Error('the world was touched'); } }, ['get'], { prefix: 'kv' });
  return fr.replayCall({ call, fn: makeTools(dead)[name], boundary: fr.boundaryOf(boundary), trace: [APP] });
}

const text = (trace) => JSON.stringify(trace.observations);

test('with nothing declared, the trace carries a local the tape never could', async () => {
  // The hole the masks below close, shown open: guards the guard, so the tests that assert the
  // key's absence are not passing because the trace never saw it.
  const report = await replay(await recordCall('deriveKey'), 'deriveKey', {});
  assert.ok(report.ok, report.divergence?.message);
  assert.ok(text(report.trace).includes(KEY));
});

test('the scrub masks the trace as it masks the tape', async () => {
  const scrub = (s) => s.replaceAll(KEY, '[KEY]');
  const report = await replay(await recordCall('deriveKey'), 'deriveKey', { scrub });
  assert.ok(report.ok, report.divergence?.message);
  assert.ok(text(report.trace).includes('[KEY]'));
  assert.ok(!text(report.trace).includes(KEY));
});

test('a redacted field name masks the local of that name', async () => {
  const report = await replay(await recordCall('deriveKey'), 'deriveKey', { redact: { secret: null } });
  assert.ok(report.ok, report.divergence?.message);
  const held = report.trace.observations.filter((o) => 'secret' in o.vars).map((o) => o.vars.secret);
  assert.ok(held.length > 0, 'the local was observed');
  assert.ok(held.every((v) => v === fr.REDACTED), JSON.stringify(held));
  assert.ok(!text(report.trace).includes(KEY));
});

test('the trace envelope is not redacted', async () => {
  // As on the tape, rules rewrite payloads only: a rule named like an envelope key cannot take
  // the structure of the trace apart. `secret` is in the rules too, so the masking demonstrably ran.
  const redact = { file: null, line: null, fn: null, vars: null, secret: null };
  const report = await replay(await recordCall('deriveKey'), 'deriveKey', { redact });
  assert.ok(report.ok, report.divergence?.message);
  const obs = report.trace.observations;
  assert.ok(obs.length > 0);
  for (const o of obs) {
    assert.equal(o.fn, 'deriveKey');
    assert.ok(o.file.endsWith(APP), o.file);
    assert.equal(typeof o.line, 'number');
    assert.equal(typeof o.vars, 'object');
  }
  assert.ok(obs.some((o) => o.vars.secret === fr.REDACTED));
});

test('forbid refuses a trace holding a forbidden value, naming the pattern and never the match', async () => {
  const call = await recordCall('deriveKey');
  await assert.rejects(
    () => replay(call, 'deriveKey', { forbid: [KEY_SHAPE] }),
    (e) => {
      assert.ok(e instanceof fr.ForbiddenValue, String(e));
      assert.ok(e.message.includes(KEY_SHAPE));
      assert.ok(!e.message.includes(KEY));
      return true;
    },
  );
});

test('forbid reads the masked trace, so a scrubbed local passes it', async () => {
  const scrub = (s) => s.replaceAll(KEY, '[KEY]');
  const report = await replay(await recordCall('deriveKey'), 'deriveKey', { scrub, forbid: [KEY_SHAPE] });
  assert.ok(report.ok, report.divergence?.message);
  assert.ok(!text(report.trace).includes(KEY));
});

test('traced() takes the masks and the tripwire directly', async () => {
  const tools = makeTools({ get: async () => ({ v: 1 }) });
  const run = () => tools.deriveKey({ email: 't@x.com' });

  const { trace } = await fr.traced(run, { include: [APP], redact: { secret: null } });
  assert.ok(trace.observations.some((o) => o.vars.secret === fr.REDACTED));
  assert.ok(!text(trace).includes(KEY));

  await assert.rejects(() => fr.traced(run, { include: [APP], forbid: [KEY_SHAPE] }), fr.ForbiddenValue);
});

// --- process-global state in a frame is its description, never its contents ------------------
//
// Python once traced a local holding a module as the module's attributes, and through
// `os.sys.modules` that reached the whole environment. These pin that the Node tracer does not:
// it reads an object local by its debugger description and never asks for its properties.

const ENV_SENTINEL = 'fr-env-sentinel-7c1e9a';

test('a traced frame holding the process, the global object and a module writes no environment', async () => {
  process.env.FR_TRACE_SENTINEL = ENV_SENTINEL;
  try {
    const report = await replay(await recordCall('holdsTheWorld'), 'holdsTheWorld', {});
    assert.ok(report.ok, report.divergence?.message);
    const names = report.trace.names();
    for (const n of ['proc', 'world', 'env', 'ns']) assert.ok(names.includes(n), `${n} was observed`);
    for (const n of ['proc', 'world', 'env', 'ns']) {
      for (const { value } of report.trace.values(n)) assert.equal(typeof value, 'string', `${n} is a description`);
    }
    assert.ok(!text(report.trace).includes(ENV_SENTINEL));
  } finally {
    delete process.env.FR_TRACE_SENTINEL;
  }
});
