// Semantic events in Node (issue #25): the app's testimony, recorded in-stream next to the
// evidence, replayed as a claim rather than an answer — and the whole thing at parity with the
// Python implementation across the one frozen tape format.
//
// The library gains no semantics from any of this. These tests assert only what a recorder may
// assert: that the claim was written down, in order, next to the raw events it encloses, and
// scrubbed like every other payload. Whether the claim is TRUE is a reader's question.

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as fr from '../src/index.js';
import { ToyStore, ToyError, makeTools } from './toy.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(HERE, '..', '..', 'spec', 'fixtures');

let dir;

const BOUNDARY = () =>
  fr.boundaryOf({
    redact: { password: null },
    errorRevivers: { ToyError: ([msg, n]) => new ToyError(msg, n) },
  });

function install({ boundary = BOUNDARY(), gate = null } = {}) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const raw = new ToyStore();
  const store = fr.wrap(raw, ['get', 'set', 'boom'], { prefix: 'store' });
  fr.install(boundary, { directory: dir, gate });
  const impls = makeTools(store);
  const tools = Object.fromEntries(
    Object.entries(impls).map(([name, fn]) => [name, fr.tool(name, fn)]),
  );
  return { raw, store, impls, tools };
}

const readTape = () => fs.readFileSync(fr.tapePath(), 'utf8');
const calls = () => readTape().split('\n').filter(Boolean).map(JSON.parse).filter((l) => l.ev === 'call');
const allEvents = () => calls().flatMap((c) => c.events);
const sems = () => allEvents().filter((e) => e.k === 'sem');

afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
  dir = null;
});

// --- off is free, and silent ---------------------------------------------------------

test('note and span are strict no-ops when nothing is installed', () => {
  // Instrumentation lives in production code paths: uninstalled it must cost nothing and have
  // no failure modes at all.
  fr.note('nothing_is_recording', { n: 1 });
  const out = fr.span('still_nothing', { k: 'v' }, () => 41 + 1);
  assert.equal(out, 42, 'the body still runs and its result still comes back');
  assert.equal(fr.tapePath(), null);
});

test('an async span is a no-op when off, and still returns its awaited value', async () => {
  const out = await fr.span('off', async () => 7);
  assert.equal(out, 7);
});

test('they are no-ops when the gate declines', async () => {
  const { tools } = install({ gate: () => false });
  await tools.enrol({ user: 'alice', password: 'x' });
  assert.deepEqual(calls(), [], 'a span must not be the thing that conjures a recorded call');
});

test('a span outside any call records nothing', async () => {
  install();
  await fr.span('orphan', async () => {
    fr.note('orphan_note');
  });
  // Spans are call-scoped: with no tool call in flight there is nowhere on the tape to go.
  assert.deepEqual(sems(), []);
});

// --- what gets written ---------------------------------------------------------------

test('a span encloses the raw events it produced, in order', async () => {
  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'x' });

  const stream = allEvents().map((e) => [e.k, e.name ?? e.fn]);
  // The clock read happens while the span's args are evaluated — before it opens — so it belongs
  // to the call, not the span. Then the outermost span opens and closes last.
  assert.deepEqual(stream[0], ['now', undefined]);
  assert.deepEqual(stream[1], ['sem', 'enrol']);
  assert.deepEqual(stream[stream.length - 1], ['sem', 'enrol']);

  // The chained read sits INSIDE load_corpus; the store.set and store.boom inside register.
  const names = stream.map(([, n]) => n);
  const lo = names.indexOf('load_corpus');
  const hi = names.lastIndexOf('load_corpus');
  assert.ok(stream.slice(lo, hi).some(([k]) => k === 'db'));
});

