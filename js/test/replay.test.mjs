// Replay: resurrection, not re-enactment.
//
// The load-bearing claims:
//   - the real code re-runs and produces the SAME answer, off the tape alone;
//   - it does so without touching the world at all;
//   - and if the code asks a different question than the tape holds, that is CAUGHT rather
//     than silently answered — because a replay that quietly answered the wrong question
//     would look like it worked, which is worse than useless.

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import * as fr from '../src/index.js';
import { ToyStore, ToyError, makeTools } from './toy.mjs';

let dir;

const BOUNDARY = () =>
  fr.boundaryOf({
    redact: { password: null },
    errorRevivers: { ToyError: ([msg, n]) => new ToyError(msg, n) },
  });

/** Record one call and hand back the tape plus the raw (unwrapped) pieces. */
async function record(toolName, args, { boundary = BOUNDARY() } = {}) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const raw = new ToyStore();
  const store = fr.wrap(raw, ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  fr.install(boundary, { directory: dir });

  const impls = makeTools(store);
  const wrapped = fr.tool(toolName, impls[toolName]);

  let threw = null;
  let result;
  try {
    result = await wrapped(args);
  } catch (e) {
    threw = e;
  }
  const tapePath = fr.tapePath();
  fr.uninstall();

  return { tape: fr.loadTape(tapePath), tapePath, raw, impls, store, threw, result, boundary };
}

/** Replay a recorded call against the real code, with a FRESH store it must never touch. */
async function replay({ tape, boundary, toolName, probe = false, call = null }) {
  const fresh = new ToyStore();
  const store = fr.wrap(fresh, ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  const impls = makeTools(store);

  const report = await fr.replayCall({
    call: call ?? fr.pickCall(tape, { fn: toolName }),
    fn: impls[toolName],
    boundary,
    probe,
  });
  return { report, fresh };
}

afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
});

// --- the round trip -------------------------------------------------------------------

test('the real code re-runs off the tape and gives the same answer', async () => {
  const rec = await record('greet', { user: 'alice' });
  const { report } = await replay({ ...rec, toolName: 'greet' });

  assert.equal(report.divergence, null);
  assert.ok(report.ok, 'result and error both match the recording');
  assert.ok(report.resultMatch);

  // and it is genuinely the same execution, not merely a similar one: the clock and the
  // dice came off the tape too.
  const recorded = fr.pickCall(rec.tape, { fn: 'greet' });
  assert.equal(report.result.token, recorded.result.token);
  assert.equal(report.result.at, recorded.result.at);
});

test('replay never touches the world', async () => {
  const rec = await record('greet', { user: 'alice' });
  assert.equal(rec.raw.writes.length, 1, 'the recording really did write');

  const { report, fresh } = await replay({ ...rec, toolName: 'greet' });

  assert.ok(report.ok);
  assert.equal(fresh.writes.length, 0, 'the replay wrote nothing — no db, no network');
});

test('a recorded error is revived with its real TYPE, so the code branches the same way', async () => {
  const rec = await record('explode', { user: 'ghost' });
  assert.ok(rec.threw instanceof ToyError);

  const { report } = await replay({ ...rec, toolName: 'explode' });

  assert.equal(report.divergence, null);
  assert.ok(report.errorMatch, 'the tool raised on replay exactly as it did when recorded');
  assert.match(report.error, /ToyError/);
});

test('a revived plain Error carries its MESSAGE, not its stack', async () => {
  const rec = await record('report', { user: 'ghost' });

  // What the app actually saw when the world refused it.
  assert.equal(rec.result.why, 'fetch failed: upstream refused: ghost');

  const { report } = await replay({ ...rec, toolName: 'report' });

  assert.equal(report.divergence, null);
  assert.ok(
    report.ok,
    'the code read `e.message` off the revived error and got the same sentence back. Rebuilt from '
    + '`repr` — the recorded STACK — it would read `Error: upstream refused: ghost\\n    at ...` '
    + 'instead, and every app that logs or returns a caught message would diverge on replay.',
  );
  assert.equal(report.result.why, 'fetch failed: upstream refused: ghost');
});

test('a tool that threw replays as having thrown', async () => {
  const rec = await record('halfway', { user: 'alice' });
  const { report } = await replay({ ...rec, toolName: 'halfway' });

  assert.ok(report.ok);
  assert.match(report.error, /tool gave up/);
});

