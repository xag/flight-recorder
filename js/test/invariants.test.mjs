// Invariants: the "right?" question.
//
// The load-bearing claims:
//   - a claim about every execution is checked against a recording, and a violation names itself;
//   - it asserts over the REPLAYED trajectory, not the recorded one;
//   - it reaches internal variables through the trace — the form that catches a bug whose output
//     is perfectly self-consistent;
//   - and under a mutated (probe) tape it becomes a property test: the verdict rests on the
//     invariants, because the result is *supposed* to differ.

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

async function record(toolName, args) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const raw = new ToyStore();
  const store = fr.wrap(raw, ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  const boundary = BOUNDARY();
  fr.install(boundary, { directory: dir });

  const wrapped = fr.tool(toolName, makeTools(store)[toolName]);
  try {
    await wrapped(args);
  } catch {
    /* a tool that throws is still a recording */
  }
  const tapePath = fr.tapePath();
  fr.uninstall();
  return { tape: fr.loadTape(tapePath), tapePath, boundary };
}

/** The real code, over a FRESH store the check must never touch. */
function freshTool(toolName) {
  const store = fr.wrap(new ToyStore(), ['get', 'set', 'boom', 'plainBoom'], { prefix: 'store' });
  return makeTools(store)[toolName];
}

afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
});

// --- the verdict ----------------------------------------------------------------------

test('a claim that holds is held, and one that fails names itself', async () => {
  const { tape, boundary } = await record('greet', { user: 'alice' });

  const report = await fr.checkInvariants({
    tape,
    fnName: 'greet',
    fn: freshTool('greet'),
    boundary,
    invariants: [
      fr.invariant('the greeting names somebody', (t) => {
        assert.ok(t.result.name, 'no name in the greeting');
      }),
      fr.invariant('the greeting names bob', (t) => {
        assert.equal(t.result.name, 'Bob');
      }),
    ],
  });

  assert.equal(report.held, 1);
  assert.equal(report.violations.length, 1);
  assert.equal(report.violations[0].invariant, 'the greeting names bob');
  assert.equal(report.ok, false, 'one violation is enough to condemn the run');
  assert.match(fr.formatReport(report), /1 violation\(s\)/);
});

test('every claim holding is a green report', async () => {
  const { tape, boundary } = await record('greet', { user: 'alice' });

  const report = await fr.checkInvariants({
    tape,
    fnName: 'greet',
    fn: freshTool('greet'),
    boundary,
    invariants: [fr.invariant('a token was issued', (t) => assert.ok(t.result.token))],
  });

  assert.ok(report.ok);
  assert.equal(report.violations.length, 0);
  assert.match(fr.formatReport(report), /1 invariant\(s\) held/);
});

// --- what the trajectory exposes ------------------------------------------------------

test('the trajectory carries the kwargs, the boundary events and the code’s own claims', async () => {
  const { tape, boundary } = await record('enrol', { user: 'alice', password: 'hunter2' });

  const report = await fr.checkInvariants({
    tape,
    fnName: 'enrol',
    fn: freshTool('enrol'),
    boundary,
    invariants: [
      fr.invariant('the call was made for alice', (t) => assert.equal(t.kwargs.user, 'alice')),
      fr.invariant('the world was asked something', (t) => assert.ok(t.events.length > 0)),
      fr.invariant('the code claimed it enrolled', (t) => {
        const names = t.sems.map((s) => (Array.isArray(s) ? s[0] : s.name));
        assert.ok(names.includes('enrol'), `no enrol claim in ${JSON.stringify(names)}`);
      }),
    ],
  });

  assert.ok(report.ok, fr.formatReport(report));
  assert.equal(report.held, 3);
});

test('a tool that raised hands the invariant the error, and a null result', async () => {
  const { tape, boundary } = await record('halfway', { user: 'alice' });

  const report = await fr.checkInvariants({
    tape,
    fnName: 'halfway',
    fn: freshTool('halfway'),
    boundary,
    invariants: [
      fr.invariant('it failed, and said why', (t) => {
        assert.equal(t.result, null, 'a call that raised has no result');
        assert.match(t.error, /tool gave up/);
      }),
    ],
  });

  assert.ok(report.ok, fr.formatReport(report));
});

// --- the form that needs the trace ----------------------------------------------------

test('an invariant reaches an internal variable through the trace', async () => {
  const { tape, boundary } = await record('greet', { user: 'alice' });

  const report = await fr.checkInvariants({
    tape,
    fnName: 'greet',
    fn: freshTool('greet'),
    boundary,
    trace: ['toy.mjs'],
    invariants: [
      fr.invariant('the row fetched for a known user is never empty', (t) => {
        const seen = t.trace.values('row');
        assert.ok(seen.length > 0, `the trace never saw 'row' (saw: ${t.trace.names()})`);
        assert.ok(
          seen.some((o) => o.value && o.value !== 'null'),
          `row was never a document: ${JSON.stringify(seen.map((o) => o.value))}`,
        );
      }),
    ],
  });

  assert.ok(report.ok, fr.formatReport(report));
});

// --- mutation + invariant: a property test over the boundary --------------------------

test('under a probe the verdict rests on the invariants, not on matching the recording', async () => {
  const { tape, boundary } = await record('greet', { user: 'alice' });
  const call = structuredClone(fr.pickCall(tape, { fn: 'greet' }));

  // A world no traffic reached: the store answers nothing for a user it certainly knows.
  call.events[0].res = null;
  call.probe = true;

  const report = await fr.checkInvariants({
    call,
    fn: freshTool('greet'),
    boundary,
    probe: true,
    invariants: [
      fr.invariant('a greeting is always issued, even to a stranger', (t) => {
        assert.ok(t.result.name, 'the code produced no name at all');
      }),
    ],
  });

  assert.equal(report.replay.resultMatch, false, 'the mutation changed the answer — the point');
  assert.ok(report.ok, 'and the claim still holds in that world: ' + fr.formatReport(report));
});

test('a probe the tape cannot answer is reported as uncheckable, not as a violation', async () => {
  const { tape, boundary } = await record('greet', { user: 'alice' });
  const call = structuredClone(fr.pickCall(tape, { fn: 'greet' }));

  // The code asks for 4 random bytes; the edited tape now holds 2.
  call.events[1].n = 2;
  call.events[1].hex = 'abcd';
  call.probe = true;

  const report = await fr.checkInvariants({
    call,
    fn: freshTool('greet'),
    boundary,
    probe: true,
    invariants: [fr.invariant('never reached', () => assert.ok(true))],
  });

  assert.equal(report.ok, false);
  assert.ok(report.replay.divergence, 'the tape is wrong, not the program');
  assert.match(fr.formatReport(report), /could not check/);
});

// --- the tape is shared ---------------------------------------------------------------

test('Node invariants judge a tape recorded by another runtime', async () => {
  // The whole point of freezing the format: an invariant consumes the tape, so where the tape
  // came from is not its business. This one was written by the Go recorder.
  const goTape = fr.loadTape(
    fs.readFileSync(new URL('../../spec/fixtures/go-toy.jsonl', import.meta.url), 'utf8'),
  );
  assert.ok(goTape.calls.length > 0, 'the Go fixture has calls to judge');
  assert.ok(goTape.header.go, 'and it really was produced by the Go runtime');

  // No replay here — replaying Go means running Go. What crosses the boundary is the tape, and
  // a Node invariant can read every recorded answer in it.
  const call = goTape.calls[0];
  assert.ok(Array.isArray(call.events));
});
