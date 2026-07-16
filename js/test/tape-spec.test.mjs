// The freeze, proved across runtimes.
//
// The load-bearing test in this file is the first one: the Node checker validating a tape
// that PYTHON produced. That is the whole contract — Python records, Node reads, one
// analysis engine serves both. Everything else here keeps the checker honest, because a
// checker that accepts everything would pass that test too.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { validateTape, VERSION } from '../src/spec/validate.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(HERE, '..', '..', 'spec', 'fixtures');

const SESSION = {
  ev: 'session', version: 1, started: '2026-07-11T10:00:00+02:00',
  node: '24.0.0', constants: {},
};
const CALL = {
  ev: 'call', seq: 1, fn: 't', kwargs: {}, events: [],
  result: null, error: null, ts: '2026-07-11T10:00:00+02:00', ms: 1.0,
};
const tape = (...lines) => lines.map((x) => JSON.stringify(x)).join('\n') + '\n';

test('version is frozen at 1', () => {
  assert.equal(VERSION, 1);
});

// --- THE contract -----------------------------------------------------------------

test('the Node checker validates a tape recorded by PYTHON', () => {
  const fixtures = fs.readdirSync(FIXTURES).filter((f) => f.endsWith('.jsonl'));
  assert.ok(fixtures.length, 'no fixtures — the freeze is unverified');

  for (const f of fixtures) {
    const text = fs.readFileSync(path.join(FIXTURES, f), 'utf8');
    assert.deepEqual(validateTape(text), [], `${f} violates the frozen spec`);
  }
});

test('the python fixture really does exercise every event kind', () => {
  // A cross-runtime agreement about a tape with no `rand` in it proves nothing about
  // `rand`. Guard the guard.
  const text = fs.readFileSync(path.join(FIXTURES, 'python-toy.jsonl'), 'utf8');
  const calls = text.split('\n').filter(Boolean).map(JSON.parse).filter((l) => l.ev === 'call');
  const kinds = new Set(calls.flatMap((c) => (c.events || []).map((e) => e.k)));

  for (const k of ['fx', 'db', 'now', 'rand']) {
    assert.ok(kinds.has(k), `fixture never exercises '${k}'`);
  }
  const errs = calls.flatMap((c) => c.events || []).filter((e) => e.k === 'fx' && 'err' in e);
  assert.ok(errs.length, "fixture has no fx event carrying 'err'");
});

test('a naive now.v is accepted (it is an app-visible value, not metadata)', () => {
  // Python's datetime.now() is naive. Normalising it to aware on replay would change
  // behaviour — comparing naive with aware raises. This is the assertion that caught the
  // spec being wrong about the real recorder.
  const call = { ...CALL, events: [{ k: 'now', v: '2026-07-11T14:39:56.231978' }] };
  assert.deepEqual(validateTape(tape(SESSION, call)), []);
});

test('call.ts and session.started, being metadata, must be timezone-aware', () => {
  const naive = { ...CALL, ts: '2026-07-11T10:00:00' };
  assert.ok(validateTape(tape(SESSION, naive)).some((v) => v.includes('timezone-aware')));
});

// --- the checker must be sharp, or "conformant" means nothing ----------------------

test('accepts a minimal valid tape', () => {
  assert.deepEqual(validateTape(tape(SESSION, CALL)), []);
});

test('rejects an unknown version', () => {
  assert.ok(validateTape(tape({ ...SESSION, version: 2 }, CALL)).some((v) => v.includes('version')));
});

test('rejects a session naming two runtimes', () => {
  const both = { ...SESSION, python: '3.13' };
  assert.ok(validateTape(tape(both, CALL)).some((v) => v.includes('exactly one runtime')));
});

test('rejects a missing header', () => {
  assert.ok(validateTape(tape(CALL)).some((v) => v.includes('session header')));
});

test('rejects non-monotonic seq', () => {
  assert.ok(validateTape(tape(SESSION, CALL, { ...CALL, seq: 5 })).some((v) => v.includes('monotonic')));
});

test('rejects fx carrying both res and err', () => {
  const ev = { k: 'fx', fn: 'f', args: [], kwargs: {}, res: 1, err: { type: 'E', repr: 'E()', args: [] } };
  assert.ok(validateTape(tape(SESSION, { ...CALL, events: [ev] })).some((v) => v.includes('exactly one')));
});

test('rejects db carrying both res and args', () => {
  const ev = { k: 'db', op: 'get', sig: "c('x')", res: [], args: [1] };
  assert.ok(validateTape(tape(SESSION, { ...CALL, events: [ev] })).some((v) => v.includes('never both')));
});

test('rejects rand.idx outside the population', () => {
  const ev = { k: 'rand', m: 'sample', n: 3, kk: 1, idx: [7] };
  assert.ok(validateTape(tape(SESSION, { ...CALL, events: [ev] })).some((v) => v.includes('out of range')));
});

