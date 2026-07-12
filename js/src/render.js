// The render boundary — the tape is the computed layout, not the screenshot.
//
// WHY NOT PIXELS
//
// Every screenshot tool records pixels, and that one choice condemns it to being a REGRESSION
// oracle forever. A PNG can only ever answer "same?". It cannot answer "right?", because there is
// no claim you can write over a bitmap that a human did not already have to make by looking. It
// is also unstable (antialiasing, font hinting, GPU), undiffable in any way that names a cause,
// and enormous.
//
// So record what the browser COMPUTED instead: for every element, its box, its painted colour,
// the backdrop it actually sits on after the cascade and every translucent ancestor, its resolved
// font, whether its text overflowed its box, its role and accessible name, its focus style under
// a real Tab. That is JSONL. It is a tape. And the whole engine already built for code applies to
// it unchanged — pin it (regression), assert invariants over it (correctness), mutate it
// (a property test over hostile content).
//
// The decisive part is the backdrop. A stylesheet linter reads `color: var(--mut)` and
// `background: var(--card)` and computes a contrast ratio that is a FICTION: it does not know
// that an ancestor is 55% opaque, that the card never painted because a media query dropped it,
// that a fixed header is over the top. The browser knows. Ask the browser.
//
// WHAT A RENDER'S NONDETERMINISM IS
//
// The same code, the same data, and the layout still differs: viewport, colour scheme, reduced
// motion, locale and text direction, device pixel ratio, and — the one everybody forgets — which
// font actually resolved. Those are ambient inputs, and they are exactly a boundary. A render call
// names them in `kwargs`, so a tape says which world it was rendered in.
//
// THIS MODULE HAS NO DEPENDENCIES, AND THAT IS DELIBERATE
//
// `captureLayout` is a pure function of the document that runs IN THE PAGE — any driver that can
// evaluate a function will do (Playwright, Puppeteer, a devtools console). `renderCall` is typed
// by DUCK, not by import: it wants something with `setViewportSize`, `emulateMedia`, `goto`,
// `evaluate` and `keyboard`. Nothing here imports a browser automation library, so nothing that
// installs flight-recorder pays for one.
//
//   import { chromium } from 'playwright';
//   import { RenderTape, renderCall } from '@xag/flight-recorder/render';
//
//   const tape = new RenderTape('.flight/design.jsonl');
//   const page = await (await chromium.launch()).newPage();
//   for (const cell of matrix) tape.write(await renderCall(page, cell));
//
// The claims are then checked once, in Python, over the tape — because a tape is only data, and
// reading one needs no browser at all.

import fs from 'node:fs';
import path from 'node:path';

import { FORMAT_VERSION, isoLocal } from './record.js';

/**
 * Walk the rendered document and return what the browser computed. Runs IN THE PAGE.
 *
 * Self-contained on purpose: a driver hands this function to `page.evaluate`, which ships its
 * SOURCE across the process boundary. It may close over nothing — no imports, no module scope.
 */
