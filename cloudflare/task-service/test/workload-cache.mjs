// Local regression tests for per-request reuse in the workload verifier.
// Runs under plain Node with synthetic keys and claims; no Cloudflare bindings.
import assert from 'node:assert/strict';
import {generateKeyPairSync, sign} from 'node:crypto';
import {authenticateGitHubWorkload, githubWorkloadToken} from '../src/workload.mjs';

const issuer = 'https://token.actions.githubusercontent.com';
const jwksURL = issuer + '/.well-known/jwks';
const organization = 'synthetic', projectId = 'synthetic-project', origin = 'https://tasks.example.test';
const project = 'ExampleQuant/SyntheticProject', ref = 'refs/heads/main';
const grant = {issuer, identity: 'synthetic-workflow', subject: `repo:${project}:ref:${ref}`, project,
  scopes: ['tasks:submit', 'tasks:read'],
  claims: {repository: project, workflow_ref: `${project}/.github/workflows/ai-task.yml@${ref}`, ref}};
const baseEnv = {GITHUB_WORKLOAD_AUDIENCE: origin + '/v1/tasks', GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([grant]),
  PROJECT_CONTEXTS: {[projectId]: {organization, ownerId: 'ExampleQuant', project, providers: ['dot']}}};
const config = {organization, projectId, origin};

// Deterministic clock for the verifier's five-minute refresh window.
const realNow = Date.now.bind(Date);
let fakeNow = realNow();
Date.now = () => fakeNow;

let jwksFetches = 0, rsaImports = 0;
const subtlePrototype = Object.getPrototypeOf(globalThis.crypto.subtle);
const originalImportKey = subtlePrototype.importKey;
subtlePrototype.importKey = function(...args) {rsaImports++; return originalImportKey.apply(this, args);};

const {privateKey, publicKey} = generateKeyPairSync('rsa', {modulusLength: 2048});
const jwk = {...publicKey.export({format: 'jwk'}), kid: 'cache-key', alg: 'RS256', use: 'sig'};
globalThis.fetch = async url => {jwksFetches++; assert.equal(String(url), jwksURL); return Response.json({keys: [jwk]});};
const token = (claims = {}, header = {}) => {
  const now = Math.floor(Date.now() / 1000);
  const payload = {iss: issuer, sub: grant.subject, aud: origin + '/v1/tasks',
    exp: now + 3600, nbf: now - 5, iat: now - 5, actor_id: '10001', repository_owner: 'ExampleQuant',
    ...grant.claims, ...claims};
  const encoded = [Buffer.from(JSON.stringify({alg: 'RS256', kid: jwk.kid, typ: 'JWT', ...header})).toString('base64url'),
    Buffer.from(JSON.stringify(payload)).toString('base64url')].join('.');
  return encoded + '.' + sign('RSA-SHA256', Buffer.from(encoded), privateKey).toString('base64url');
};

