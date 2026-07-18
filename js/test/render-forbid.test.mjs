// The forbid tripwire on the OTHER writer: the render tape.
//
// `RenderTape` opens its own file and writes its own session-format lines; nothing it emits
// passes through `Recorder.write`. So the recorder's tripwire says nothing about it, and a
// boundary that declared "this recording carries no credential" was telling the truth about one
// file while the design tape beside it held the value. That gap is what these tests close.
//
// NO BROWSER HERE, DELIBERATELY. `renderCall` needs a page; `RenderTape` needs a filename and an
// object. The claim under test is about what reaches disk, and disk is reachable without
// Chromium — so these run on every `npm test`, not only the opted-in ones. The captured layout
// below is a hand-written stand-in for one, and it is allowed to be: nothing here reads it, the
// tripwire only reads the line.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { RenderTape } from '../src/render.js';
import { ForbiddenValue } from '../src/errors.js';
import { validateTape } from '../src/spec/validate.js';

const KEY_SHAPE = /-----BEGIN [A-Z ]*PRIVATE KEY-----/;
const PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK7\n';

/** A fresh directory that does not exist yet, so "was anything created?" is answerable. */
function freshTape() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fr-render-forbid-'));
  return { dir, file: path.join(dir, 'nested', 'design.jsonl') };
}

/** The shape `renderCall` returns, minus the browser. */
function call(text) {
  return {
    ev: 'call',
    fn: 'render',
    kwargs: { state: 'settings', url: null, viewport: [600, 800], theme: 'light', reducedMotion: false },
    events: [],
    result: {
      doc: { title: 'Settings', dir: 'ltr', scrollWidth: 600, clientWidth: 600, scrollHeight: 800, clientHeight: 800 },
      ambient: { dpr: 1, dark: false, reducedMotion: false, fontsReady: 'loaded' },
      nodes: [{ p: 'body>main>code', tag: 'code', box: [0, 0, 600, 40], text, fs: 14, fw: 400, pad: [0, 0, 0, 0], disp: 'block', out: 'none 0px rgb(0,0,0)', shadow: 'none', backdrop: '#ffffff' }],
    },
    error: null,
    ms: 12.5,
  };
}

test('a forbidden value in a rendered node refuses the write — and the tape does not hold it', () => {
  const { dir, file } = freshTape();
  const tape = new RenderTape(file, { forbid: [KEY_SHAPE] });

  // A page can paint a credential. `text` is whatever the DOM said, and no redact rule sees it:
  // there is no field name to name, only the bytes the browser laid out.
  assert.throws(() => tape.write(call(PRIVATE_KEY)), ForbiddenValue);

  const written = fs.readFileSync(file, 'utf8');
  assert.ok(!written.includes('PRIVATE KEY'), 'the credential is nowhere on the render tape');
  assert.ok(!written.includes('MIIBOgIBAAJBAK7'), 'nor is the body of it');
  assert.deepEqual(
    written.split('\n').filter(Boolean).map(JSON.parse).map((l) => l.ev),
    ['session'],
    'the refused line was never appended — the header is all there is',
  );
  assert.deepEqual(validateTape(written), [], 'and what is left is still a conformant tape');

  fs.rmSync(dir, { recursive: true, force: true });
});

test('a credential in the header means no render tape is created at all', () => {
  const { dir, file } = freshTape();

  // The header is a line like any other and it is written by the constructor. The claim is not
  // that an error was raised — it is that there is no file to go and delete afterwards, and not
  // even the directory that would have held it.
  assert.throws(
    () => new RenderTape(file, { constants: { 'config.KEY': PRIVATE_KEY }, forbid: [KEY_SHAPE] }),
    ForbiddenValue,
  );

  assert.equal(fs.existsSync(file), false, 'no tape was created to hold it');
  assert.deepEqual(fs.readdirSync(dir), [], 'not even the directory it would have lived in');

  fs.rmSync(dir, { recursive: true, force: true });
});

