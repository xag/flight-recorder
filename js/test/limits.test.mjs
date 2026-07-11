// Every door the world comes through, closed — and proved closed by replaying.
//
// A half-shimmed door is worse than an open one, because it looks shut: code reaching for
// the un-shimmed form re-rolls on replay, silently, and the divergence points at a value
// instead of at the door it came through. So each of these tests records a call, replays it
// against a world that no longer exists, and asserts the answer is IDENTICAL. If a door were
// still open, the replayed value would differ and the test would fail.

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import * as fr from '../src/index.js';

let dir;

async function roundTrip(impl, args = {}) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});
  fr.install(boundary, { directory: dir });

  const recorded = await fr.tool('t', impl)(args);
  const tape = fr.loadTape(fr.tapePath());
  fr.uninstall();

  const report = await fr.replayCall({ call: fr.pickCall(tape, { fn: 't' }), fn: impl, boundary });
  return { recorded, report, call: fr.pickCall(tape, { fn: 't' }) };
}

afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
});

// --- the clock ------------------------------------------------------------------------

test('new Date() is shimmed — not just Date.now()', async () => {
  const { recorded, report, call } = await roundTrip(async () => ({
    viaNow: Date.now(),
    viaCtor: new Date().toISOString(),
  }));

  assert.deepEqual(call.events.map((e) => e.k), ['now', 'now'], 'both ways asked the clock');
  assert.ok(report.ok);
  assert.equal(report.result.viaCtor, recorded.viaCtor, 'new Date() came off the tape, not the clock');
  assert.equal(report.result.viaNow, recorded.viaNow);
});

test('new Date(args) is arithmetic, not a question — it stays deterministic and unrecorded', async () => {
  const { report, call } = await roundTrip(async () => ({
    fixed: new Date('2020-01-01T00:00:00Z').toISOString(),
    fromMs: new Date(1577836800000).getUTCFullYear(),
  }));

  assert.deepEqual(call.events, [], 'a Date built from arguments asks the world nothing');
  assert.ok(report.ok);
  assert.equal(report.result.fixed, '2020-01-01T00:00:00.000Z');
});

test('shimming Date does not break instanceof — the shim stays invisible', async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  fr.install(fr.boundaryOf({}), { directory: dir });

  // A Date the app makes now, and one made by code holding the ORIGINAL Date (the hazard:
  // a subclassed global would fail instanceof for every pre-existing Date in the process).
  const fresh = new Date();
  const revived = fr.fromJsonable({ __dt__: '2020-01-01T00:00:00.000Z' });

  assert.ok(fresh instanceof Date, 'a Date made under the shim is a Date');
  assert.ok(revived instanceof Date, 'and so is one made with the captured original');
  assert.equal(typeof fresh.toISOString(), 'string');
  fr.uninstall();
});

test('performance.now() is a DIFFERENT clock, and gets its own event', async () => {
  const { recorded, report, call } = await roundTrip(async () => ({ t: performance.now() }));

  assert.deepEqual(call.events.map((e) => e.k), ['perf'], 'not a wall-clock `now`');
  assert.ok(report.ok);
  assert.equal(report.result.t, recorded.t, 'the monotonic clock came off the tape too');
});

// --- randomness: every door -------------------------------------------------------------

test('Math.random() is shimmed', async () => {
  const { recorded, report, call } = await roundTrip(async () => ({ r: Math.random() }));

  assert.equal(call.events[0].k, 'rand');
  assert.equal(call.events[0].m, 'float');
  assert.ok(call.events[0].v >= 0 && call.events[0].v < 1);
  assert.ok(report.ok);
  assert.equal(report.result.r, recorded.r, 'the same draw, off the tape');
});

test('crypto.randomBytes — the CALLBACK form is recorded, not just the sync one', async () => {
  const impl = async () =>
    new Promise((resolve, reject) => {
      crypto.randomBytes(8, (err, buf) => (err ? reject(err) : resolve({ hex: buf.toString('hex') })));
    });

  const { recorded, report, call } = await roundTrip(impl);

  assert.equal(call.events[0].k, 'rand');
  assert.equal(call.events[0].m, 'bytes');
  assert.equal(call.events[0].n, 8);
  assert.ok(report.ok);
  assert.equal(report.result.hex, recorded.hex, 'the async draw replayed identically');
});

