import {build} from 'esbuild';
import {resolve} from 'node:path';
await build({entryPoints:['src/worker.mjs'],bundle:true,format:'esm',platform:'browser',target:'es2022',outfile:'dist/worker.mjs',external:['cloudflare:*'],plugins:[{name:'official-browser-export',setup(b){b.onResolve({filter:/^@hyperjump\/browser$/},()=>({path:resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));}}]});