test('begin/end pair by sid and nest; sids are unique within the call', async () => {
  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'x' });

  const stack = [];
  for (const e of sems()) {
    if (e.phase === 'begin') stack.push(e.sid);
    else if (e.phase === 'end') assert.equal(stack.pop(), e.sid, 'spans do not nest');
  }
  assert.equal(stack.length, 0, 'a span was left open');

  const ids = sems().filter((e) => e.phase !== 'end').map((e) => e.sid);
  assert.equal(ids.length, new Set(ids).size, 'sids are not unique within the call');
});

test('the end carries outcome ok, and error when the body raised', async () => {
  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'x' });

  const byKey = new Map(sems().map((e) => [`${e.name}:${e.phase}`, e]));
  assert.equal(byKey.get('register:end').outcome, 'error');
  assert.equal(byKey.get('enrol:end').outcome, 'ok');
  assert.ok(byKey.has('registration_failed:point'));
});

test('a raising body still closes its span with outcome error and re-raises', async () => {
  const { store } = install();
  const boom = fr.tool('boom', async ({ user }) =>
    fr.span('doomed', async () => {
      await store.boom(user); // ToyError, uncaught
    }),
  );
  await assert.rejects(() => boom({ user: 'ghost' }), /no such key/);

  const ends = sems().filter((e) => e.phase === 'end');
  assert.deepEqual(ends.map((e) => e.outcome), ['error']);
});

test('note carries its data; a sync span records and returns', async () => {
  const { store } = install();
  const t = fr.tool('t', async ({ user }) => {
    const n = fr.span('compute', { user }, () => 3);
    fr.note('computed', { n });
    await store.get(user);
    return n;
  });
  const out = await t({ user: 'alice' });
  assert.equal(out, 3);

  const point = sems().find((e) => e.name === 'computed');
  assert.equal(point.phase, 'point');
  assert.deepEqual(point.data, { n: 3 });
});

// --- testimony is scrubbed exactly like evidence --------------------------------------

test('sem data goes through redact, like every other payload', async () => {
  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'hunter2' });

  assert.ok(!readTape().includes('hunter2'), 'the secret is nowhere on the tape');
  const data = sems().filter((e) => 'data' in e).map((e) => e.data);
  assert.ok(data.some((d) => d.password === '[REDACTED]'), 'a password rode a sem event in the clear');
});

// --- conformance ---------------------------------------------------------------------

test('the recorder writes a conformant sem tape, exercising every phase and both outcomes', async () => {
  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'hunter2' });

  assert.deepEqual(fr.validateTape(readTape()), [], 'the sem tape violates the frozen spec');

  const s = sems();
  assert.deepEqual(new Set(s.map((e) => e.phase)), new Set(['begin', 'end', 'point']));
  assert.deepEqual(new Set(s.filter((e) => e.phase === 'end').map((e) => e.outcome)), new Set(['ok', 'error']));
  assert.ok(s.filter((e) => e.phase === 'begin').length >= 3, 'the fixture must carry a span inside a span');
  // A value marker had to reach sem data, exercising the encoder.
  assert.ok(s.some((e) => e.data?.started?.__dt__), 'no value marker in any sem data');
});

// --- replay --------------------------------------------------------------------------

/** Record enrol, then hand back the tape and a fresh boundary for replaying against. */
async function recordEnrol() {
  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'hunter2' });
  const tape = fr.loadTape(fr.tapePath());
  const boundary = BOUNDARY();
  fr.uninstall();
  return { tape, boundary };
}

/** Build a fresh, unwrapped enrol the replay drives — the store it never touches. */
function freshEnrol() {
  const store = fr.wrap(new ToyStore(), ['get', 'set', 'boom'], { prefix: 'store' });
  return makeTools(store).enrol;
}

test('a sem tape replays green, and the sems are consumed not mistaken for answers', async () => {
  const { tape, boundary } = await recordEnrol();
  const report = await fr.replayCall({ call: fr.pickCall(tape, { fn: 'enrol' }), fn: freshEnrol(), boundary });

  assert.ok(report.ok, report.divergence?.message);
  assert.equal(report.divergence, null);
  assert.equal(report.unconsumed, 0, 'the sems were consumed, not left over as a shorter path');
  assert.equal(report.semDivergence, null);
  assert.deepEqual(report.semsRecorded, report.semsReplayed);
  assert.ok(report.semsRecorded.some(([n, p]) => n === 'enrol' && p === 'begin'));
});

