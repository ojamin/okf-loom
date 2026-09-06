import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {setImmediate} from 'node:timers/promises';
import {JSDOM} from 'jsdom';

const behavior = readFileSync(new URL('../../scripts/okf_loom/viewer/static/live.js', import.meta.url), 'utf8');
test('distinct CLI and HTTP events with equal revision counters both patch; replay does not', async () => {
  const dom = new JSDOM('<main class="okf-page__body">Original</main>', {url:'https://example.test/topic',runScripts:'outside-only'});
  const window = dom.window, sources = [], patches = [];
  class Source {
    constructor() { this.handlers = {}; sources.push(this); }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    close() {}
    send(event) { this.handlers[event.type]({data:JSON.stringify(event)}); }
  }
  window.EventSource = Source;
  window.__OKF_LOOM_STUDIO__ = {live:true};
  window.scrollTo = () => {};
  window.okfLoomStudio = {applyDoc(doc) { patches.push(doc.raw); return true; }};
  let current = 'Revised';
  window.fetch = async () => ({ok:true, json:async () => ({rev:current,raw:current,html:`<p>${current}</p>`})});
  try {
    window.eval(behavior);
    await setImmediate();
    const source = sources[0];
    const edit = {type:'changed',event_id:'cli-event',rev:4,ids:['topic']};
    source.send(edit);
    await setImmediate();
    current = 'Original';
    const undo = {...edit,event_id:'http-undo'};
    source.send(undo);
    await setImmediate();
    source.send(undo);
    await setImmediate();
    assert.deepEqual(patches,['Revised','Original']);
  } finally { window.close(); }
});