test('crypto.randomUUID is shimmed', async () => {
  const { recorded, report } = await roundTrip(async () => ({ id: crypto.randomUUID() }));
  assert.ok(report.ok);
  assert.equal(report.result.id, recorded.id);
  assert.match(report.result.id, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
});

test('crypto.randomInt is shimmed — a scalar draw, recorded as its value', async () => {
  const { recorded, report, call } = await roundTrip(async () => ({ n: crypto.randomInt(1, 1_000_000) }));

  assert.equal(call.events[0].m, 'int');
  assert.ok(Number.isInteger(call.events[0].v));
  assert.ok(report.ok);
  assert.equal(report.result.n, recorded.n);
});

test('crypto.randomFillSync is shimmed', async () => {
  const impl = async () => {
    const buf = Buffer.alloc(6);
    crypto.randomFillSync(buf);
    return { hex: buf.toString('hex') };
  };
  const { recorded, report, call } = await roundTrip(impl);

  assert.equal(call.events[0].m, 'bytes');
  assert.equal(call.events[0].n, 6);
  assert.ok(report.ok);
  assert.equal(report.result.hex, recorded.hex);
});

test('webcrypto getRandomValues is shimmed — what portable code reaches for', async () => {
  const impl = async () => {
    const arr = new Uint8Array(4);
    globalThis.crypto.getRandomValues(arr);
    return { hex: Buffer.from(arr).toString('hex') };
  };
  const { recorded, report, call } = await roundTrip(impl);

  assert.equal(call.events[0].m, 'bytes');
  assert.equal(call.events[0].n, 4);
  assert.ok(report.ok);
  assert.equal(report.result.hex, recorded.hex);
});

// --- undefined ---------------------------------------------------------------------------

test('undefined survives the round trip — it is not flattened onto null', async () => {
  const impl = async () => ({ a: undefined, b: null, c: 1 });
  const { report, call } = await roundTrip(impl);

  // On the tape, the two nothings are distinguishable.
  assert.deepEqual(call.result, { a: { __undef__: true }, b: null, c: 1 });

  assert.ok(report.ok);
  assert.equal(report.result.a, undefined);
  assert.equal(report.result.b, null);
  assert.ok('a' in report.result, 'present-and-undefined is not the same as absent');
});

test('a function returning undefined is not one returning null', async () => {
  const undef = await roundTrip(async () => undefined);
  const nul = await roundTrip(async () => null);

  assert.deepEqual(undef.call.result, { __undef__: true });
  assert.equal(nul.call.result, null);
  assert.notDeepEqual(undef.call.result, nul.call.result, 'the tape keeps them apart');
});

test('undefined in effect arguments and results survives too', async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});
  const raw = { look: (_a, _b) => undefined };
  const client = fr.wrap(raw, ['look']);
  fr.install(boundary, { directory: dir });

  const impl = async () => ({ got: client.look('x', undefined) });
  await fr.tool('t', impl)({});

  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 't' });
  fr.uninstall();

  assert.deepEqual(call.events[0].args, ['x', { __undef__: true }], 'the undefined ARG is on the tape');
  assert.deepEqual(call.events[0].res, { __undef__: true }, 'and so is the undefined RESULT');

  const freshClient = fr.wrap({ look: () => 'WRONG — the world was touched' }, ['look']);
  const replayImpl = async () => ({ got: freshClient.look('x', undefined) });
  const report = await fr.replayCall({ call, fn: replayImpl, boundary });

  assert.equal(report.divergence, null);
  assert.equal(report.result.got, undefined, 'and it replays as undefined, not null');
});

// --- the recorder must not record itself --------------------------------------------------

test('the recorder never writes clock events the app did not ask for', async () => {
  // It stamps every line with the time and measures every call's duration. If those calls
  // went through its own shims, the tape would be full of phantom events — and replay would
  // consume them, diverging on the first real question.
  const { report, call } = await roundTrip(async () => ({ x: 1 }));

  assert.deepEqual(call.events, [], 'a call that asks nothing records nothing');
  assert.ok(report.ok);
});

// --- scrub: the values a field-name rule cannot see ----------------------------------------

