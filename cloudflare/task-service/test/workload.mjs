import assert from 'node:assert/strict';
import {generateKeyPairSync, sign, createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {build} from 'esbuild';
import {Miniflare} from 'miniflare';

const issuer = 'https://token.actions.githubusercontent.com';
const jwksURL = issuer + '/.well-known/jwks';
const {privateKey, publicKey} = generateKeyPairSync('rsa', {modulusLength: 2048});
const otherKey = generateKeyPairSync('rsa', {modulusLength: 2048}).privateKey;
const jwk = {...publicKey.export({format: 'jwk'}), kid: 'synthetic-key', alg: 'RS256', use: 'sig'};
const fixture = (await build({entryPoints: ['test/fixture.mjs'], bundle: true, format: 'esm',
  platform: 'browser', write: false, external: ['cloudflare:*'], plugins: [{name: 'browser', setup(b) {
    b.onResolve({filter: /^@hyperjump\/browser$/}, () => ({path: resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));
  }}]})).outputFiles[0].text;
const task = {mode: 'agent', model: 'configured-dot', objective: 'Synthetic workload compatibility',
  output_schema: {type: 'object', properties: {ok: {type: 'boolean'}}, required: ['ok'], additionalProperties: false}};
let passed = 0;
const test = async (name, fn) => {await fn(); passed++; console.log('PASS ' + name);};
const fixtures = [];
const organizations = [];

async function organizationFixture(organization, ownerId, internalOwnerId = ownerId) {
  const origin = `https://${organization}.example.test`, projectId = organization + '-synthetic';
  const project = ownerId + '/SyntheticProject', ref = 'refs/heads/main';
  const grant = {issuer, identity: 'synthetic-workflow', subject: `repo:${project}:ref:${ref}`,
    project, scopes: ['tasks:submit', 'tasks:read'],
    claims: {repository: project, workflow_ref: `${project}/.github/workflows/ai-task.yml@${ref}`, ref}};
  const bindings = {ORGANIZATION: organization, ORGANIZATION_ID: organization === 'quant' ? 'Quant' : 'Pigbibi', PUBLIC_ORIGIN: origin,
    PROJECT_ID: projectId, TASK_STORE_NAME: 'synthetic-tasks', AUTH_CLIENTS: {}, ADMISSIONS_JSON: '[]',
    GITHUB_WORKLOAD_AUDIENCE: origin + '/v1/tasks',
    GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([grant]),
    PROJECT_CONTEXTS: {[projectId]: {organization, ownerId: internalOwnerId, project, providers: ['dot']}},
    PROJECT_POLICIES: {[project]: {providers: {agent: 'dot'}, models: {dot: ['configured-dot']},
      max_active: 20, max_timeout_seconds: 3600, max_request_bytes: 2000000}}};
  const outbound = [];
  const mf = new Miniflare({cf: false, workers: [{name: 'workload', modules: true, script: fixture,
    compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings,
    kvNamespaces: {OAUTH_KV: 'synthetic-oauth'},
    durableObjects: {TASK_STATE: {className: organization === 'quant' ? 'QuantFixtureStore' : 'PersonalFixtureStore', useSQLite: true}},
    serviceBindings: {TASK_SERVICE: {name: 'workload', entrypoint: 'TaskService'}},
    outboundService: async request => {
      outbound.push(request.url);
      assert.equal(request.url, jwksURL, 'only fixed GitHub JWKS may be fetched');
      assert.equal(request.method, 'GET');
      assert.equal(request.headers.get('authorization'), null, 'bearer must not reach JWKS');
      return Response.json({keys: [jwk]});
    }}]});
  fixtures.push(mf);
  const worker = await mf.getWorker();
  const token = (overrides = {}, headers = {}, key = privateKey) => {
    const now = Math.floor(Date.now() / 1000);
    const payload = {iss: issuer, sub: grant.subject, aud: origin + '/v1/tasks',
      exp: now + 300, nbf: now - 5, iat: now - 5, actor_id: '10001', repository_owner: ownerId,
      ...grant.claims, ...overrides};
    const encoded = [Buffer.from(JSON.stringify({alg: 'RS256', kid: jwk.kid, typ: 'JWT', ...headers})).toString('base64url'),
      Buffer.from(JSON.stringify(payload)).toString('base64url')].join('.');
    return encoded + '.' + sign('RSA-SHA256', Buffer.from(encoded), key).toString('base64url');
  };
  const request = (path, {bearer = token(), method = 'GET', body, headers = {}} = {}) =>
    worker.fetch(origin + path, {method, headers: {Authorization: 'Bearer ' + bearer,
      'Content-Type': 'application/json', ...headers}, ...(body === undefined ? {} : {body: JSON.stringify(body)}), redirect: 'manual'});
  return {organization, ownerId, origin, projectId, project, grant, worker, token, request, outbound, bindings};
}

try {
  for (const [organization, ownerId] of [['pigbibi', 'ExamplePersonal'], ['quant', 'ExampleQuant']]) {
    const f = await organizationFixture(organization, ownerId);
    organizations.push(f);
    let id;
    await test(organization + ': valid workload submits and reads one actual local task', async () => {
      const submitted = await f.request('/v1/tasks', {method: 'POST', body: task,
        headers: {'Idempotency-Key': 'synthetic-workload-1'}});
      assert.equal(submitted.status, 202, await submitted.clone().text());
      const record = await submitted.json(); id = record.id;
      assert.equal(record.owner, f.project);
      const read = await f.request('/v1/tasks/' + id);
      assert.equal(read.status, 200); assert.equal((await read.json()).id, id);
    });
    await test(organization + ': unchanged idempotency lookup, missing task and conflict semantics', async () => {
      const replay = await f.request('/v1/tasks', {method: 'POST', body: task,
        headers: {'Idempotency-Key': 'synthetic-workload-1'}});
      assert.equal(replay.status, 202); assert.equal((await replay.json()).id, id);
      // The stored canonical body is key sorted recursively, as in the Python client.
      const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
        ? Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])])) : value;
      const exactDigest = createHash('sha256').update(JSON.stringify(canonical(task))).digest('hex');
      const lookup = await f.request('/v1/tasks/lookup', {headers: {'Idempotency-Key': 'synthetic-workload-1', 'X-Request-SHA256': exactDigest}});
      assert.equal(lookup.status, 200); assert.deepEqual(await lookup.json(), {id});
      assert.equal((await f.request('/v1/tasks/absent')).status, 404);
      assert.equal((await f.request('/v1/tasks/lookup', {headers: {'Idempotency-Key': 'absent', 'X-Request-SHA256': exactDigest}})).status, 404);
      assert.equal((await f.request('/v1/tasks', {method: 'POST', body: {...task, objective: 'Different frozen intent'},
        headers: {'Idempotency-Key': 'synthetic-workload-1'}})).status, 409);
    });
    await test(organization + ': forged identity headers cannot change context or provider', async () => {
      const r = await f.request('/v1/tasks/' + id, {headers: {'X-Owner-ID': 'OtherOwner',
        'X-Project-ID': 'other-project', 'X-Project': 'OtherOwner/Private', 'X-Provider': 'grok',
        'X-GitHub-User-ID': '99999', 'X-Authenticated-Identity': 'forged', 'X-Organization': 'other'}});
      assert.equal(r.status, 200); assert.equal((await r.json()).owner, f.project);
      assert.equal((await f.request('/v1/tasks/' + id + '?project=OtherOwner%2FPrivate')).status, 403);
      assert.equal((await f.request('/v1/tasks', {method: 'POST', body: {...task, provider: 'grok'},
        headers: {'Idempotency-Key': 'forged-provider'}})).status, 400);
    });
    await test(organization + ': preserves the internal owner alias and validates the signed repository owner', async () => {
      const alias = organization + '-existing-owner';
      const a = await organizationFixture(organization, ownerId, alias);
      const submitted = await a.request('/v1/tasks', {method: 'POST', body: task,
        headers: {'Idempotency-Key': 'synthetic-owner-alias'}});
      assert.equal(submitted.status, 202, await submitted.clone().text());
      const record = await submitted.json();
      const read = await a.request('/v1/tasks/' + record.id);
      assert.equal(read.status, 200, await read.clone().text());
      assert.equal((await read.json()).owner, a.project);
      assert.equal((await a.request('/v1/tasks/' + record.id,
        {bearer: a.token({repository_owner: alias})})).status, 401);
      for (const ownerId of ['', null]) {
        const context = {...a.bindings.PROJECT_CONTEXTS[a.projectId], ownerId};
        await a.worker.policy({PROJECT_CONTEXTS: {[a.projectId]: context}});
        assert.equal((await a.request('/v1/tasks/' + record.id)).status, 401);
      }
    });
    for (const [name, claims, headers, key] of [
      ['wrong issuer', {iss: 'https://issuer.example.test'}],
      ['wrong audience', {aud: f.origin + '/mcp'}],
      ['audience array', {aud: [f.origin + '/v1/tasks']}],
      ['expired', {exp: 1}], ['not yet valid', {nbf: 9999999999}],
      ['future issued', {iat: 9999999999}], ['missing nbf', {nbf: null}],
      ['invalid expiry type', {exp: '9999999999'}], ['invalid issued type', {iat: true}],
      ['invalid actor', {actor_id: 'unknown'}],
      ['wrong subject', {sub: `repo:${f.project}:pull_request`}],
      ['fork repository', {repository: 'ExternalFork/SyntheticProject'}],
      ['wrong workflow', {workflow_ref: `${f.project}/.github/workflows/other.yml@refs/heads/main`}],
      ['wrong ref', {ref: 'refs/pull/12/merge'}], ['wrong owner', {repository_owner: 'OtherOwner'}],
      ['PR target event', {event_name: 'pull_request_target'}],
      ['unknown key', {}, {kid: 'not-admitted'}], ['wrong signature', {}, {}, otherKey],
      ['wrong algorithm', {}, {alg: 'HS256'}],
      ['attacker key URL', {}, {jku: 'https://attacker.example.test/keys'}],
    ]) {
      await test(organization + ': rejects ' + name, async () => {
        const r = await f.request('/v1/tasks/' + id, {bearer: f.token(claims, headers, key)});
        assert.equal(r.status, 401, name + ': ' + await r.clone().text());
      });
    }
    await test(organization + ': workload cannot reach MCP, mutation extras or management', async () => {
      for (const [path, method] of [['/mcp', 'POST'], ['/v1/tasks/' + id + '/messages', 'POST'],
        ['/v1/tasks/' + id + '/claim', 'POST'],
        ['/v1/tasks/' + id + '/report', 'POST'], ['/v1/tasks/' + id + '/events', 'GET'], ['/v1/tasks', 'DELETE']]) {
        const r = await f.request(path, {method, ...(method === 'GET' ? {} : {body: {}})});
        assert.ok([401, 403, 405].includes(r.status), path + ': ' + r.status);
      }
    });
    await test(organization + ': grant revocation, widening and cross-project configuration deny access', async () => {
      for (const grants of [[], [{...f.grant, scopes: ['tasks:work']}],
        [{...f.grant, project: 'OtherOwner/Private'}], [f.grant, f.grant],
        [{...f.grant, subject: `repo:${f.project}:pull_request`}],
        [{...f.grant, claims: {...f.grant.claims, ref: 'refs/pull/12/merge'}}]]) {
        await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify(grants)});
        const r = await f.request('/v1/tasks/' + id);
        assert.equal(r.status, 401);
      }
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([f.grant])});
      assert.equal((await f.request('/v1/tasks/' + id)).status, 200);
    });
    await test(organization + ': read-only grant cannot submit and optional scope claims narrow authority', async () => {
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([{...f.grant, scopes: ['tasks:read']}])});
      assert.equal((await f.request('/v1/tasks/' + id)).status, 200);
      assert.equal((await f.request('/v1/tasks', {method: 'POST', body: task,
        headers: {'Idempotency-Key': 'read-only-rejected'}})).status, 403);
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([f.grant])});
      assert.equal((await f.request('/v1/tasks/' + id, {bearer: f.token({scope: 'tasks:submit'})})).status, 403);
    });
    await test(organization + ': missing, malformed or detached context configuration rejects by default', async () => {
      for (const GITHUB_WORKLOAD_GRANTS_JSON of ['', 'not-json', '{}']) {
        await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON});
        assert.equal((await f.request('/v1/tasks/' + id)).status, 401);
      }
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([f.grant]), PROJECT_CONTEXTS: {}});
      assert.equal((await f.request('/v1/tasks/' + id)).status, 401);
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([f.grant]), PROJECT_CONTEXTS: f.bindings.PROJECT_CONTEXTS});
    });
    await test(organization + ': immutable subject needs exact owner and repository IDs', async () => {
      const claims = {...f.grant.claims, repository_owner_id: '20001', repository_id: '30001'};
      const sub = `repo:${ownerId}@20001/SyntheticProject@30001:ref:refs/heads/main`;
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([{...f.grant, subject: sub, claims}])});
      assert.equal((await f.request('/v1/tasks/' + id, {bearer: f.token({...claims, sub})})).status, 200);
      assert.equal((await f.request('/v1/tasks/' + id, {bearer: f.token({...claims, repository_id: '30002', sub})})).status, 401);
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([f.grant])});
    });
    await test(organization + ': one explicit legacy audience preserves caller JWT without MCP access', async () => {
      const legacy = `https://legacy-${organization}.example.test/mcp`;
      await f.worker.policy({GITHUB_WORKLOAD_AUDIENCE: legacy});
      assert.equal((await f.request('/v1/tasks/' + id, {bearer: f.token({aud: legacy})})).status, 200);
      assert.equal((await f.request('/v1/tasks/' + id)).status, 401);
      assert.equal((await f.request('/mcp', {method: 'POST', bearer: f.token({aud: legacy}), body: {}})).status, 401);
      for (const audience of ['', 'not-a-url', 'http://legacy.example.test/mcp', ['https://legacy.example.test/mcp'],
        'https://legacy.example.test/mcp?different=1', 'https://user:password@legacy.example.test/mcp']) {
        await f.worker.policy({GITHUB_WORKLOAD_AUDIENCE: audience});
        assert.equal((await f.request('/v1/tasks/' + id)).status, 401);
      }
      await f.worker.policy({GITHUB_WORKLOAD_AUDIENCE: f.origin + '/v1/tasks'});
    });
    await test(organization + ': original project-owner cancellation is bounded and idempotent', async () => {
      const path = '/v1/tasks/' + id + '/cancel';
      assert.equal((await f.request(path + '?project=OtherOwner%2FPrivate', {method: 'POST', body: {}})).status, 403);
      assert.equal((await f.request('/v1/tasks/absent/cancel', {method: 'POST', body: {}})).status, 404);
      assert.equal((await f.request(path, {method: 'POST', body: {unexpected: true}})).status, 400);
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([{...f.grant, scopes: ['tasks:read']}])});
      assert.equal((await f.request(path, {method: 'POST', body: {}})).status, 403);
      await f.worker.policy({GITHUB_WORKLOAD_GRANTS_JSON: JSON.stringify([f.grant])});
      // This fixture has no active Dot subscription: its existing alarm makes the task failed.
      // Cancelling a terminal task must retain that fact rather than relabel it cancelled.
      let original;
      for (let attempt = 0; attempt < 40; attempt++) {
        original = await (await f.request('/v1/tasks/' + id)).json();
        if (original.status === 'failed') break;
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      assert.equal(original.status, 'failed');
      const first = await f.request(path, {method: 'POST', body: {}});
      assert.equal(first.status, 200);
      const record = await first.json(); assert.equal(record.id, id); assert.equal(record.owner, f.project);
      assert.equal(record.status, original.status);
      const second = await f.request(path, {method: 'POST', body: {}});
      assert.equal(second.status, 200); assert.equal((await second.json()).status, record.status);
    });
    assert.ok(f.outbound.length >= 1);
    assert.ok(f.outbound.every(url => url === jwksURL));
    f.taskId = id;
  }
  await test('two organizations reject the other audience, repository and task', async () => {
    const [a, b] = organizations;
    assert.equal((await b.request('/v1/tasks/' + a.taskId, {bearer: a.token()})).status, 401);
    assert.equal((await b.request('/v1/tasks/' + a.taskId, {bearer: a.token({aud: b.origin + '/v1/tasks'})})).status, 401);
    assert.equal((await b.request('/v1/tasks/' + a.taskId)).status, 404);
    assert.equal((await a.request('/v1/tasks/' + b.taskId)).status, 404);
    assert.equal((await b.request('/v1/tasks/' + a.taskId + '/cancel', {method: 'POST', body: {}})).status, 404);
    assert.equal((await a.request('/v1/tasks/' + b.taskId + '/cancel', {method: 'POST', body: {}})).status, 404);
  });
  await test('authentication strips all trust headers before the service binding', async () => {
    const f = organizations[0];
    const script = (await build({stdin: {resolveDir: process.cwd(), contents: `
      import {configuration, createAuthApplication} from './src/auth.mjs';
      export default {fetch(request, env, ctx) {
        return createAuthApplication(configuration(env), {taskHandler: async (request, identity) => {
          const names = ['authorization', 'x-owner-id', 'x-owner', 'x-project-id', 'x-project', 'x-provider',
            'x-user-id', 'x-github-user-id', 'x-authenticated-identity', 'x-organization'];
          return Response.json({headersCleared: names.every(name => request.headers.get(name) === null),
            key: request.headers.get('Idempotency-Key'), identity,
            frozen: Object.isFrozen(identity) && Object.isFrozen(identity.scopes)});
        }}).fetch(request, env, ctx);
      }};`}, bundle: true, format: 'esm', platform: 'browser', write: false, external: ['cloudflare:*']})).outputFiles[0].text;
    const mf = new Miniflare({cf: false, modules: true, script, compatibilityDate: '2026-07-30',
      compatibilityFlags: ['global_fetch_strictly_public'], bindings: f.bindings,
      outboundService: async request => {assert.equal(request.url, jwksURL); return Response.json({keys: [jwk]});}});
    fixtures.push(mf);
    const r = await mf.dispatchFetch(f.origin + '/v1/tasks', {method: 'POST', body: '{}', headers: {
      Authorization: 'Bearer ' + f.token(), 'Idempotency-Key': 'preserved', 'X-Owner-ID': 'forged',
      'X-Owner': 'forged', 'X-Project-ID': 'forged', 'X-Project': 'forged', 'X-Provider': 'grok',
      'X-User-ID': 'forged', 'X-GitHub-User-ID': 'forged', 'X-Authenticated-Identity': 'forged', 'X-Organization': 'forged'}});
    assert.equal(r.status, 200);
    const result = await r.json();
    assert.equal(result.headersCleared, true); assert.equal(result.key, 'preserved'); assert.equal(result.frozen, true);
    assert.equal(result.identity.ownerId, f.ownerId); assert.equal(result.identity.projectId, f.projectId);
    assert.equal(result.identity.githubUserId, '10001'); assert.equal(result.identity.provider, null);
  });
  console.log(JSON.stringify({status: 'PASS', cases: passed, synthetic: true, remoteJWTRequests: 0, modelRequests: 0}));
} finally {await Promise.all(fixtures.map(mf => mf.dispose()));}
