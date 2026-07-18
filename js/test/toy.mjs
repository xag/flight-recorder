// A toy app for the recorder's tests: a client with methods (the Upstash/Redis shape),
// plus the clock and the RNG. Deliberately semantics-free — record/replay fidelity is all
// that is being tested, so the store answers from canned rows.

import crypto from 'node:crypto';

import { note, span, query, queryOne, exec, sampleIndices, snapshot } from '../src/index.js';

export class ToyError extends Error {
  constructor(msg, n) {
    super(msg);
    this.name = 'ToyError';
    this.args = [msg, n];
  }
}

/** The raw client. Everything the app knows about the world goes through this. */
export class ToyStore {
  constructor() {
    this.rows = { alice: { name: 'Alice', x: 3 }, bob: { name: 'Bob', x: 1 } };
    this.writes = [];
  }

  async get(key) {
    return this.rows[key] ?? null;
  }

  async set(key, value) {
    this.writes.push([key, value]);
    return 'OK';
  }

  async boom(key) {
    throw new ToyError(`no such key: ${key}`, 42);
  }

  // A PLAIN Error — which is what every real client throws. ToyError above carries an explicit
  // `.args`, and so was the one shape the recorder happened to get right.
  async plainBoom(key) {
    throw new Error(`upstream refused: ${key}`);
  }
}

/** The tools, built over an injected (and therefore wrappable) store. */
export function makeTools(store) {
  return {
    // clock + RNG + two effect calls, one of which writes
    async greet({ user }) {
      const row = await store.get(user);
      const token = crypto.randomBytes(4).toString('hex');
      const at = Date.now();
      await store.set(`greeted:${user}`, { at, token });
      return { name: row?.name ?? 'stranger', token, at };
    },

    // an effect that throws: the fx.err branch
    async explode({ user }) {
      await store.boom(user);
    },

    // The shape that actually happens in the wild: a client throws a plain Error, the app catches
    // it and puts `e.message` into what it returns. If revival rebuilds the error from its repr —
    // the stack — this returns 300 characters of `at ClientRequest.<anonymous>` instead of a
    // sentence, and the tool's result diverges for a reason that has nothing to do with the app.
    async report({ user }) {
      try {
        await store.plainBoom(user);
        return { ok: true };
      } catch (e) {
        return { ok: false, why: `fetch failed: ${e.message}` };
      }
    },

    // a tool that itself throws after a successful effect: call.error
    async halfway({ user }) {
      await store.get(user);
      throw new Error('tool gave up');
    },

    // secrets on every surface redaction must cover: a tool kwarg, an effect arg,
    // an effect result field, and a tool result field
    async signup({ email, password }) {
      await store.set(`user:${email}`, { password });
      const row = { email, password };
      return { account: row, password };
    },

    // The instrumented tool: the same work as the others, but saying what it MEANT. Every shape
    // a sem event can take is here — spans nested inside a span, a point note, a span whose body
    // raises (recorded with outcome "error", the exception caught by the caller), span data
    // carrying a value marker (a datetime) and a value a redaction must reach (a password).
    async enrol({ user, password }) {
      const at = new Date(); // a clock read before the span opens: it belongs to the call
      return span('enrol', { user, started: at, password }, async () => {
        // A chained read, not an effect: the canonical scenario puts a `db` event inside a span,
        // which is the one enclosure a reader most wants to see and the one an fx-only span
        // never demonstrates.
        const row = await span('load_corpus', () =>
          queryOne('get', `collection("users").document("${user}")`, () =>
            snapshot(user, { name: 'Alice', x: 3 }),
          ),
        );
        note('corpus_read', { found: row.exists });

        try {
          await span('register', { password }, async () => {
            await store.set(`user:${user}`, { password });
            await store.boom(user); // raises: the span ends with outcome "error"
          });
        } catch (e) {
          note('registration_failed', { why: e.message });
        }

        return { user, name: row.data?.name ?? 'stranger' };
      });
    },
  };
}

/**
 * The canonical fixture scenario — the same shape every runtime records into
 * `spec/fixtures/*-toy.jsonl`, so the six tapes differ only in the runtime key and the
 * timestamps.
 *
 * Kept apart from `makeTools` on purpose. That one is Node's own app toy, shaped by what this
 * suite needs to test; this one is shaped by what the CROSS-RUNTIME fixture has to prove, and
 * the two pull in different directions. Conflating them is how a fixture quietly drifts to suit
 * a local test.
 */
export function makeCanonicalTools(store) {
  return {
    // An effect, a chained read, all four random shapes, both clocks, and a chained write:
    // every event kind the format defines, on one tape.
    async greet({ user }) {
      const row = await store.get(user);

      await query('stream', 'collection("users").where("x", ">", 0)', () => [
        snapshot('0', { name: 'alpha', x: 1 }),
        snapshot('1', { name: 'beta', x: 2 }),
      ]);

      sampleIndices(3, 2);
      crypto.randomBytes(4);
      Math.random();
      crypto.randomInt(100);
      const at = new Date();
      performance.now();

      await exec('set', `store.set(greeted:${user})`, [{ at }], async () => {});

      return { name: row?.name ?? 'stranger' };
    },

    // A raising effect produces both an fx.err and a non-null call.error.
    async explode({ user }) {
      await store.boom(user);
    },
  };
}