test('scrub sweeps values a field rule cannot see — positional args, keys, prose', async () => {
  const ADDRESS = /[\w.+-]+@[\w-]+\.[\w.-]+/g;
  const hide = (s) => (typeof s === 'string' ? s.replace(ADDRESS, 'user@hidden') : s);

  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({ scrub: hide });
  const raw = { has: () => true, put: () => 'OK' };
  const store = fr.wrap(raw, ['has', 'put'], { prefix: 'kv' });
  fr.install(boundary, { directory: dir });

  const impl = async ({ email }) => {
    await store.has('allowlist', email);        // POSITIONAL — no field name to match
    await store.put(`user:${email}`, { note: `welcome ${email}` }); // in a KEY, and in PROSE
    return { greeting: `hi ${email}` };
  };

  await fr.tool('t', impl)({ email: 'writer@example.com' });
  const text = fs.readFileSync(fr.tapePath(), 'utf8');
  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 't' });
  fr.uninstall();

  assert.ok(!text.includes('writer@example.com'), 'the address is nowhere on the tape');
  assert.deepEqual(call.events[0].args, ['allowlist', 'user@hidden'], 'positional arg swept');
  assert.equal(call.events[1].args[0], 'user:user@hidden', 'inside a key, swept');
  assert.equal(call.events[1].args[1].note, 'welcome user@hidden', 'inside prose, swept');
  assert.equal(call.result.greeting, 'hi user@hidden');
});

test('a scrubbed recording still REPLAYS — the sweep is consistent under derivation', async () => {
  // This is the subtle one. Masking an INPUT poisons everything derived from it: the tape
  // holds a key built from the RAW address, while replay — handed the mask — builds a key
  // from the MASK, and the two no longer match. A substring sweep does not have that
  // problem, because `user:${addr}` scrubs to exactly what the replayed code builds out of
  // the scrubbed `addr`.
  const ADDRESS = /[\w.+-]+@[\w-]+\.[\w.-]+/g;
  const hide = (s) => (typeof s === 'string' ? s.replace(ADDRESS, 'user@hidden') : s);

  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({ scrub: hide });
  const store = fr.wrap({ get: () => ({ ok: true }) }, ['get'], { prefix: 'kv' });
  fr.install(boundary, { directory: dir });

  const impl = async ({ email }) => ({ found: await store.get(`user:${email}`) });
  await fr.tool('t', impl)({ email: 'writer@example.com' });

  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 't' });
  fr.uninstall();

  const dead = fr.wrap({ get: () => { throw new Error('the world was touched'); } }, ['get'], { prefix: 'kv' });
  const replayImpl = async ({ email }) => ({ found: await dead.get(`user:${email}`) });

  const report = await fr.replayCall({ call, fn: replayImpl, boundary });
  assert.equal(report.divergence, null, report.divergence?.message);
  assert.ok(report.ok, 'a pseudonymised tape replays — which is what makes it usable at all');
});

// --- a door's own internals stay behind the door -------------------------------------------

test("a wrapped client's internal clock/RNG calls never reach the tape", async () => {
  // The hazard, found by wiring a real app: @upstash/redis calls performance.now() inside
  // its own request path. That is the world's machinery on the far side of a boundary we are
  // already recording — not the app asking anything. Recorded, it becomes an answer to a
  // question nobody asks on replay (the real client never runs), and it surfaces as a
  // divergence on event 0 pointing at the app, which did nothing wrong.
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});

  const chatty = {
    async get(key) {
      performance.now();          // like a client timing its own request
      Date.now();                 // like a client stamping a retry
      Math.random();              // like a client jittering a backoff
      await new Promise((r) => setImmediate(r)); // and it does it across an await, too
      performance.now();
      return { key };
    },
  };
  const store = fr.wrap(chatty, ['get'], { prefix: 'kv' });
  fr.install(boundary, { directory: dir });

  const impl = async () => {
    const t = Date.now();          // the APP asking — this one counts
    const row = await store.get('k');
    return { t, row };
  };

  await fr.tool('t', impl)({});
  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 't' });
  fr.uninstall();

  assert.deepEqual(
    call.events.map((e) => e.k),
    ['now', 'fx'],
    "only the app's own clock call and the effect itself — none of the client's internals",
  );

  // …and it replays, which is the proof that mattered.
  const dead = fr.wrap({ get: () => { throw new Error('the world was touched'); } }, ['get'], { prefix: 'kv' });
  const replayImpl = async () => {
    const t = Date.now();
    const row = await dead.get('k');
    return { t, row };
  };
  const report = await fr.replayCall({ call, fn: replayImpl, boundary });
  assert.equal(report.divergence, null, report.divergence?.message);
  assert.ok(report.ok);
});
