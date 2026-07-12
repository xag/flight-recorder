// A toy app for the recorder's tests: a client with methods (the Upstash/Redis shape),
// plus the clock and the RNG. Deliberately semantics-free — record/replay fidelity is all
// that is being tested, so the store answers from canned rows.

import crypto from 'node:crypto';

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
  };
}