test('rejects rand.idx disagreeing with kk', () => {
  const ev = { k: 'rand', m: 'sample', n: 5, kk: 3, idx: [0, 1] };
  assert.ok(validateTape(tape(SESSION, { ...CALL, events: [ev] })).some((v) => v.includes('kk=')));
});

// --- sem: parity with spec/validate.py -----------------------------------------------

const semCall = (...events) => ({ ...CALL, events });

test('accepts well-nested spans', () => {
  const call = semCall(
    { k: 'sem', name: 'outer', phase: 'begin', sid: 1 },
    { k: 'sem', name: 'inner', phase: 'begin', sid: 2 },
    { k: 'sem', name: 'mark', phase: 'point', sid: 3, data: { n: 1 } },
    { k: 'sem', name: 'inner', phase: 'end', sid: 2, outcome: 'ok' },
    { k: 'sem', name: 'outer', phase: 'end', sid: 1, outcome: 'error' },
  );
  assert.deepEqual(validateTape(tape(SESSION, call)), []);
});

test('rejects straddling spans', () => {
  const call = semCall(
    { k: 'sem', name: 'a', phase: 'begin', sid: 1 },
    { k: 'sem', name: 'b', phase: 'begin', sid: 2 },
    { k: 'sem', name: 'a', phase: 'end', sid: 1 },
    { k: 'sem', name: 'b', phase: 'end', sid: 2 },
  );
  assert.ok(validateTape(tape(SESSION, call)).some((v) => v.includes('well-nested')));
});

test('rejects an unclosed span', () => {
  const call = semCall({ k: 'sem', name: 'a', phase: 'begin', sid: 1 });
  assert.ok(validateTape(tape(SESSION, call)).some((v) => v.includes('never closed')));
});

test('rejects an end with no begin', () => {
  const call = semCall({ k: 'sem', name: 'a', phase: 'end', sid: 1 });
  assert.ok(validateTape(tape(SESSION, call)).some((v) => v.includes('no open span')));
});

test('rejects a reused sid', () => {
  const call = semCall(
    { k: 'sem', name: 'a', phase: 'begin', sid: 1 },
    { k: 'sem', name: 'b', phase: 'point', sid: 1 },
    { k: 'sem', name: 'a', phase: 'end', sid: 1 },
  );
  assert.ok(validateTape(tape(SESSION, call)).some((v) => v.includes('reused')));
});

test('rejects a bad phase and a misplaced outcome', () => {
  const badPhase = semCall({ k: 'sem', name: 'a', phase: 'middle', sid: 1 });
  assert.ok(validateTape(tape(SESSION, badPhase)).some((v) => v.includes('phase')));

  const misplaced = semCall({ k: 'sem', name: 'a', phase: 'point', sid: 1, outcome: 'ok' });
  assert.ok(validateTape(tape(SESSION, misplaced)).some((v) => v.includes('outcome')));
});

test('rejects a non-int sid and an empty name', () => {
  const badSid = semCall({ k: 'sem', name: 'a', phase: 'point', sid: 1.5 });
  assert.ok(validateTape(tape(SESSION, badSid)).some((v) => v.includes("int 'sid'")));

  const noName = semCall({ k: 'sem', name: '', phase: 'point', sid: 1 });
  assert.ok(validateTape(tape(SESSION, noName)).some((v) => v.includes("non-empty string 'name'")));
});

test('a reader from before sem existed still accepts a sem tape (forward compatibility)', () => {
  // The Node port carries the same guarantee as the Python checker's parametrized twin: an
  // unknown event kind is ignored, and the tape stays conformant.
  const call = semCall(
    { k: 'sem-future', name: 'x', phase: 'whatever', sid: 'nope' },
    { k: 'fx', fn: 'f', args: [], kwargs: {}, res: 1 },
  );
  assert.deepEqual(validateTape(tape(SESSION, call)), []);
});

test('tolerates unknown ev and unknown keys (this IS the versioning story)', () => {
  const weird = { ev: 'inflight', fn: 't', whatever: 1 };
  const call = { ...CALL, events: [{ k: 'future-kind', payload: 1 }], unknown_key: true };
  assert.deepEqual(validateTape(tape(SESSION, weird, call)), []);
});

test('tolerates a torn final line, rejects a torn middle one', () => {
  assert.deepEqual(validateTape(tape(SESSION, CALL) + '{"ev":"call","seq":2,"fn":"t'), []);

  const torn = tape(SESSION) + '{"ev":"call","seq":1,"fn":"t\n' + JSON.stringify(CALL) + '\n';
  assert.ok(validateTape(torn).some((v) => v.includes('not JSON')));
});