export function captureLayout(opts = {}) {
  const O = { maxNodes: 2000, textLimit: 140, ...opts };

  const r1 = (n) => Math.round(n * 10) / 10;

  // --- colour: what actually painted -------------------------------------------------

  const parseColor = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    if (p.length < 3 || p.slice(0, 3).some(Number.isNaN)) return null;
    return [p[0], p[1], p[2], p.length > 3 && !Number.isNaN(p[3]) ? p[3] : 1];
  };

  const over = (fg, bg) => {
    const a = fg[3];
    return [
      fg[0] * a + bg[0] * (1 - a),
      fg[1] * a + bg[1] * (1 - a),
      fg[2] * a + bg[2] * (1 - a),
      1,
    ];
  };

  const hex = (c) =>
    c && '#' + c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');

  /**
   * The backdrop a node's own text is painted on.
   *
   * Climbs until something opaque, compositing every translucent layer on the way back down — and
   * gives up honestly (`image`) when a background image or gradient is in the stack, because a
   * contrast ratio against an unknown bitmap is a number nobody should trust.
   *
   * `opacity` on an ancestor is folded in too. It is the most common way a contrast calculation
   * done on the stylesheet is wrong, and the only way to see it is to be here.
   */
  const backdropOf = (el) => {
    const stack = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return { image: true, rgb: null };
      const c = parseColor(cs.backgroundColor);
      const op = parseFloat(cs.opacity);
      if (c && c[3] > 0) {
        const eff = [c[0], c[1], c[2], c[3] * (Number.isNaN(op) ? 1 : op)];
        stack.push(eff);
        if (eff[3] >= 1) break;
      }
    }
    // The canvas under everything. When nothing opaque was found the browser paints white.
    let base = [255, 255, 255, 1];
    for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
    return { image: false, rgb: [r1(base[0]), r1(base[1]), r1(base[2])] };
  };

  /** The colour the text is ACTUALLY painted in: its own alpha, and every ancestor's opacity. */
  const inkOf = (el, cs, backdrop) => {
    const c = parseColor(cs.color);
    if (!c) return null;
    let a = c[3];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const op = parseFloat(getComputedStyle(n).opacity);
      if (!Number.isNaN(op)) a *= op;
    }
    if (!backdrop.rgb) return { rgba: [r1(c[0]), r1(c[1]), r1(c[2]), r1(a)], rgb: null };
    const p = over([c[0], c[1], c[2], a], [...backdrop.rgb, 1]);
    return { rgba: [r1(c[0]), r1(c[1]), r1(c[2]), r1(a)], rgb: [r1(p[0]), r1(p[1]), r1(p[2])] };
  };

  // --- identity ------------------------------------------------------------------------

  /** A path that survives a re-render, so a claim (or a human's annotation) can be pinned. */
  const pathOf = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      let seg = n.localName;
      if (n.id) {
        parts.unshift(`${seg}#${n.id}`);
        break;
      }
      const cls = (n.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean);
      if (cls.length) seg += '.' + cls.join('.');
      const sibs = [...(n.parentElement?.children ?? [])].filter((s) => s.localName === n.localName);
      if (sibs.length > 1) seg += `:nth(${sibs.indexOf(n) + 1})`;
      parts.unshift(seg);
    }
    return parts.join('>');
  };

  // --- semantics -----------------------------------------------------------------------

  // What CLAIMS to be operable — regardless of whether it is reachable. A `role="button"` with
  // `tabindex="-1"` must count, because "reachable by mouse and not by keyboard" is precisely the
  // finding, and an instrument that filtered it out by definition could never report it.
  const OPERABLE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[contenteditable=""],[contenteditable=true]';
  const isOperable = (el) =>
    !el.hasAttribute('disabled') &&
    (el.matches(OPERABLE) || el.matches('[tabindex]:not([tabindex="-1"])'));

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const t = el.localName;
    if (t === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (t === 'button') return 'button';
    if (t === 'input') return el.type === 'submit' || el.type === 'button' ? 'button' : 'textbox';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'img') return 'img';
    if (t === 'nav') return 'navigation';
    if (t === 'main') return 'main';
    return null;
  };

  /** An approximation of the accessible name — enough to catch the ones that HAVE none. */
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria?.trim()) return aria.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const txt = by
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.textContent?.trim() ?? '')
        .filter(Boolean)
        .join(' ');
      if (txt) return txt;
    }
    if (el.localName === 'img') return (el.getAttribute('alt') ?? '').trim();
    if (el.localName === 'input') {
      const lab = el.labels?.[0]?.textContent?.trim();
      if (lab) return lab;
      const ph = el.getAttribute('placeholder');
      if (ph?.trim()) return ph.trim();
    }
    const title = el.getAttribute('title');
    // The subtree's text, minus anything explicitly hidden from the accessibility tree.
    const clone = el.cloneNode(true);
    clone.querySelectorAll('[aria-hidden=true]').forEach((n) => n.remove());
    const txt = (clone.textContent ?? '').replace(/\s+/g, ' ').trim();
    return txt || (title?.trim() ?? '');
  };

  /** Only the text this element paints itself — not its children's. That is what has a colour. */
  const ownText = (el) => {
    let s = '';
    for (const n of el.childNodes) if (n.nodeType === 3) s += n.nodeValue;
    return s.replace(/\s+/g, ' ').trim().slice(0, O.textLimit);
  };

  const hiddenFrom = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      if (n.getAttribute('aria-hidden') === 'true') return true;
      if (n.hasAttribute('hidden')) return true;
    }
    return false;
  };

  // --- the walk ------------------------------------------------------------------------

  const SKIP = new Set(['script', 'style', 'link', 'meta', 'title', 'head', 'noscript', 'template', 'br']);
  const nodes = [];
  let truncated = false;

  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  for (let el = document.body; el; el = walk.nextNode()) {
    if (SKIP.has(el.localName)) continue;
    if (nodes.length >= O.maxNodes) {
      truncated = true;
      break;
    }

    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue; // it has no box at all

    const b = el.getBoundingClientRect();
    const text = ownText(el);
    const interactive = isOperable(el);
    if (!text && !interactive && b.width === 0 && b.height === 0) continue;

    const backdrop = backdropOf(el);
    const ink = text ? inkOf(el, cs, backdrop) : null;

    const clipsX = /hidden|clip|auto|scroll/.test(cs.overflowX);
    const clipsY = /hidden|clip|auto|scroll/.test(cs.overflowY);

    nodes.push({
      p: pathOf(el),
      tag: el.localName,
      box: [r1(b.x), r1(b.y), r1(b.width), r1(b.height)],
      text: text || undefined,
      role: roleOf(el) ?? undefined,
      name: interactive || roleOf(el) === 'heading' || el.localName === 'img' ? nameOf(el) : undefined,
      act: interactive || undefined,
      hid: hiddenFrom(el) || undefined,
      // `col` is the colour the stylesheet asked for (what a palette claim is about); `ink` is
      // what the eye receives (what a contrast claim is about). They differ exactly when
      // something was translucent — which is the case a source-reading linter gets wrong.
      col: ink ? hex(ink.rgba.slice(0, 3)) : undefined,
      ink: ink?.rgb ? hex(ink.rgb) : undefined,
      alpha: ink && ink.rgba[3] < 1 ? ink.rgba[3] : undefined,
      backdrop: backdrop.image ? 'image' : hex(backdrop.rgb),
      // Type.
      fs: parseFloat(cs.fontSize),
      fw: parseInt(cs.fontWeight, 10) || 400,
      ff: (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim() || undefined,
      lh: cs.lineHeight === 'normal' ? undefined : parseFloat(cs.lineHeight),
      // Space. A design system's spacing scale is a claim about these numbers.
      pad: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].map(parseFloat),
      gap: cs.gap && cs.gap !== 'normal' ? cs.gap : undefined,
      rad: parseFloat(cs.borderTopLeftRadius) || undefined,
      disp: cs.display,
      // Overflow: the box says it clips, and the content says it did not fit.
      ov: clipsX || clipsY ? [cs.overflowX, cs.overflowY] : undefined,
      over:
        (clipsX && el.scrollWidth > el.clientWidth + 1) || (clipsY && el.scrollHeight > el.clientHeight + 1)
          ? [el.scrollWidth - el.clientWidth, el.scrollHeight - el.clientHeight]
          : undefined,
      // Deliberate truncation is a design decision, not a bug. It must be distinguishable.
      ell: cs.textOverflow === 'ellipsis' || undefined,
      // The unfocused focus-relevant style, so a Tab pass can say whether anything changed.
      out: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`,
      shadow: cs.boxShadow,
    });
  }

  const de = document.documentElement;
  return {
    doc: {
      title: document.title,
      lang: de.lang || undefined,
      dir: de.dir || 'ltr',
      scrollWidth: de.scrollWidth,
      clientWidth: de.clientWidth,
      scrollHeight: de.scrollHeight,
      clientHeight: de.clientHeight,
      truncated: truncated || undefined,
    },
    // What the world answered when the render asked. The font is the one everybody forgets: a
    // layout that fits in Segoe UI and breaks in the fallback is not a flaky test, it is an
    // ambient input nobody wrote down.
    ambient: {
      dpr: devicePixelRatio,
      dark: matchMedia('(prefers-color-scheme: dark)').matches,
      reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      fontsReady: document.fonts?.status ?? 'unknown',
    },
    nodes,
  };
}

/**
 * Tab through the document and record what focus actually looks like.
 *
 * Never `el.focus()`: `:focus-visible` does not match a programmatic focus on a link, so a capture
 * that focused elements itself would report "no focus ring" for a page that has a perfectly good
 * one — an instrument lying in the direction of alarm. A real Tab is a real keyboard interaction,
 * and the browser then applies the real rule.
 *
 * The by-product is the tab ORDER, which is itself a design property no source file states.
 */
async function captureFocus(page, unfocused, max = 60) {
  const seen = [];
  await page.evaluate(() => document.body.focus?.() ?? document.activeElement?.blur?.());

  for (let i = 0; i < max; i++) {
    await page.keyboard.press('Tab');
    const hit = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body || el === document.documentElement) return null;
      const cs = getComputedStyle(el);
      const parts = [];
      for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
        let seg = n.localName;
        if (n.id) {
          parts.unshift(`${seg}#${n.id}`);
          break;
        }
        const cls = (n.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean);
        if (cls.length) seg += '.' + cls.join('.');
        const sibs = [...(n.parentElement?.children ?? [])].filter((s) => s.localName === n.localName);
        if (sibs.length > 1) seg += `:nth(${sibs.indexOf(n) + 1})`;
        parts.unshift(seg);
      }
      return {
        p: parts.join('>'),
        out: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`,
        shadow: cs.boxShadow,
        visible: el.matches(':focus-visible'),
      };
    });
    if (!hit) break; // focus left the document (the URL bar) — the cycle is done
    if (seen.some((s) => s.p === hit.p)) break; // wrapped

    const before = unfocused.find((n) => n.p === hit.p);
    // A ring is *some* visible change under :focus-visible. WHICH change is a taste question and
    // belongs to the design system; that there is one at all is not.
    const changed = !before || before.out !== hit.out || before.shadow !== hit.shadow;
    seen.push({
      p: hit.p, i, ring: hit.visible && changed,
      focusVisible: hit.visible, out: hit.out, shadow: hit.shadow,
    });
  }
  return seen;
}

/**
 * Render one cell of the state matrix and return it as a tape call.
 *
 * `cell` names the world: the state, and the ambient inputs the code never asked for but the
 * layout depends on entirely.
 *
 * @param {object} page  any driver page — Playwright, Puppeteer, anything exposing
 *                       setViewportSize / emulateMedia / goto / evaluate / keyboard. Not imported.
 * @param {object} cell
 * @param {string} cell.state      the app state being rendered — the name a human would use
 * @param {string} cell.url        where it lives
 * @param {{w:number,h:number}} cell.viewport
 * @param {'light'|'dark'} [cell.theme]
 * @param {boolean} [cell.reducedMotion]
 * @param {(page) => Promise<void>} [cell.setup]   put the app IN the state (click, seed, wait)
 * @param {(page) => Promise<void>} [cell.mutate]  hostile content: the render's probe mode
 */
export async function renderCall(page, cell) {
  const t0 = performance.now();
  const { state, url, viewport, theme = 'light', reducedMotion = false, setup, mutate, focus = true } = cell;

  await page.setViewportSize({ width: viewport.w, height: viewport.h });
  await page.emulateMedia({
    colorScheme: theme,
    reducedMotion: reducedMotion ? 'reduce' : 'no-preference',
  });
  if (url) await page.goto(url, { waitUntil: 'load' });
  await page.evaluate(() => document.fonts?.ready);
  if (setup) await setup(page);
  if (mutate) await mutate(page);

  const result = await page.evaluate(captureLayout, {});
  if (focus) result.focus = await captureFocus(page, result.nodes);

  return {
    ev: 'call',
    fn: 'render',
    kwargs: {
      state,
      url: url ?? null,
      viewport: [viewport.w, viewport.h],
      theme,
      reducedMotion,
    },
    events: [],
    result,
    error: null,
    ms: Math.round((performance.now() - t0) * 100) / 100,
    // A mutated render is a PROBE, exactly as a mutated recording is: it was never observed, so it
    // can never be a regression pin. Only invariants may judge it.
    probe: mutate ? true : undefined,
  };
}

/** A render tape: format v1, `fn: "render"`. A reader that already reads tapes reads this one. */
export class RenderTape {
  constructor(file, { constants = {} } = {}) {
    this.path = file;
    this.seq = 0;
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(
      file,
      JSON.stringify({
        ev: 'session',
        version: FORMAT_VERSION,
        started: isoLocal(new Date()),
        node: process.versions.node,
        constants,
      }) + '\n',
      'utf8',
    );
  }

  write(call) {
    this.seq += 1;
    const line = { ...call, seq: this.seq, ts: isoLocal(new Date()) };
    fs.appendFileSync(this.path, JSON.stringify(line) + '\n', 'utf8');
    return line;
  }
}
