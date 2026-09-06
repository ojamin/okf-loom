import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const behavior = readFileSync(new URL('../../scripts/okf_loom/viewer/static/workspace.js', import.meta.url), 'utf8');

test('catalog filtering, empty state and keyboard clear preserve native cards', () => {
  const dom = new JSDOM(`<body data-okf-mode="static">
    <nav class="okf-workspace-nav"><a href="./index.html">Library</a></nav>
    <p class="okf-studio-fallback-banner">Unavailable</p>
    <main class="okf-index"><div id="okf-catalog-tools" hidden>
      <input id="okf-catalog-filter"><button id="okf-catalog-clear">Clear</button>
      <output id="okf-catalog-status"></output></div>
      <section class="okf-section"><a class="okf-card" href="a.html">Refund policy</a></section>
      <section class="okf-section"><a class="okf-card" href="b.html">Orders table</a></section>
    </main></body>`, { url: 'https://example.test/docs/index.html', runScripts: 'outside-only' });
  dom.window.eval(behavior);
  const doc = dom.window.document, filter = doc.querySelector('input');
  const cards = [...doc.querySelectorAll('.okf-card')];
  assert.equal(doc.querySelector('nav a').getAttribute('aria-current'), 'page');
  assert.equal(doc.querySelector('#okf-catalog-tools').hidden, false);
  filter.value = 'refund policy';
  filter.dispatchEvent(new dom.window.Event('input'));
  assert.deepEqual(cards.map(n => n.hidden), [false, true]);
  filter.value = 'missing';
  filter.dispatchEvent(new dom.window.Event('input'));
  assert.match(doc.querySelector('output').textContent, /No concepts match/);
  filter.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'Escape'}));
  assert.deepEqual(cards.map(n => n.hidden), [false, false]);
  assert.equal(doc.querySelector('.okf-studio-fallback-banner').hidden, true);
});
