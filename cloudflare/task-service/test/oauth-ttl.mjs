import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare} from 'miniflare';
import {resolve} from 'node:path';

const bundled = await build({entryPoints: ['test/oauth-ttl-fixture.mjs'], bundle: true,
  write: false, format: 'esm', platform: 'browser', external: ['node:*', 'cloudflare:*'],
  plugins: [{name: 'browser', setup(b) {
    b.onResolve({filter: /^@hyperjump\/browser$/}, () => ({path: resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));
  }}]});
const mf = new Miniflare({cf: false, modules: true, script: bundled.outputFiles[0].text,
  compatibilityDate: '2026-07-30', compatibilityFlags: ['nodejs_compat']});
try {
  const response = await mf.dispatchFetch('https://synthetic.example.test/');
  assert.equal(response.status, 200, 'local OAuth lifetime regression');
  assert.deepEqual(await response.json(), {passed: true});
  console.log('PASS OAuth 3600s issuance, reuse without writes, pre-expiry refresh, expiry, audience and revocation');
} finally {
  await mf.dispose();
}
