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

// --- the off-box sink ---------------------------------------------------------------------

test('a sink receives the whole session, and is AWAITED before the call returns', async () => {
  // The awaiting is the point. On a serverless host the instance is frozen the moment the
  // response goes out, so a publish left in flight is a publish that never happened.
  const published = [];
  let inFlight = false;

  const sink = {
    async publish(name, text) {
      inFlight = true;
      await new Promise((r) => setTimeout(r, 5)); // a real network hop
      published.push({ name, text });
      inFlight = false;
    },
  };

  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});
  fr.install(boundary, { directory: dir, sink });

  await fr.tool('t', async () => ({ ok: 1 }))({});

  assert.equal(inFlight, false, 'the call did not return until the sink had finished');
  assert.ok(published.length >= 1);

  const last = published.at(-1);
  assert.match(last.name, /^flight-.*\.jsonl$/);
  assert.deepEqual(fr.validateTape(last.text), [], 'what the sink got is a conformant tape');
  assert.equal(fr.loadTape(last.text).calls.length, 1, 'and it contains the call');
  fr.uninstall();
});

test('with no directory, the sink IS the tape (serverless: the filesystem dies with you)', async () => {
  let latest = null;
  const sink = { publish: (_n, text) => { latest = text; } };

  const boundary = fr.boundaryOf({});
  fr.install(boundary, { directory: null, sink });

  await fr.tool('t', async () => ({ n: Date.now() }))({});
  fr.uninstall();

  assert.ok(latest, 'the session reached the sink with nothing written to disk');
  const tape = fr.loadTape(latest);
  assert.equal(tape.calls.length, 1);
  assert.equal(tape.calls[0].events[0].k, 'now');
  assert.deepEqual(fr.validateTape(latest), []);
});

test('a sink that throws is never the reason a call fails', async () => {
  const sink = { publish: () => { throw new Error('the bucket is on fire'); } };
  fr.install(fr.boundaryOf({}), { directory: null, sink });

  const out = await fr.tool('t', async () => ({ ok: true }))({});
  fr.uninstall();

  assert.deepEqual(out, { ok: true }, 'the app carried on, which is the only acceptable outcome');
});

// --- concurrency: events are recorded in ASK order, not answer order ------------------------

test('a concurrent fan-out records in the order the questions were ASKED', async () => {
  // Found by replaying a PRODUCTION tape. listArticles does
  // `Promise.all(ids.map(id => kv.hgetall(id)))` — ordinary code. Recording each event when
  // its promise SETTLED produced a tape in completion order, while replay asks in issue
  // order, so the two diverged mid-fan-out. It looked perfect on a toy that made one call at
  // a time.
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});

  // Answers come back in the REVERSE of the order they were asked.
  const slow = {
    async get(key) {
      const delay = { a: 30, b: 20, c: 1 }[key];
      await new Promise((r) => setTimeout(r, delay));
      return { key };
    },
  };
  const store = fr.wrap(slow, ['get'], { prefix: 'kv' });
  fr.install(boundary, { directory: dir });

  const impl = async () => ({ rows: await Promise.all(['a', 'b', 'c'].map((k) => store.get(k))) });
  await fr.tool('t', impl)({});

  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 't' });
  fr.uninstall();

  assert.deepEqual(
    call.events.map((e) => e.args[0]),
    ['a', 'b', 'c'],
    'ask order — NOT c, b, a, which is the order the answers arrived in',
  );

  // …and therefore it replays.
  const dead = fr.wrap({ get: () => { throw new Error('the world was touched'); } }, ['get'], { prefix: 'kv' });
  const replayImpl = async () => ({ rows: await Promise.all(['a', 'b', 'c'].map((k) => dead.get(k))) });
  const report = await fr.replayCall({ call, fn: replayImpl, boundary });

  assert.equal(report.divergence, null, report.divergence?.message);
  assert.ok(report.ok);
  assert.deepEqual(report.result.rows.map((r) => r.key), ['a', 'b', 'c']);
});

