import { build } from 'esbuild';
import { resolve } from 'node:path';
const output=process.argv[2];
if (!output) throw new Error('Output path required');
await build({stdin:{contents:`import {createElement} from 'react';import {createRoot} from 'react-dom/client';import {LoomWorkspace} from './plugins/meridian/workspace.js';const root=createRoot(document.getElementById('root'));root.render(createElement(LoomWorkspace));window.loomTestUnmount=()=>root.unmount();`,resolveDir:process.cwd()},
  outfile:output,bundle:true,format:'esm',platform:'browser',
  plugins:[{name:'test-boundaries',setup(build){
    build.onResolve({filter:/^@meridian\/plugin-sdk$/},()=>({path:resolve('tests/meridian-host-double.js')}));
    build.onResolve({filter:/^\.\/configuration\.js$/},()=>({path:'configuration',namespace:'test'}));
    build.onLoad({filter:/.*/,namespace:'test'},()=>({contents:'export const configuration={origin:"https://loom.example.com"};',loader:'js'}));
    build.onResolve({filter:/^\.\/client\.js$/},args=>args.importer.endsWith('/plugins/meridian/bridge.js')?{path:resolve('scripts/okf_loom/viewer/static/client.js')}:null);
  }}]});
