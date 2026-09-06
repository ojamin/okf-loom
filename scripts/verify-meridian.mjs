// Use the deployment's real SDK, never a locally reinvented manifest schema.
import { readFile, stat } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [bundle, meridian] = process.argv.slice(2);
if (!bundle || !meridian) throw new Error('Usage: node scripts/verify-meridian.mjs <export-directory> <built-meridian-checkout>');
const { parsePluginManifest } = await import(pathToFileURL(resolve(meridian,'packages/plugin-sdk/dist/index.js')));
const manifest=JSON.parse(await readFile(resolve(bundle,'manifest.json'),'utf8'));
const result=parsePluginManifest(manifest);
if (!result.ok) throw new Error(JSON.stringify(result.error,null,2));
const root=resolve(bundle,'dist');
for (const extension of result.value.extensions) {
  const path=resolve(root,extension.client.module);
  if (!path.startsWith(root+sep) || !(await stat(path)).isFile()) throw new Error(`Missing or unsafe client module: ${extension.key}`);
}
console.log(`Validated ${manifest.key}@${manifest.version}: ${manifest.extensions.length} surfaces, using this Meridian deployment's SDK.`);
