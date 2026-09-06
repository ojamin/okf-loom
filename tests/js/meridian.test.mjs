import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, copyFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { JSDOM } from 'jsdom';

const dir = await mkdtemp(join(tmpdir(), 'loom-meridian-'));
await copyFile('plugins/meridian/bridge.js',join(dir,'bridge.js'));
await copyFile('scripts/okf_loom/viewer/static/client.js',join(dir,'client.js'));
await writeFile(join(dir,'package.json'),'{"type":"module"}');
const {connectLoom,createMeridianTransport,safeMarkup,placement}=await import(pathToFileURL(join(dir,'bridge.js')));
test.after(()=>rm(dir,{recursive:true,force:true}));

function host(net) {return {can:()=>true,hasPermission:p=>p==='network:loom.example.com',net:{fetch:net}};}
test('transport uses only the granted host and relays the real bridge response shape', async()=>{
  const calls=[];
  const client=connectLoom(host(async r=>{calls.push(r);return {status:200,body:'{"ok":true}',headers:{}};}),'https://loom.example.com','session-secret');
  await client.save({id:'topic',source:'---\ntype: Note\n---\n',expected_rev:'abc'});
  assert.equal(calls[0].url,'https://loom.example.com/__save');
  assert.equal(calls[0].headers['X-OKF-Token'],'session-secret');
  assert.equal(calls[0].method,'POST');
  assert.equal(JSON.parse(calls[0].body).expected_rev,'abc');
  const fetch=createMeridianTransport(host(async()=>assert.fail('must not dispatch')),'https://loom.example.com');
  await assert.rejects(()=>fetch('https://elsewhere.example.com/__save'),/outside/);
  await assert.rejects(()=>fetch('https://loom.example.com/private'),/outside/);
});
test('failed mutations surface exactly once and do not automatically retry',async()=>{
  let count=0;
  const client=connectLoom(host(async()=>{count++;return {status:409,body:'{"error":"conflict"}'};}),'https://loom.example.com');
  await assert.rejects(()=>client.save({id:'topic'}),e=>e.status===409);
  assert.equal(count,1);
});
test('cancellation suppresses stale results without retrying dispatched writes',async()=>{
  let finish,count=0;
  const transport=createMeridianTransport(host(()=>{count++;return new Promise(r=>finish=r);}),'https://loom.example.com');
  const controller=new AbortController();
  const request=transport('https://loom.example.com/__data/doc',{signal:controller.signal});
  controller.abort();
  await assert.rejects(request,e=>e.name==='AbortError');
  finish({status:200,body:'{}'});
  assert.equal(count,1);
});
test('document rendering strips active content and dangerous links',()=>{
  const {window}=new JSDOM();
  const markup=safeMarkup('<script>bad()</script><p onclick="bad()">Hello <strong>world</strong></p><a href="javascript:bad()">bad</a><a href="/topic">topic</a><iframe src="https://evil.example"></iframe><img src=x onerror=bad()>',window.document);
  assert.match(markup,/<strong>world<\/strong>/);
  assert.match(markup,/href="\/topic"/);
  assert.doesNotMatch(markup,/script|onclick|iframe|onerror|javascript/);
  const anchors=safeMarkup('<h2 id="schema">Schema</h2><a href="#schema">Jump</a><img alt="Revenue chart" src="x">',window.document);
  assert.match(anchors,/id="loom-document-schema"/);
  assert.match(anchors,/href="#loom-document-schema"/);
  assert.match(anchors,/Image: Revenue chart/);
  window.close();
});
test('placement state follows host context without broadening its scope',()=>{
  assert.deepEqual(placement({context:{itemId:'itm_1',boardId:'brd_1',extensionKey:'concept'}}),{scope:'item:itm_1',key:'loom-placement:concept'});
  assert.deepEqual(placement({context:{boardId:'brd_2',viewId:'view_2'}}),{scope:'board:brd_2',key:'loom-placement:view_2'});
});
