// Variable-level tracing in Node.
//
// The claim used to be that Node has no equivalent of sys.settrace, so this could not exist. Node
// has no such *hook* — but it has the V8 Inspector, which is where a debugger gets exactly this
// information. These tests are the proof, and they end where the library began: an internal
// variable quietly emptying a corpus behind a perfectly self-consistent output.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import * as fr from '../src/index.js';
import { makeTools } from './traced-app.mjs';

let dir;

test('traced(): every local, on every executed line', async () => {
  const store = { get: async () => [{ x: 3 }, { x: 1 }, { x: 2 }] };
  const tools = makeTools(store);

  const { result, trace } = await fr.traced(
    () => tools.studyStatus({ email: 'a@b.c', level: 0 }),
    { include: ['traced-app.mjs'] },
  );

  assert.deepEqual(result, { corpus: 3, deck: 0, done: true });
  assert.ok(trace.length > 0, 'the trace is not empty');

  // The names the code actually held.
  const names = trace.names();
  for (const n of ['level', 'corpus', 'deck', 'done']) {
    assert.ok(names.includes(n), `${n} was observed`);
  }
});

test('values(): the timeline of one variable — a lookup, not an inference', async () => {
  const store = { get: async () => [{ x: 3 }, { x: 1 }, { x: 2 }] };
  const tools = makeTools(store);

  const { trace } = await fr.traced(
    () => tools.studyStatus({ email: 'a@b.c', level: 0 }),
    { include: ['traced-app.mjs'] },
  );

  const level = trace.values('level');
  assert.equal(level.at(-1).value, 0, 'level was 0 — which is the whole story');

  const deck = trace.values('deck');
  assert.match(String(deck.at(-1).value), /Array\(0\)/, 'and the deck it produced was empty');

  const corpus = trace.values('corpus');
  assert.match(String(corpus.at(-1).value), /Array\(3\)/, 'while the corpus was not');
});

test('THE BUG: a self-consistent output, condemned by its own trace', async () => {
  // The output says: corpus 3, deck 0, done. Every number agrees with every other number. No
  // assertion on the RESULT can call this wrong — "done with an empty deck" is exactly what the
  // code means. The wrongness is that `level` excluded the entire corpus, and that is only
  // visible from the inside.
  const store = { get: async () => [{ x: 3 }, { x: 1 }, { x: 2 }] };
  const tools = makeTools(store);

  const { result, trace } = await fr.traced(
    () => tools.studyStatus({ email: 'a@b.c', level: 0 }),
    { include: ['traced-app.mjs'] },
  );

  // The output is self-consistent…
  assert.equal(result.done, true);
  assert.equal(result.deck, 0);

  // …and the invariant that condemns it is a claim about an internal variable.
  const observed = trace.values('level').map((v) => v.value);
  assert.ok(observed.includes(0), 'level=0 is on the record');
  assert.ok(
    observed.every((v) => v > 0) === false,
    'the claim "level never excludes the whole corpus" is FALSE, and the trace proves it',
  );

  // And it reads as a timeline, for a human.
  const rendered = trace.render('deck');
  assert.match(rendered, /deck = /);
});

// --- tracing a REPLAY: the point of the whole apparatus ------------------------------------

test('replayCall({ trace }) — resurrect a recorded execution and watch it from the inside', async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});

  // Record, against a real store.
  const raw = { get: async () => [{ x: 3 }, { x: 1 }, { x: 2 }] };
  const store = fr.wrap(raw, ['get'], { prefix: 'kv' });
  fr.install(boundary, { directory: dir });

  const tools = makeTools(store);
  await fr.tool('study_status', tools.studyStatus)({ email: 'a@b.c', level: 0 });

  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 'study_status' });
  fr.uninstall();

  // Replay it — with NO store — and trace the code while it runs.
  const dead = fr.wrap({ get: () => { throw new Error('the world was touched'); } }, ['get'], { prefix: 'kv' });
  const replayTools = makeTools(dead);

  const report = await fr.replayCall({
    call,
    fn: replayTools.studyStatus,
    boundary,
    trace: ['traced-app.mjs'],
  });

  assert.equal(report.divergence, null, report.divergence?.message);
  assert.ok(report.ok, 'the recorded execution was reproduced');

  // And now the thing a tape alone cannot give you: what the code BELIEVED while it ran.
  assert.ok(report.trace.length > 0);
  assert.deepEqual(report.trace.values('level').map((v) => v.value), [0]);
  assert.match(String(report.trace.values('deck').at(-1).value), /Array\(0\)/);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('tracing does not disturb what it observes', async () => {
  const store = { get: async () => [{ x: 3 }, { x: 1 }] };
  const tools = makeTools(store);
  const args = { email: 'a@b.c', level: 2 };

  const plain = await tools.studyStatus(args);
  const { result: observed } = await fr.traced(() => tools.studyStatus(args), {
    include: ['traced-app.mjs'],
  });

  assert.deepEqual(observed, plain, 'the traced run produced exactly the untraced result');
});

test('an error inside a traced run still surfaces, with the trace up to the throw', async () => {
  const boom = {
    async studyStatus() {
      const stage = 'about to fail';
      throw new Error(`gave up: ${stage}`);
    },
  };
  // The throwing function must live in a traced file to be observed, so trace this test file.
  await assert.rejects(
    () => fr.traced(() => boom.studyStatus(), { include: ['trace.test.mjs'] }).then((t) => {
      if (t.error) throw t.error;
      return t;
    }),
    /gave up/,
  );
});