test('a redacted tape still replays (the rules are idempotent)', async () => {
  const rec = await record('signup', { email: 'a@b.c', password: 'hunter2' });
  const { report } = await replay({ ...rec, toolName: 'signup' });

  assert.equal(report.divergence, null, 'the masked arg scrubs to itself, so it compares');
  assert.ok(report.ok);
  assert.equal(report.result.password, '[REDACTED]', 'the secret never came back');
});

// --- divergence: the most useful failure the library can produce -----------------------

test('a different question is caught, not silently answered', async () => {
  const rec = await record('greet', { user: 'alice' });

  // The code changed: it now looks up a different key. Every recorded answer is still
  // there and still plausible — only the QUESTION moved.
  const fresh = new ToyStore();
  const store = fr.wrap(fresh, ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  const changed = async ({ user }) => {
    await store.get(`v2:${user}`); // was: store.get(user)
    return {};
  };

  const report = await fr.replayCall({
    call: fr.pickCall(rec.tape, { fn: 'greet' }),
    fn: changed,
    boundary: rec.boundary,
  });

  assert.ok(report.divergence, 'the divergence is the finding');
  assert.match(report.divergence.message, /different question/);
  assert.match(report.divergence.message, /v2:alice/, 'and it says what was asked');
  assert.match(report.divergence.message, /"alice"/, 'and what was recorded');
});

test('asking a different EFFECT is caught', async () => {
  const rec = await record('greet', { user: 'alice' });

  const fresh = new ToyStore();
  const store = fr.wrap(fresh, ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  const changed = async ({ user }) => { await store.set(user, 1); }; // set, not get

  const report = await fr.replayCall({
    call: fr.pickCall(rec.tape, { fn: 'greet' }),
    fn: changed,
    boundary: rec.boundary,
  });

  assert.ok(report.divergence);
  assert.match(report.divergence.message, /store\.set/);
  assert.match(report.divergence.message, /store\.get/);
});

test('the code that stops asking is caught too — the sneaky one', async () => {
  // Everything "passes": no wrong answer is ever given. The code simply quietly stopped
  // doing some of its work. Unconsumed answers are the only evidence.
  const rec = await record('greet', { user: 'alice' });

  const fresh = new ToyStore();
  const store = fr.wrap(fresh, ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  const lazy = async ({ user }) => {
    await store.get(user); // and then... nothing. No write, no clock, no dice.
    return {};
  };

  const report = await fr.replayCall({
    call: fr.pickCall(rec.tape, { fn: 'greet' }),
    fn: lazy,
    boundary: rec.boundary,
  });

  assert.ok(report.divergence, 'a silent under-execution is still a divergence');
  assert.match(report.divergence.message, /stopped asking/);
  assert.equal(report.unconsumed, 3, 'rand, now and the write went unasked');
});

// --- edit the tape to visit a world that never happened ---------------------------------

test('editing the tape re-runs the real code against a world that never happened', async () => {
  const rec = await record('greet', { user: 'alice' });
  const call = structuredClone(fr.pickCall(rec.tape, { fn: 'greet' }));

  // ToyStore can never answer null for alice — no real recording could produce this. One
  // edit, and the unreachable branch is reachable.
  call.events[0].res = null;
  call.probe = true;

  const { report } = await replay({ ...rec, toolName: 'greet', call, probe: true });

  assert.equal(report.divergence, null, 'the mutation is not a divergence — it is the point');
  assert.equal(report.result.name, 'stranger', 'the real code took the branch no traffic reached');
  assert.equal(report.resultMatch, false, 'and it no longer matches the recording, as it should not');
});

test('a mutated tape that cannot answer says so, plainly', async () => {
  const rec = await record('greet', { user: 'alice' });
  const call = structuredClone(fr.pickCall(rec.tape, { fn: 'greet' }));

  // The code asks for 4 random bytes; the edited tape now holds 2.
  call.events[1].n = 2;
  call.events[1].hex = 'abcd';
  call.probe = true;

  const { report } = await replay({ ...rec, toolName: 'greet', call, probe: true });

  assert.ok(report.divergence, 'the tape is wrong, not the program — and it says which');
  assert.match(report.divergence.message, /asked for 4 random bytes but the tape holds 2/);
});

// --- the tape is portable ----------------------------------------------------------------

test('a tape reloaded from disk replays identically', async () => {
  const rec = await record('greet', { user: 'alice' });

  // Not the in-memory object: the bytes, off the filesystem, as a stored recording is.
  const reloaded = fr.loadTape(rec.tapePath);
  const { report } = await replay({ ...rec, tape: reloaded, toolName: 'greet' });

  assert.ok(report.ok);
});