test('the error names the rule and points at the fix — never the match', () => {
  const { dir, file } = freshTape();
  const tape = new RenderTape(file, { forbid: [KEY_SHAPE] });

  assert.throws(() => tape.write(call(PRIVATE_KEY)), (e) => {
    assert.equal(e.name, 'ForbiddenValue');
    assert.match(e.message, /BEGIN \[A-Z \]\*PRIVATE KEY/, 'it names the pattern that fired');
    assert.match(e.message, /nothing was written/i);
    assert.match(e.message, /redact|widen a rule/, 'and says what to do about it');
    assert.ok(!e.message.includes('MIIBOgIBAAJBAK7'), 'a tripwire that quotes the secret IS the leak');
    return true;
  });

  fs.rmSync(dir, { recursive: true, force: true });
});

test('a render tape that declares no forbid is completely unaffected', () => {
  const { dir, file } = freshTape();
  const tape = new RenderTape(file);

  // Free when unused. Every render tape written before the tripwire existed declares none, and
  // must keep behaving exactly as it did — including for content that would trip a rule someone
  // else declared.
  const a = tape.write(call(PRIVATE_KEY));
  const b = tape.write(call('hello'));

  assert.deepEqual([a.seq, b.seq], [1, 2]);
  const written = fs.readFileSync(file, 'utf8');
  assert.equal(written.split('\n').filter(Boolean).length, 3, 'header plus both calls');
  assert.ok(written.includes('PRIVATE KEY'), 'nothing was withheld from a tape that asked for nothing');
  assert.deepEqual(validateTape(written), []);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('a clean render passes a declared tripwire untouched, and the sequence stays contiguous', () => {
  const { dir, file } = freshTape();
  const tape = new RenderTape(file, { forbid: [KEY_SHAPE, /\b[a-f0-9]{64}\b/] });

  assert.equal(tape.write(call('Save changes')).seq, 1);
  assert.throws(() => tape.write(call(PRIVATE_KEY)), ForbiddenValue);
  // A refused write consumed no sequence number. Had it done so, the next line would land at 3
  // and the tape would read as though a record went missing — which is a different, and false,
  // story about what happened.
  assert.equal(tape.write(call('Cancel')).seq, 2);

  const written = fs.readFileSync(file, 'utf8');
  assert.deepEqual(
    written.split('\n').filter(Boolean).map(JSON.parse).filter((l) => l.ev === 'call').map((l) => l.seq),
    [1, 2],
  );
  assert.deepEqual(validateTape(written), []);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('a bad render-tape pattern fails at declaration, not at the first render', () => {
  const { dir, file } = freshTape();

  // Compiled when the tape is declared, so a rule that does not parse is loud in the line that
  // wrote it — rather than looking installed and being inert until the render that mattered.
  assert.throws(() => new RenderTape(file, { forbid: ['(unclosed'] }), (e) => {
    assert.match(e.message, /bad forbid pattern/);
    assert.match(e.message, /\(unclosed/, 'and says which one');
    return true;
  });
  assert.equal(fs.existsSync(file), false);

  fs.rmSync(dir, { recursive: true, force: true });
});

test("a boundary's already-compiled patterns thread straight through", () => {
  const { dir, file } = freshTape();

  // The intended wiring: one declaration, both writers. `boundaryOf` compiles its patterns, and
  // handing those compiled RegExps back to `compileForbid` must be a no-op rather than a second
  // compilation that quietly changes them.
  const forbid = [/\b[a-f0-9]{64}\b/g]; // global, as a hand-written rule often is
  const tape = new RenderTape(file, { forbid });

  const digest = 'a'.repeat(64);
  assert.throws(() => tape.write(call(digest)), ForbiddenValue);
  // Twice: a global regex carries lastIndex between calls, so an uncompiled one would stop
  // matching on the second line and the tripwire would silently go quiet mid-session.
  assert.throws(() => tape.write(call(digest)), ForbiddenValue);
  assert.ok(!fs.readFileSync(file, 'utf8').includes(digest));

  fs.rmSync(dir, { recursive: true, force: true });
});
