// The stock credential scrub, checked against the vectors every runtime shares.
//
// spec/fixtures/credentials.json is the Python reference's output; this runtime must mask every
// case to the same bytes. Then the scrub is used as what it is for, a boundary's `scrub` and
// `forbid`, on a recorded call and its replay trace.

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as fr from '../src/index.js';
import { makeTools } from './traced-secret-app.mjs';

const { credentials, credentialsData, CREDENTIAL_SHAPES, MASK, secret } = fr.scrubs;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VECTORS = JSON.parse(
  fs.readFileSync(path.join(HERE, '..', '..', 'spec', 'fixtures', 'credentials.json'), 'utf8'),
);
const TEXTS = VECTORS.cases.filter((c) => 'text' in c);
const DATA = VECTORS.cases.filter((c) => 'data' in c);

test('the vectors hold both forms', () => {
  assert.ok(TEXTS.length > 0 && DATA.length > 0);
  assert.equal(MASK.source, VECTORS.mask);
});

test('every text case masks to the reference bytes', () => {
  for (const c of TEXTS) assert.equal(credentials(c.text, c.exempt), c.masked, c.text.slice(0, 200));
});

test('every data case masks to the reference data', () => {
  for (const c of DATA) assert.deepEqual(credentialsData(c.data, c.exempt), c.masked_data);
});

test('a mask is never masked again', () => {
  for (const c of TEXTS) assert.equal(credentials(c.masked, c.exempt), c.masked);
  for (const c of DATA) assert.deepEqual(credentialsData(c.masked_data, c.exempt), c.masked_data);
});

test('the shapes as forbid pass a scrubbed text', () => {
  // Whatever `credentials` leaves, no shape fires on, so declaring both never refuses a
  // recording the scrub already cleaned.
  const shapes = fr.boundaryOf({ forbid: CREDENTIAL_SHAPES }).forbid;
  for (const c of TEXTS) {
    const masked = credentials(c.text, c.exempt);
    assert.equal(shapes.find((p) => p.test(masked)), undefined, masked.slice(0, 200));
  }
  const raw = 'sk-ant-' + 'api03-abcdefghijklmnopqrstu';
  assert.ok(shapes.some((p) => p.test(JSON.stringify({ note: raw }))));
});

test('a mask is the first 8 hex of the UTF-8 sha256', () => {
  // The non-ASCII case pins UTF-8 (Python: hashlib.sha256('é'.encode()).hexdigest()[:8]).
  assert.equal(secret(''), '<secret:e3b0c442>');
  assert.equal(secret('é'), '<secret:4a99557e>');
});

// --- as a boundary declares it: tape, trace, replay --------------------------------------------

const KEY = 'sk-live-' + '9f8e7d6c5b4a39281706';
let dir;

afterEach(() => {
  fr.uninstall();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
  dir = null;
});

test('a boundary declaring the scrub writes no credential and still replays', async () => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'flight-'));
  const boundary = fr.boundaryOf({ scrub: credentials, forbid: CREDENTIAL_SHAPES });
  const store = fr.wrap({ get: async () => ({ v: 1 }) }, ['get'], { prefix: 'kv' });
  fr.install(boundary, { directory: dir });
  await fr.tool('deriveKey', makeTools(store).deriveKey)({ email: 't@x.com' });
  const call = fr.pickCall(fr.loadTape(fr.tapePath()), { fn: 'deriveKey' });
  fr.uninstall();

  const dead = fr.wrap({ get: () => { throw new Error('the world was touched'); } }, ['get'], { prefix: 'kv' });
  const report = await fr.replayCall({
    call,
    fn: makeTools(dead).deriveKey,
    boundary,
    trace: ['traced-secret-app.mjs'],
  });
  assert.ok(report.ok, report.divergence?.message);
  const trace = JSON.stringify(report.trace.observations);
  assert.ok(!trace.includes(KEY));
  assert.ok(new RegExp(MASK.source).test(trace));
});
