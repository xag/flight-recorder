// The render boundary, against a real browser.
//
// A real browser, and never a DOM stub. The whole claim of the render module is that the CASCADE
// is a computation only the browser has run — so an instrument that asked a hand-rolled fake what
// a colour composited to would be relocated guessing, dressed up as a measurement.
//
// PLAYWRIGHT IS OPT-IN. It is an optional peer dependency, not a devDependency: the library
// imports no browser automation library, and nobody who installs flight-recorder — or who clones
// it to change the recorder — should have to download a browser to run `npm test`. So this file
// SKIPS when playwright is absent, and says exactly how to opt in:
//
//     npm run browser        # installs playwright + chromium, then `npm test` covers this too
//
// CI opts in (see .github/workflows/test.yml), because a claim nothing ever checks is not a claim.
// A skip that is invisible is how an instrument quietly stops being one, so this one is loud.
//
// `RECORD=1 node --test test/render.test.mjs` also regenerates the committed fixture tape that the
// Python invariant tests assert over. Every fixture must have been produced by an implementation.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { pathToFileURL } from 'node:url';

import { renderCall, RenderTape } from '../src/render.js';
import { validateTape } from '../src/spec/validate.js';

const FIXTURES = path.resolve(import.meta.dirname, '..', '..', 'tests', 'fixtures');
const PAGE = pathToFileURL(path.join(FIXTURES, 'defects.html')).href;

const OUT = process.env.RECORD === '1'
  ? path.join(FIXTURES, 'design-defects.jsonl')
  : path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'fr-render-')), 'defects.jsonl');

// The optional peer, loaded by name so its absence is a skip and not a crash.
const playwright = await import('playwright').catch(() => null);

test('a render is a tape, and the tape holds what only the browser knows', { skip: playwright ? false : 'playwright is not installed — run `npm run browser` to check the render boundary too' }, async (t) => {
  const browser = await playwright.chromium.launch();
  const page = await browser.newPage();
  t.after(() => browser.close());

  const tape = new RenderTape(OUT);
  const call = tape.write(
    await renderCall(page, { state: 'defects', url: PAGE, viewport: { w: 600, h: 800 }, theme: 'light' }),
  );

  // It IS a tape. Not a new format, not a sidecar — the same v1 line the recorder writes, so every
  // reader that already reads tapes reads this one, and the analysis engine stays written once.
  assert.deepEqual(validateTape(fs.readFileSync(OUT, 'utf8')), []);
  assert.equal(call.fn, 'render');

  const at = (suffix) => call.result.nodes.find((n) => n.p.endsWith(suffix));
  const faded = at('p.faded');
  const chip = at('span.chip');
  const card = at('div.card:nth(2)');

  // The colour the stylesheet asked for, and the colour the eye receives. A linter only ever sees
  // the first, which is why it passes this page.
  assert.equal(faded.col, '#1a1c20');
  assert.notEqual(faded.ink, faded.col);
  assert.equal(faded.alpha, 0.3);

  // The panel's background lives in a media query that does not match, so it never painted and the
  // chip is on white. No source file says so.
  assert.equal(chip.backdrop, '#ffffff');

  // The sentence did not fit, and nothing asked for an ellipsis, so it is simply cut.
  assert.ok(card.over[1] > 0, 'the card should report vertical overflow');
  assert.equal(card.ell, undefined);

  // The default button background is #f0f0f0 and no stylesheet on this page says so. The browser's
  // own UA sheet is part of the cascade, and only the browser has run it.
  assert.equal(at('button.icon:nth(3)').backdrop, '#f0f0f0');

  // Focus, under a REAL Tab — because :focus-visible does not match a programmatic focus() on a
  // link, and an instrument that focused elements itself would cry wolf on a good page.
  const ghost = call.result.focus.find((f) => f.p.endsWith('button.ghost:nth(1)'));
  assert.equal(ghost.ring, false, 'outline:none with no replacement is not a ring');
  assert.ok(call.result.focus.every((f) => !f.p.includes('fake-button')),
    'a role=button with tabindex=-1 is never reached by Tab');

  // ...and it is still operable, which is the whole point: unreachable, not absent.
  assert.equal(at('div.fake-button:nth(3)').act, true);
  assert.equal(at('button.icon:nth(3)').name, '');
});