test('a pre-sem tape replays exactly as before: no sem field interferes', async () => {
  const { tools } = install();
  await tools.greet({ user: 'alice' });
  const tape = fr.loadTape(fr.tapePath());
  const boundary = BOUNDARY();
  fr.uninstall();

  const store = fr.wrap(new ToyStore(), ['get', 'set', 'boom'], { prefix: 'store' });
  const report = await fr.replayCall({ call: fr.pickCall(tape, { fn: 'greet' }), fn: makeTools(store).greet, boundary });

  assert.ok(report.ok);
  assert.deepEqual(report.semsRecorded, []);
  assert.deepEqual(report.semsReplayed, []);
  assert.equal(report.semDivergence, null);
});

test('a changed account is named but does not fail the replay by default', async () => {
  const { tape, boundary } = await recordEnrol();

  // enrol, refactored: the same boundary questions in the same order — only the load_corpus span
  // is gone. Every existing signal stays green; the only change is the code's account of itself.
  const store = fr.wrap(new ToyStore(), ['get', 'set', 'boom'], { prefix: 'store' });
  const refactored = async ({ user, password }) => {
    const at = new Date();
    return fr.span('enrol', { user, started: at, password }, async () => {
      // the load_corpus span is gone; the question it asked is unchanged
      const row = await fr.queryOne('get', `collection("users").document("${user}")`, () =>
        fr.snapshot(user, { name: 'Alice', x: 3 }),
      );
      fr.note('corpus_read', { found: row.exists });
      try {
        await fr.span('register', { password }, async () => {
          await store.set(`user:${user}`, { password });
          await store.boom(user);
        });
      } catch (e) {
        fr.note('registration_failed', { why: e.message });
      }
      return { user, name: row.data?.name ?? 'stranger' };
    });
  };

  const report = await fr.replayCall({ call: fr.pickCall(tape, { fn: 'enrol' }), fn: refactored, boundary });
  assert.ok(report.ok, 'a changed span must not fail a replay by default');
  assert.equal(report.divergence, null);
  assert.equal(report.unconsumed, 0);
  assert.ok(report.semDivergence, 'the changed account is a signal, even when it does not gate');
  assert.match(report.semDivergence, /load_corpus/);
  assert.match(report.semDivergence, /semantic divergence at 1/);

  // ...and semStrict folds it into the verdict, once a suite's vocabulary has settled.
  const strict = await fr.replayCall({ call: fr.pickCall(tape, { fn: 'enrol' }), fn: refactored, boundary, semStrict: true });
  assert.equal(strict.ok, false);
  assert.ok(strict.semDivergence);

  // ...and semStrict still says nothing about a tape whose claims did not change.
  const unchanged = await fr.replayCall({ call: fr.pickCall(tape, { fn: 'enrol' }), fn: freshEnrol(), boundary, semStrict: true });
  assert.ok(unchanged.ok);
});

// --- freeze the node sem fixture, for the PYTHON checker and Recording.spans() ---------

test('regenerate the node sem fixture (FR_REGEN_FIXTURES=1)', async (t) => {
  if (!process.env.FR_REGEN_FIXTURES) return t.skip('set FR_REGEN_FIXTURES=1 to regenerate');

  const { tools } = install();
  await tools.enrol({ user: 'alice', password: 'hunter2' });
  const text = readTape();
  assert.deepEqual(fr.validateTape(text), []);
  fs.mkdirSync(FIXTURES, { recursive: true });
  fs.writeFileSync(path.join(FIXTURES, 'node-sem-toy.jsonl'), text, 'utf8');
});
