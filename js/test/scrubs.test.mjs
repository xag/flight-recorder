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

// --- a pair inside a JSON string, and unquoted assignments -------------------------------------
//
// What leaked: a transcript holds a file a tool read as a JSON string, so a pair reached the tape
// as `\"ALPACA_API_SECRET_KEY\": \"...\"`. The domain mirrors tests/test_scrubs.py.

const KEYS = ['ALPACA_API_KEY_ID', 'api_token', 'Authorization', 'password', 'session', 'key', 'token_env',
  'auth', 'author', 'keywords', 'command', 'url', 'DB'];
const EXEMPT = ['key', 'token_env', 'auth'];
const SK = 'sk-ant-' + 'api03-abcdefghijklmnopqrstu';
const GHP = 'ghp_' + 'abcdefghijklmnopqrstuvwxyz0123';
const AKIA = 'AKIA' + 'ABCDEFGHIJKLMNOP';
const BS = String.fromCharCode(92); // one backslash
const VALUES = ['PKRAW0001', 's3cr"et\\x', 'Bearer abc123def456', SK, GHP, AKIA, 'postgres://u:pw1234@h/db',
  'https://x.io/mcp?api_key=abc123&x=1', '${MY_TOKEN}', '{env:MY_TOKEN}', '', 'mcpServers',
  'use a Bearer token', 'é', '<secret:0123abcd>'];
