// The Node recorder: does it write a conformant tape, and is that tape the execution?

import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as fr from '../src/index.js';
import { ToyStore, makeTools } from './toy.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(HERE, '..', '..', 'spec', 'fixtures');

let dir;
let store;
let tools;

function setup({ redact = {}, constants = {}, gate = null } = {}) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const raw = new ToyStore();
  store = fr.wrap(raw, ['get', 'set', 'boom'], { prefix: 'store' });
  const b = fr.boundaryOf({ redact, constants });
  fr.install(b, { directory: dir, gate });

  // Wrap every tool: this is the call boundary. (Forgetting this is exactly how a tape ends
  // up with a valid header and no calls — which is *conformant* and *useless*, hence the
  // "and it actually recorded something" assertion below.)
  tools = Object.fromEntries(
    Object.entries(makeTools(store)).map(([name, fn]) => [name, fr.tool(name, fn)]),
  );
  return raw;
}

const readTape = () => fs.readFileSync(fr.tapePath(), 'utf8');
const lines = () => readTape().split('\n').filter(Boolean).map(JSON.parse);
const calls = () => lines().filter((l) => l.ev === 'call');

beforeEach(() => {});
afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
});

// --- conformance -------------------------------------------------------------------

test('the recorder writes a tape that satisfies the frozen spec', async () => {
  setup();
  await tools.greet({ user: 'alice' });
  await assert.rejects(() => tools.explode({ user: 'ghost' }));

  const violations = fr.validateTape(readTape());
  assert.deepEqual(violations, [], 'own tape violates the spec:\n' + violations.join('\n'));

  // A header-only tape is perfectly conformant and proves nothing. Guard the guard.
  assert.equal(calls().length, 2, 'the tape must actually contain the calls');
  const kinds = new Set(calls().flatMap((c) => c.events.map((e) => e.k)));
  assert.deepEqual([...kinds].sort(), ['fx', 'now', 'rand']);
});

test('the header names node as the runtime, and carries the constants', () => {
  setup({ constants: { 'config.LIMIT': 5 } });
  const [header] = lines();
  assert.equal(header.ev, 'session');
  assert.equal(header.version, 1);
  assert.equal(header.node, process.versions.node);
  assert.ok(!('python' in header), 'a tape names exactly one runtime');
  assert.deepEqual(header.constants, { 'config.LIMIT': 5 });
});

// --- the line IS the execution -------------------------------------------------------

test('one call, every answer the world gave, in the order it was asked', async () => {
  setup();
  await tools.greet({ user: 'alice' });

  const [c] = calls();
  assert.equal(c.fn, 'greet');
  assert.equal(c.seq, 1);
  assert.deepEqual(c.kwargs, { user: 'alice' });
  assert.equal(c.error, null);

  // greet: get → randomBytes → Date.now → set. Order is load-bearing: replay pops these
  // in sequence and a different question at position n is where behaviour changed.
  assert.deepEqual(
    c.events.map((e) => e.k + (e.k === 'fx' ? `:${e.fn}` : e.k === 'rand' ? `:${e.m}` : '')),
    ['fx:store.get', 'rand:bytes', 'now', 'fx:store.set'],
  );

  const [get, rand, now, set] = c.events;
  assert.deepEqual(get.args, ['alice']);
  assert.deepEqual(get.res, { name: 'Alice', x: 3 });
  assert.equal(get.kwargs !== undefined && Object.keys(get.kwargs).length, 0);

  assert.equal(rand.n, 4);
  assert.match(rand.hex, /^[0-9a-f]{8}$/);
  assert.ok(!Number.isNaN(Date.parse(now.v)));

  assert.equal(set.args[0], 'greeted:alice');
  assert.equal(set.res, 'OK');

  // and the result carries what the world actually handed back
  assert.equal(c.result.name, 'Alice');
  assert.equal(c.result.token, rand.hex);
});

test('an effect that throws is recorded as err, not res', async () => {
  setup();
  await assert.rejects(() => tools.explode({ user: 'ghost' }));

  const [c] = calls();
  const [e] = c.events;
  assert.equal(e.k, 'fx');
  assert.ok(!('res' in e), 'a raised effect has no result');
  assert.equal(e.err.type, 'ToyError');
  assert.deepEqual(e.err.args, ['no such key: ghost', 42]);
  assert.match(c.error, /ToyError/, 'the tool re-raised, so the call records an error');
});

test('a tool that throws still records the answers it got first', async () => {
  setup();
  await assert.rejects(() => tools.halfway({ user: 'alice' }));

  const [c] = calls();
  assert.equal(c.events.length, 1, 'the effect it did get through is on the tape');
  assert.equal(c.events[0].fn, 'store.get');
  assert.match(c.error, /tool gave up/);
  assert.equal(c.result, null);
});

