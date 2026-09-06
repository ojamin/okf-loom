import test from "node:test";
import assert from "node:assert/strict";
import { JSDOM } from "jsdom";
import { diffAndPatchBody } from "../../scripts/okf_loom/viewer/static/document-patch.js";

function setup(html) {
  const dom = new JSDOM(`<main>${html}</main>`);
  globalThis.document = dom.window.document;
  return dom.window.document.querySelector("main");
}

test("updates text beyond a shared 200 character prefix and preserves untouched nodes", () => {
  const prefix = "A long paragraph. ".repeat(30);
  const root = setup(`<h2 id="keep">Keep</h2><p>${prefix}old ending</p>`);
  const heading = root.firstElementChild;
  diffAndPatchBody(root, `<h2 id="keep">Keep</h2><p>${prefix}new ending</p>`);
  assert.equal(root.firstElementChild, heading);
  assert.match(root.textContent, /new ending/);
  assert.doesNotMatch(root.textContent, /old ending/);
});

test("same-label link destination and formatting changes are applied", () => {
  const root = setup('<p><a href="/old">Read this</a> text</p>');
  diffAndPatchBody(root, '<p><a href="/new">Read this</a> <strong>text</strong></p>');
  assert.equal(root.querySelector("a").getAttribute("href"), "/new");
  assert.equal(root.querySelector("strong").textContent, "text");
});

test("large documents preserve stable prefix and suffix with bounded reconciliation", () => {
  const content = Array.from({ length: 900 }, (_, i) => `<p>Paragraph ${i}</p>`).join("");
  const root = setup(content);
  const first = root.firstElementChild, last = root.lastElementChild;
  diffAndPatchBody(root, content.replace("Paragraph 450", "Revised 450"));
  assert.equal(root.firstElementChild, first);
  assert.equal(root.lastElementChild, last);
  assert.match(root.textContent, /Revised 450/);
  assert.equal(root.children.length, 900);
});

test("heading permalinks and comment marks preserve unchanged block identity", () => {
  const root = setup('<h2 id="keep">Keep<a class="okf-heading-anchor" href="#keep">¶</a></h2><p>A <mark>note</mark></p>');
  const nodes = [...root.children];
  diffAndPatchBody(root, '<h2 id="keep">Keep</h2><p>A note</p>');
  assert.deepEqual([...root.children], nodes);
});

test("formatting position and code whitespace edits are visible", () => {
  const root = setup('<p><strong>one</strong> two</p><pre><code>  indented\n</code></pre>');
  diffAndPatchBody(root, '<p>one <strong>two</strong></p><pre><code>    indented\n</code></pre>');
  assert.equal(root.querySelector('strong').textContent, 'two');
  assert.equal(root.querySelector('code').textContent, '    indented\n');
});

test("enhanced tables retain sorting unless source semantics change", () => {
  const source = '<table><tbody><tr><td><a href="/old">Link</a></td></tr></tbody></table>';
  const root = setup('<div class="okf-tablewrap">'+source+'</div>');
  const wrapper = root.firstElementChild;
  wrapper.setAttribute('data-source-html', source);
  diffAndPatchBody(root, source);
  assert.equal(root.firstElementChild, wrapper);
  diffAndPatchBody(root, source.replace('/old', '/new'));
  assert.equal(root.querySelector('a').getAttribute('href'), '/new');
});