const NESTABLE = VALUES.filter((v) => !/["\\\n]/.test(v));
const ASSIGNABLE = VALUES.filter((v) => v && !/[ \t\r\n"'`\\&;|<>,)}\]]/.test(v) && v[0] !== '=');
const ASSIGNMENTS = [
  (k, v) => `${k}=${v}`,
  (k, v) => `export ${k}=${v}`,
  (k, v) => `set ${k}=${v}`,
  (k, v) => `$env:${k} = ${v}`,
  (k, v) => `${k}="${v}"`,
  (k, v) => `${k}='${v}'`,
  (k, v) => `$env:${k} = "${v}"`,
  (k, v) => `--${k}=${v}`,
];
const product = (as, bs) => as.flatMap((a) => bs.map((b) => [a, b]));
const nest = (text, depth) => {
  for (let i = 0; i < depth; i++) text = JSON.stringify(text);
  return text;
};
// Whether a value under a credential field is kept as it is (empty, a reference, a mask).
const kept = (v) => credentialsData({ password: v }).password !== secret(v);

for (const depth of [1, 2, 3]) {
  for (const exempt of [[], EXEMPT]) {
    test(`a pair nested ${depth} JSON-string level(s) deep masks as its parse does (exempt: ${exempt.length})`, () => {
      for (const [k, v] of product(KEYS, NESTABLE)) {
        const inners = [
          JSON.stringify({ servers: { x: { [k]: v } } }, null, 2),
          `${JSON.stringify(k)} = ${JSON.stringify(v)}\n`,
        ];
        for (const inner of inners) {
          const outer = nest(inner, depth);
          const masked = credentials(outer, exempt);
          assert.equal(JSON.parse(masked), credentials(nest(inner, depth - 1), exempt), outer);
          assert.equal(credentials(masked, exempt), masked);
          // and the agreement is not two equally unmasked texts
          if (fr.scrubs.secretField(k, exempt) && !kept(v)) assert.notEqual(masked, outer, outer);
        }
      }
    });
  }
}

// The shape that leaked: a trace line read with a tool, held as the tool result's content in one
// transcript row; and that row once more inside the tape that recorded the transcript.
const KEY_VALUE = 'PKRAW' + '0001TESTVALUE';
const TRANSCRIPT_ROW = JSON.stringify({ type: 'user', message: { content: [
  { type: 'tool_result', tool_use_id: 't1', content: JSON.stringify(
    { e: 'L', d: { env: { ALPACA_API_SECRET_KEY: KEY_VALUE, PATH: '/usr/bin' } } }) }] } });

test('the transcript row that leaked is masked at depth 1 and 2', () => {
  for (const text of [TRANSCRIPT_ROW, JSON.stringify(TRANSCRIPT_ROW)]) {
    const masked = credentials(text);
    assert.ok(!masked.includes(KEY_VALUE) && masked.includes('/usr/bin'));
    assert.equal(credentials(masked), masked);
  }
});

test('each assignment form masks its value iff the name holds a credential', () => {
  for (const [[key, value], form] of product(product(KEYS, ASSIGNABLE), ASSIGNMENTS)) {
    const text = form(key, value) + ' # done';
    const expect = fr.scrubs.secretField(key) && !kept(value) ? secret(value) : credentials(value);
    assert.equal(credentials(text), form(key, expect) + ' # done', text);
  }
});

for (const depth of [0, 1, 2]) {
  test(`a dotenv file masks alike at depth ${depth}`, () => {
    const dotenv = product(product(KEYS, ASSIGNABLE), ASSIGNMENTS).map(([[k, v], form]) => form(k, v)).join('\n') + '\n';
    const outer = nest(dotenv, depth);
    let masked = credentials(outer);
    for (let i = 0; i < depth; i++) masked = JSON.parse(masked);
    assert.equal(masked, credentials(dotenv));
    assert.equal(credentials(credentials(outer)), credentials(outer));
  });
}

test('a comparison is not an assignment', () => {
  for (const text of ['if token == other:', 'token != x', 'password==hash(p)', 'token<=3']) {
    assert.equal(credentials(text), text);
  }
});

// Texts whose first pass once left something a second pass changed (tests/test_scrubs.py).
const SECOND_PASS = [
  "'" + AKIA + '=Bearer  = Bearer ,',               // a shape masked later freed an assignment
  'token = $env:' + AKIA + String.fromCharCode(92) + '"KEY', // a kept reference had a shape masked inside
  '{"token": "$' + AKIA + '"}',
  '{"token": "${' + AKIA + '}"}',
  AKIA + ' = "api_key": "v1"',                      // a pair left as it is swallowed the next pair's key
  'token="token": "{"x"',                           // a masked value read back as a key holds "secret"
  `$token='pw' = "api_key"Bearer`,
  // a later pass changed how an earlier one read the text (the passes now run to a fixed point)
  AKIA + ':""key":"y"',
  'token=v:"auth:"y"',
  'token=-=""KEY":"1"',
  'session=d:' + BS + '"passw:' + BS + '"y' + BS + '"',
  'token=_x="token="token": "\t)&{',
  'token="token": "_;x=" token="token": "0123456789',
];

test('the scrub is a no-op on its own output', () => {
  for (const text of SECOND_PASS) {
    const once = credentials(text);
    assert.equal(credentials(once), once, text);
    assert.ok(!once.includes(AKIA), text);
  }
});

// Dense texts built from the fragments the passes read, as tests/test_scrubs.py builds them.
const FRAGMENTS = [BS, BS + BS, BS + '"', BS + BS + BS + '"', '"', '""', "'", '`', '$', '$env:',
  '${X}', '%X%', '--', '==', '=', ' = ', ':', ': ', 'token', 'secretBearer', 'api_key',
  'password', 'a.b.', 'KEY', 'v1', AKIA, 'Bearer ', 'é', '\n', ' ', '\t', '<', '>', '&', '?',
  '://', '@', '{', '}', ')', ',', ';', '<secret:0123abcd>', '"api_key"', '${', 'a=', 'token=',
  "'pw'", 'x = ', '": "', '"token": "', 'x="', 'auth:', 'passw:'];

/** mulberry32: a small seeded generator, so a failure names a text that can be built again. */
function seeded(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test('the scrub is a fixed point over dense fragments', () => {
  const rand = seeded(17);
  const pick = (n) => Math.floor(rand() * n);
  for (let i = 0; i < 20000; i++) {
    let text = '';
    for (let j = 1 + pick(16); j > 0; j--) text += FRAGMENTS[pick(FRAGMENTS.length)];
    const exempt = i % 2 ? ['password'] : [];
    const once = credentials(text, exempt);
    assert.equal(credentials(once, exempt), once, JSON.stringify(text));
  }
});

test('an unchanged assignment gives its value back to the scan', () => {
  assert.equal(credentials('a=token=abc123'), 'a=token=' + secret('abc123'));
});

test('a pair left as it is gives back a value that opens a pair', () => {
  assert.equal(credentials('x = "api_key": "v1"'), 'x = "api_key": ' + `"${secret('v1')}"`);
  const escaped = String.fromCharCode(92) + '"';
  const text = `${escaped}x${escaped}: ${escaped}api_key${escaped}: ${escaped}v1${escaped}`;
  assert.ok(!credentials(text).includes('v1'));
});

test('a long string of escaped JSON scrubs in linear time', () => {
  // A transcript is one long string of escaped JSON. When every escaped quote could open a quoted
  // token running to the string's end, 60 KB took 9 s in the Python reference.
  let seed = 3;
  const next = () => (seed = (seed * 1103515245 + 12345) % 2147483648);
  const alphabet = ['"', 'a', 'b', ' ', ':'];
  let inner = '';
  for (let i = 0; i < 50000; i++) inner += alphabet[next() % alphabet.length];
  for (const text of [JSON.stringify(inner), JSON.stringify(JSON.stringify(inner))]) {
    const t = performance.now();
    credentials(text);
    const ms = performance.now() - t;
    assert.ok(ms < 1000, `${text.length} chars took ${ms.toFixed(0)} ms`);
  }
});

test('a reference opening an unquoted value is kept', () => {
  for (const text of ['TOKEN=${MY_TOKEN}', 'TOKEN={env:MY_TOKEN}', 'TOKEN=%MY_TOKEN%', 'TOKEN=$MY_TOKEN']) {
    assert.equal(credentials(text), text);
  }
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