const cases = [];
const test = async (name, fn) => {await fn(); cases.push(name); console.log('PASS ' + name);};
try {
  await test('first verification fetches the JWKS and imports the signing key once', async () => {
    const identity = await authenticateGitHubWorkload(token(), baseEnv, config);
    assert.equal(identity.organization, organization);
    assert.equal(identity.ownerId, 'ExampleQuant');
    assert.equal(identity.provider, null);
    assert.deepEqual([...identity.scopes].sort(), ['tasks:read', 'tasks:submit']);
    assert.equal(jwksFetches, 1); assert.equal(rsaImports, 1);
  });
  await test('later verifications reuse the imported key and parsed grants', async () => {
    const narrowed = await authenticateGitHubWorkload(token({scope: 'tasks:read'}), baseEnv, config);
    assert.deepEqual([...narrowed.scopes], ['tasks:read']);
    const second = await authenticateGitHubWorkload(token(), baseEnv, config);
    assert.equal(second.ownerId, 'ExampleQuant');
    assert.equal(jwksFetches, 1); assert.equal(rsaImports, 1);
  });
  await test('expiry, audience and future-validity checks still reject on the cached-key path', async () => {
    await assert.rejects(authenticateGitHubWorkload(token({exp: 1}), baseEnv, config));
    await assert.rejects(authenticateGitHubWorkload(token({aud: origin + '/mcp'}), baseEnv, config));
    await assert.rejects(authenticateGitHubWorkload(token({nbf: 9999999999}), baseEnv, config));
    assert.equal(rsaImports, 1);
  });
  await test('an unknown kid is rejected without importing anything', async () => {
    await assert.rejects(authenticateGitHubWorkload(token({}, {kid: 'absent'}), baseEnv, config));
    assert.equal(rsaImports, 1);
  });
  await test('the five-minute refresh window re-fetches the JWKS and re-imports the key', async () => {
    fakeNow += 300001;
    const identity = await authenticateGitHubWorkload(token(), baseEnv, config);
    assert.equal(identity.organization, organization);
    assert.equal(jwksFetches, 2); assert.equal(rsaImports, 2);
  });
  await test('grant configuration changes revoke and restore access without redeploy', async () => {
    const otherRef = 'refs/heads/dev';
    const otherGrant = {...grant, subject: `repo:${project}:ref:${otherRef}`,
      claims: {...grant.claims, workflow_ref: `${project}/.github/workflows/ai-task.yml@${otherRef}`, ref: otherRef}};
    const otherEnv = {...baseEnv, GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([otherGrant])};
    await assert.rejects(authenticateGitHubWorkload(token(), otherEnv, config));
    const otherToken = token({...otherGrant.claims, sub: otherGrant.subject});
    assert.equal((await authenticateGitHubWorkload(otherToken, otherEnv, config)).ownerId, 'ExampleQuant');
    const restored = await authenticateGitHubWorkload(token(), baseEnv, config);
    assert.equal(restored.organization, organization);
    await assert.rejects(authenticateGitHubWorkload(otherToken, baseEnv, config));
  });
  await test('bearer probe decodes once and stays null for other issuers', async () => {
    const bearer = token();
    const request = new Request(origin + '/v1/tasks', {headers: {authorization: 'Bearer ' + bearer}});
    const parsed = githubWorkloadToken(request);
    assert.equal(parsed.token, bearer); assert.equal(parsed.decoded.claims.iss, issuer);
    const foreign = new Request(origin + '/v1/tasks', {headers: {authorization: 'Bearer ' + token({iss: 'https://issuer.example.test'})}});
    assert.equal(githubWorkloadToken(foreign), null);
    assert.equal(githubWorkloadToken(new Request(origin + '/v1/tasks')), null);
  });
  await test('concurrent workload verifications share fetch/import and spend zero OAuth KV ops', async () => {
    const {authenticateGitHubWorkload: freshAuth} = await import('../src/workload.mjs?single-flight');
    let fetches = 0, imports = 0, kvOps = 0;
    globalThis.fetch = async () => {fetches++; await Promise.resolve(); return Response.json({keys:[jwk]});};
    subtlePrototype.importKey = function(...args) {imports++; return originalImportKey.apply(this,args);};
    const kv = new Proxy({}, {get() {kvOps++; throw Error('unexpected_oauth_kv');}});
    await Promise.all(Array.from({length:20},()=>freshAuth(token(), {...baseEnv,OAUTH_KV:kv}, config)));
    assert.equal(fetches,1); assert.equal(imports,1); assert.equal(kvOps,0);
    const {authenticateGitHubWorkload: retryAuth} = await import('../src/workload.mjs?retry-import');
    let failed = false;
    subtlePrototype.importKey = function(...args) {
      if (!failed) {failed=true; return Promise.reject(Error('synthetic_import_failure'));}
      return originalImportKey.apply(this,args);
    };
    await assert.rejects(retryAuth(token(),baseEnv,config),/synthetic_import_failure/);
    assert.equal((await retryAuth(token(),baseEnv,config)).organization,organization);
  });
  await test('an old in-flight import cannot poison a refreshed JWKS snapshot', async () => {
    const {authenticateGitHubWorkload: freshAuth} = await import('../src/workload.mjs?rotation-race');
    const rotated = generateKeyPairSync('rsa', {modulusLength: 2048});
    const rotatedJwk = {...rotated.publicKey.export({format: 'jwk'}), kid: jwk.kid, alg: 'RS256', use: 'sig'};
    let releaseOld, notifyStarted;
    const started = new Promise(resolve => {notifyStarted = resolve;});
    const blocked = new Promise(resolve => {releaseOld = resolve;});
    const oldBearer = token();
    const parts = token().split('.');
    const rotatedBearer = parts.slice(0, 2).join('.') + '.' +
      sign('RSA-SHA256', Buffer.from(parts.slice(0, 2).join('.')), rotated.privateKey).toString('base64url');
    let served = jwk;
    globalThis.fetch = async () => Response.json({keys: [served]});
    subtlePrototype.importKey = async function(...args) {
      if (args[1].n === jwk.n) {notifyStarted(); await blocked;}
      return originalImportKey.apply(this, args);
    };
    const oldRequest = freshAuth(oldBearer, baseEnv, config);
    await started;
    fakeNow += 300001;
    served = rotatedJwk;
    await freshAuth(rotatedBearer, baseEnv, config);
    releaseOld();
    await oldRequest;
    await freshAuth(rotatedBearer, baseEnv, config);
    await assert.rejects(freshAuth(oldBearer, baseEnv, config), /workload_token_rejected/);
    subtlePrototype.importKey = originalImportKey;
  });
  console.log(JSON.stringify({status: 'PASS', cases: cases.length, synthetic: true}));
} catch (error) {
  console.error(error); process.exitCode = 1;
}
