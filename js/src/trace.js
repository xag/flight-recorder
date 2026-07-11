// Variable-level tracing: every local, on every executed line, of the code you name.
//
// This is the thing that turns "what was `level` when it went wrong?" from an inference into a
// lookup. Python gets it from `sys.settrace` for free. Node has no such hook — but it does have
// the V8 Inspector, which is where a debugger gets the same information, and that is enough.
//
// The mechanics and their sharp edge live in trace.worker.js. What matters here: tracing is for
// REPLAY. It pauses the isolate on every traced line, which costs milliseconds per line — fine
// when you are resurrecting one recorded execution to find out what it did, unthinkable in a
// request path. Recording stays cheap; understanding is where you spend.

import { Worker } from 'node:worker_threads';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const WORKER = new URL('./trace.worker.js', import.meta.url);

/**
 * One observation of the local scope, at one line.
 * @typedef {{file: string, line: number, fn: string, vars: Record<string, unknown>}} Observation
 */

/** The trace of a run: the observations, and the queries worth making of them. */
export class Trace {
  constructor(observations = []) {
    this.observations = observations;
  }

  get length() {
    return this.observations.length;
  }

  /**
   * The timeline of one variable — every value it ever held, in order, with where.
   *
   * This is the query the whole apparatus exists to answer. A self-consistent output tells you
   * nothing about the internal value that produced it; the timeline tells you everything.
   */
  values(name) {
    const out = [];
    let last;
    for (const o of this.observations) {
      if (!(name in o.vars)) continue;
      const value = o.vars[name];
      // Only report CHANGES: a variable unchanged across forty lines is forty lines of noise.
      const key = JSON.stringify(value);
      if (key === last) continue;
      last = key;
      out.push({ value, at: `${short(o.file)}:${o.line}`, fn: o.fn, line: o.line });
    }
    return out;
  }

  /** Every distinct variable the trace ever saw. */
  names() {
    const s = new Set();
    for (const o of this.observations) for (const k of Object.keys(o.vars)) s.add(k);
    return [...s].sort();
  }

  /** A readable timeline, for a human or a failure message. */
  render(name) {
    const vs = this.values(name);
    if (!vs.length) return `${name}: never observed`;
    return vs.map((v) => `  ${v.at.padEnd(28)} ${name} = ${JSON.stringify(v.value)}`).join('\n');
  }
}

const short = (url) => {
  try {
    return path.basename(fileURLToPath(url));
  } catch {
    return url;
  }
};

/**
 * Run `fn` on this thread with the named files traced.
 *
 * @param {Function} fn                 the code to run and observe
 * @param {object} o
 * @param {(string|RegExp)[]} o.include which files to trace — a path fragment or a regex.
 *   Trace what you are investigating, not the world: every traced line costs a pause.
 * @returns {Promise<{result: unknown, error: Error|null, trace: Trace}>}
 */
export async function traced(fn, { include }) {
  const patterns = include.map((p) => (p instanceof RegExp ? p.source : escapeRegex(String(p))));

  const worker = new Worker(WORKER, { workerData: { include: patterns } });
  worker.unref();

  // The main thread's inspector back-end is serviced on the MAIN thread's event loop. While we
  // wait for the worker to attach, this thread must keep turning — otherwise the attach never
  // completes and both sides wait for each other. (Learned the hard way: an idle `await` here
  // deadlocks.)
  const keepalive = setInterval(() => {}, 5);

  let result;
  let error = null;
  let observations = [];

  try {
    const ready = await once(worker);
    if (ready.error) throw new Error(`tracer failed to attach: ${ready.error}`);

    try {
      result = await fn();
    } catch (e) {
      error = e;
    }

    worker.postMessage('stop');
    ({ observations } = await once(worker));
  } finally {
    clearInterval(keepalive);
    await worker.terminate();
  }

  return { result, error, trace: new Trace(observations) };
}

const once = (worker) =>
  new Promise((resolve, reject) => {
    worker.once('message', resolve);
    worker.once('error', reject);
  });

const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