test('seq is 1-based and monotonic across calls', async () => {
  setup();
  await tools.greet({ user: 'alice' });
  await tools.greet({ user: 'bob' });
  assert.deepEqual(calls().map((c) => c.seq), [1, 2]);
});

// --- the proxy is transparent ---------------------------------------------------------

test('wrapping does not change what the app sees', async () => {
  const raw = setup();
  const out = await tools.greet({ user: 'alice' });

  assert.equal(out.name, 'Alice');
  assert.equal(raw.writes.length, 1, 'the real store really was written to');
  assert.equal(raw.writes[0][0], 'greeted:alice');
});

test('un-named methods pass through unrecorded', async () => {
  setup();
  const raw = new ToyStore();
  const partial = fr.wrap(raw, ['get'], { prefix: 's' });
  await fr.tool('t', async () => {
    await partial.get('alice');
    await partial.set('k', 1); // not named → invisible to the tape
  })({});

  const [c] = calls();
  assert.deepEqual(c.events.map((e) => e.fn), ['s.get']);
  assert.equal(raw.writes.length, 1, 'but it still really ran');
});

test('recording is off outside a tool call', async () => {
  setup();
  await store.get('alice'); // no enclosing tool
  assert.equal(calls().length, 0, 'an effect with no call to belong to writes nothing');
});

// --- concurrency ----------------------------------------------------------------------

test('concurrent calls do not interleave their events', async () => {
  setup();
  await Promise.all([
    tools.greet({ user: 'alice' }),
    tools.greet({ user: 'bob' }),
    tools.greet({ user: 'alice' }),
  ]);

  const cs = calls();
  assert.equal(cs.length, 3);
  for (const c of cs) {
    // Each call must own exactly its own four answers. AsyncLocalStorage is what makes
    // this true across awaits; a module-level buffer would shred it.
    assert.deepEqual(c.events.map((e) => e.k), ['fx', 'rand', 'now', 'fx']);
    assert.equal(c.events[0].args[0], c.kwargs.user);
  }
});

// --- redaction ------------------------------------------------------------------------

test('redaction covers every surface: tool kwargs, effect args, effect res, tool result', async () => {
  setup({ redact: { password: null } });
  await tools.signup({ email: 'a@b.c', password: 'hunter2' });

  const tape = readTape();
  assert.ok(!tape.includes('hunter2'), 'the secret is nowhere on the tape');

  const [c] = calls();
  assert.equal(c.kwargs.password, '[REDACTED]');       // tool kwarg
  assert.equal(c.events[0].args[1].password, '[REDACTED]'); // effect arg
  assert.equal(c.result.password, '[REDACTED]');       // tool result
  assert.equal(c.result.account.password, '[REDACTED]'); // nested in the result
  assert.equal(c.kwargs.email, 'a@b.c', 'and nothing else was touched');
});

test('a redaction rule that throws masks rather than leaks', async () => {
  setup({ redact: { password: () => { throw new Error('bad rule'); } } });
  await tools.signup({ email: 'a@b.c', password: 'hunter2' });

  assert.ok(!readTape().includes('hunter2'), 'the failure direction is masked, never leaked');
  assert.equal(calls()[0].kwargs.password, '[REDACTED]');
});

// --- gating ---------------------------------------------------------------------------

test('a gate decides per call', async () => {
  setup({ gate: (fn) => fn === 'greet' });
  await tools.greet({ user: 'alice' });
  await assert.rejects(() => tools.halfway({ user: 'alice' }));

  assert.deepEqual(calls().map((c) => c.fn), ['greet'], 'the gated-out call left no line');
});

// --- uninstall ------------------------------------------------------------------------

test('uninstall restores the clock and the RNG', async () => {
  const before = { now: Date.now, bytes: (await import('node:crypto')).default.randomBytes };
  setup();
  assert.notEqual(Date.now, before.now, 'patched while installed');
  fr.uninstall();
  assert.equal(Date.now, before.now, 'and put back');
  assert.equal((await import('node:crypto')).default.randomBytes, before.bytes);
});

// --- freeze the node fixture, for the PYTHON checker to validate -----------------------

test('regenerate the node fixture (FR_REGEN_FIXTURES=1)', async (t) => {
  if (!process.env.FR_REGEN_FIXTURES) return t.skip('set FR_REGEN_FIXTURES=1 to regenerate');

  setup({ redact: { password: null }, constants: { 'toy.LIMIT': 3 } });
  await tools.greet({ user: 'alice' });
  await assert.rejects(() => tools.explode({ user: 'ghost' }));
  await tools.signup({ email: 'a@b.c', password: 'hunter2' });

  const text = readTape();
  assert.deepEqual(fr.validateTape(text), []);
  fs.mkdirSync(FIXTURES, { recursive: true });
  fs.writeFileSync(path.join(FIXTURES, 'node-toy.jsonl'), text, 'utf8');
});