test('replaying a tape does not switch off the recorder', async () => {
  // It used to. replayCall() shimmed the clock for the duration of the replay and cleaned up
  // with uninstall(), which also tore down the RECORDER — so the next real call went
  // unrecorded, silently, and only a test that recorded AFTER a replay ever noticed.
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});
  fr.install(boundary, { directory: dir });

  await fr.tool('one', async () => ({ t: Date.now() }))({});
  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 'one' });

  const report = await fr.replayCall({ call, fn: async () => ({ t: Date.now() }), boundary });
  assert.ok(report.ok, 'the replay itself worked');

  // …and recording is still on.
  await fr.tool('two', async () => ({ t: Date.now() }))({});
  const names = fr.loadTape(fr.tapePath()).calls.map((c) => c.fn);
  fr.uninstall();

  assert.deepEqual(names, ['one', 'two'], 'the call made after the replay is still on the tape');
});

test('tape names are unique — two recorders in the same second do not collide', async () => {
  // Timestamp-to-the-second + pid looked unique and was not. On Vercel, separate functions run
  // in separate containers that reuse low pids and start in the same second, so two of them
  // chose the same name and the sink silently overwrote one tape with the other. It cost a
  // moderator's recording before anyone noticed.
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));

  const names = new Set();
  for (let i = 0; i < 20; i++) {
    fr.install(fr.boundaryOf({}), { directory: dir });
    names.add(path.basename(fr.tapePath()));
    fr.uninstall();
  }

  assert.equal(names.size, 20, 'twenty recorders in the same second produced twenty names');
  for (const n of names) assert.match(n, /^flight-\d{8}T\d{6}-\d+-[0-9a-f]{8}\.jsonl$/);
});

test('naming a tape does not put a rand event on it', async () => {
  // The nonce must come from the captured RNG, not the shim — otherwise the recorder writes a
  // draw the app never made, and consumes one on replay.
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({});
  fr.install(boundary, { directory: dir });

  const { report, call } = await (async () => {
    const impl = async () => ({ ok: 1 });
    await fr.tool('t', impl)({});
    const c = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 't' });
    fr.uninstall();
    return { report: await fr.replayCall({ call: c, fn: impl, boundary }), call: c };
  })();

  assert.deepEqual(call.events, [], 'a call that asks nothing records nothing');
  assert.ok(report.ok);
});

// --- the sink must never be able to hurt the app -------------------------------------------

test('defer takes publishing off the critical path — the call returns before the sink lands', async () => {
  // Publishing is telemetry, and telemetry must not sit between a user and their response.
  // A host that offers waitUntil will keep the instance alive for it; the app should not wait.
  let landed = false;
  const sink = {
    async publish() {
      await new Promise((r) => setTimeout(r, 60));
      landed = true;
    },
  };

  const pending = [];
  const defer = (p) => pending.push(p); // stands in for waitUntil

  fr.install(fr.boundaryOf({}), { directory: null, sink, defer });
  await fr.tool('t', async () => ({ ok: 1 }))({});

  assert.equal(landed, false, 'the call did NOT wait for the sink');

  await Promise.all(pending); // …and the host kept it alive
  assert.equal(landed, true, 'and the tape still landed');
  fr.uninstall();
});

test('without defer the publish is awaited — a lost tape is worse than a slow response', async () => {
  let landed = false;
  const sink = { async publish() { await new Promise((r) => setTimeout(r, 20)); landed = true; } };

  fr.install(fr.boundaryOf({}), { directory: null, sink });
  await fr.tool('t', async () => ({ ok: 1 }))({});

  assert.equal(landed, true, 'on a host that freezes at response time, awaiting is the only honest fallback');
  fr.uninstall();
});

test('a sink that HANGS cannot hold the request open', async () => {
  // The sharp one. A throwing sink was always swallowed; a hanging sink used to block until the
  // platform killed the function — a slow Redis becoming a slow site. A recorder that can take
  // the app down with it has failed at its first duty, which is to be ignorable.
  const sink = { publish: () => new Promise(() => {}) }; // never settles. ever.

  fr.install(fr.boundaryOf({}), { directory: null, sink, sinkTimeoutMs: 50 });

  const t0 = performance.now();
  const out = await fr.tool('t', async () => ({ ok: true }))({});
  const elapsed = performance.now() - t0;
  fr.uninstall();

  assert.deepEqual(out, { ok: true }, 'the app got its answer');
  assert.ok(elapsed < 1000, `and it did not wait for a sink that never answers (${Math.round(elapsed)}ms)`);
});
