import assert from 'node:assert/strict';
import test from 'node:test';
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {createHash,createHmac,randomBytes} from 'node:crypto';
import {resolve} from 'node:path';
import {readFile} from 'node:fs/promises';


const until = async (fn) => {
  const end = Date.now() + 10000;
  while (Date.now() < end) {
    const value = await fn();
    if (value) return value;
    await new Promise((resolveWait) => setTimeout(resolveWait, 40));
  }
  throw Error('bounded wait expired');
};
async function subscriptionFixture(t) {
  const fixture = (await build({entryPoints: ['test/fixture.mjs'], bundle: true, format: 'esm', platform: 'browser', write: false, external: ['cloudflare:*'], plugins: [{name: 'signing-boundary', setup(b) {b.onLoad({filter: /src\/webhooks\.mjs$/}, async ({path}) => ({contents: (await readFile(path, 'utf8')).replace("  return {'Content-Type'", "  if (id.startsWith('evt_')) await globalThis.subscriptionSigningBoundary?.();\n  return {'Content-Type'"), loader: 'js'}));}}, {name: 'browser', setup(b) {b.onResolve({filter: /^@hyperjump\/browser$/}, () => ({path: resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));}}]})).outputFiles[0].text;
  const origin = 'https://tasks.example.test';
  const project = 'Quant/SyntheticProject';
  const projectId = 'quant-synthetic';
  const key = randomBytes(32);
  const secret = 'whsec_' + key.toString('base64');
  const seen = [];
  let retryStatus = 503;
  let verificationDelay = 0, verifiedAt = 0, verifying = false, onDelivery = null;
  const outboundService = async (request) => {
    const url = new URL(request.url);
    assert.equal(url.hostname, 'dot-callback.example');
    const body = await request.text();
    const stamp = request.headers.get('webhook-timestamp');
    const id = request.headers.get('webhook-id');
    const expected = 'v1,' + createHmac('sha256', key).update(id + '.' + stamp + '.' + body).digest('base64');
    assert.ok(request.headers.get('webhook-signature').split(' ').includes(expected));
    const event = JSON.parse(body);
    if (event.type === 'verification') {verifying = true;if (verificationDelay) await new Promise(resolveWait => setTimeout(resolveWait, verificationDelay)); verifiedAt = Date.now(); return Response.json({challenge: event.challenge});}
    if (onDelivery) {const action = onDelivery; onDelivery = null; await action();}
    seen.push({path: url.pathname, eventId: event.eventId, task: event.data?.task_id});
    if (['/retry', '/stop'].includes(url.pathname)) return new Response(null, {status: retryStatus});
    return new Response(null, {status: 202});
  };
  const policy = {ORGANIZATION: 'quant', ORGANIZATION_ID: 'Quant', PUBLIC_ORIGIN: origin, PROJECT_ID: projectId, TASK_STORE_NAME: 'personal-tasks-v1', GITHUB_CLIENT_ID: 'synthetic-client', GITHUB_CLIENT_SECRET: 'synthetic-only-secret', AUTH_CLIENTS: {}, ADMISSIONS_JSON: '[]', PROJECT_CONTEXTS: {[projectId]: {organization: 'quant', ownerId: 'Quant', project, providers: ['dot']}}, PROJECT_POLICIES: {[project]: {providers: {agent: 'dot'}, models: {dot: ['configured-dot']}, max_active: 20, max_timeout_seconds: 3600, max_request_bytes: 2000000}}};
  const workerOptions = (bindings) => ({cf: false, workers: [{name: 'personal', modules: true, script: fixture, compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings, kvNamespaces: {OAUTH_KV: 'test-oauth'}, durableObjects: {TASK_STATE: {className: 'QuantFixtureStore', useSQLite: true}}, serviceBindings: {TASK_SERVICE: {name: 'personal', entrypoint: 'TaskService'}}, outboundService}]});
  const mf = new Miniflare(workerOptions(policy));
  t.after(() => mf.dispose());
  let worker = await mf.getWorker();
  {
    const dot = (await worker.client('http://localhost:8989/callback')).clientId;
    const caller = (await worker.client('http://localhost:8989/callback')).clientId;
    const clients = {[dot]: {provider: 'dot', scopes: ['tasks:read', 'tasks:work', 'events:subscribe'], resources: [origin + '/mcp']}, [caller]: {provider: null, scopes: ['tasks:read', 'tasks:submit'], resources: [origin + '/v1/tasks']}};
    const admission = [
      {organization: 'quant', projectId, githubUserId: '424242', ownerId: 'Quant', clientIds: [dot, caller], providers: ['dot'], scopes: ['tasks:read', 'tasks:work', 'tasks:submit', 'events:subscribe']},
      {organization: 'quant', projectId, githubUserId: '424243', ownerId: 'Quant', clientIds: [dot], providers: ['dot'], scopes: ['tasks:read', 'tasks:work', 'events:subscribe']},
    ];
    policy.AUTH_CLIENTS = clients;
    policy.ADMISSIONS_JSON = JSON.stringify(admission);
    await worker.policy({AUTH_CLIENTS: clients, ADMISSIONS_JSON: JSON.stringify(admission)});
    await mf.setOptions(workerOptions(policy));
    worker = await mf.getWorker();
    const request = (path, init = {}) => worker.fetch(origin + path, {...init, redirect: 'manual'});
    const mint = async (githubUserId, clientId, scope, resource, provider) => {
      const verifier = randomBytes(48).toString('base64url');
      const challenge = createHash('sha256').update(verifier).digest('base64url');
      const code = await worker.grant({clientId, scope, resource, challenge, provider, githubUserId});
      const response = await request('/oauth/token', {method: 'POST', body: new URLSearchParams({grant_type: 'authorization_code', code, client_id: clientId, redirect_uri: 'http://localhost:8989/callback', code_verifier: verifier, resource})});
      assert.equal(response.status, 200);
      return (await response.json()).access_token;
    };
    const otherToken = await mint('424243', dot, ['tasks:read', 'tasks:work', 'events:subscribe'], origin + '/mcp', 'dot');
    const callerToken = await mint('424242', caller, ['tasks:read', 'tasks:submit'], origin + '/v1/tasks', null);
    // A second grant for the same user and client replaces the previous access token.
    const dotToken = await mint('424242', dot, ['tasks:read', 'tasks:work', 'events:subscribe'], origin + '/mcp', 'dot');
    const rpc = async (method, params = {}, token = dotToken) => {
      const response = await request('/mcp', {method: 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify({jsonrpc: '2.0', id: 1, method, params})});
      const text = await response.text();
      return {status: response.status, body: text ? JSON.parse(text) : null};
    };
    const tool = (name, args, token = dotToken) => rpc('tools/call', {name, arguments: args}, token);
    const notify = async (token = dotToken) => request('/mcp', {method: 'POST', headers: {Authorization: 'Bearer ' + token, 'Content-Type': 'application/json'}, body: JSON.stringify({jsonrpc: '2.0', method: 'notifications/initialized'})});
    const view = async (id, token = dotToken) => (await tool('get_event_subscription', {subscription_id: id}, token)).body.result.structuredContent;
    const logs = async () => (await worker.eventLogs()).map(line => JSON.parse(line));
    const taskRequest = {mode: 'agent', model: 'configured-dot', objective: 'Synthetic subscription observation', materials: [], output_schema: {type: 'object', additionalProperties: false}};
    const submit = async (idem) => {
      const response = await request('/v1/tasks', {method: 'POST', headers: {Authorization: 'Bearer ' + callerToken, 'Content-Type': 'application/json', 'Idempotency-Key': idem}, body: JSON.stringify(taskRequest)});
      assert.equal(response.status, 202, await response.clone().text());
      return response.json();
    };
    return {
      rpc, tool, notify, view, submit, seen, mint, dot, dotToken, origin, project, logs,
      signingBoundary: (id, action) => worker.signingBoundary(id, action),
      revokeOAuth: () => worker.revokeOAuthGrants('424242', dot),
      grantReadMode: (mode) => worker.grantReadMode(mode),
      legacyIdentity: (id) => worker.legacyIdentity(id),
      acceptCallbacks: () => {retryStatus = 202;},
      delayVerification: (ms) => {verificationDelay = ms;},
      verifiedAt: () => verifiedAt,
      verifying: () => verifying,
      onDelivery: (action) => {onDelivery = action;},
      subscribeDefault: () => rpc('events/subscribe', {name: 'task.requested', arguments: {project}, delivery: {mode: 'webhook', url: 'https://dot-callback.example/default', secret}}),
      internalSubmit: () => worker.projectTask(project, taskRequest),
      subscribe: (path, ttlMs = 60000, token = dotToken) => rpc('events/subscribe', {name: 'task.requested', arguments: {project}, delivery: {mode: 'webhook', url: 'https://dot-callback.example/' + path, secret}, ttlMs}, token),
      expire: (id) => worker.setSubscriptionExpiry(id, Date.now() / 1000 - 1),
      message: async (id, idem) => {const response = await request('/v1/tasks/' + id + '/messages', {method: 'POST', headers: {Authorization: 'Bearer ' + callerToken, 'Content-Type': 'application/json', 'Idempotency-Key': idem}, body: JSON.stringify({text: 'synthetic follow-up'})}); assert.equal(response.status, 200);},
      revoke: async () => {policy.ADMISSIONS_JSON = '[]'; await mf.setOptions(workerOptions(policy)); worker = await mf.getWorker();},
      setExpiry: (id, timestamp) => worker.setSubscriptionExpiry(id, timestamp),
      setGrant: (id, ttl) => worker.setSubscriptionGrant(id, ttl),
      lease: (id) => worker.subscriptionLease(id),
      taskStatus: (id) => worker.fixtureTaskStatus(project, id),
      tick: () => worker.alarmProbe('tick'),
      restart: async () => {await mf.setOptions(workerOptions(policy)); worker = await mf.getWorker();},
    };
  }
}


const sid = r => {assert.ok(r.body?.result?.id,JSON.stringify(r.body));return r.body.result.id;};
test('R1 revoked OAuth grant stops persisted unlimited callback',async t=>{
 const f=await subscriptionFixture(t);sid(await f.subscribe('revoked',null));
 assert.ok(await f.revokeOAuth()>0);assert.equal((await f.rpc('ping')).status,401);
 const task=await f.submit('revoked-task');
 await until(async()=>!['queued','submitting'].includes(await f.taskStatus(task.id)));
 await f.tick();await new Promise(r=>setTimeout(r,2500));await f.tick();
 const state=await f.taskStatus(task.id);
 console.log(JSON.stringify({probe:'revoked-grant',taskState:state,postRevocationDeliveries:f.seen.length}));
 assert.equal(state,'failed','revoked subscription must not admit new Dot work');
 assert.equal(f.seen.length,0,'revoked grant must not start deliveries');
});
test('R2 restoring expired ID obeys four active slots',async t=>{
 const f=await subscriptionFixture(t);const id=sid(await f.subscribe('expired',60000));await f.expire(id);
 for(let i=0;i<4;i++)sid(await f.subscribe('active-'+i,null));
 const restored=await f.subscribe('expired',60000);
 assert.ok(restored.body?.error,'fifth active subscription must be rejected');
});
test('R3 delayed verification grants full finite window',async t=>{
 const f=await subscriptionFixture(t);f.delayVerification(1200);
 const r=await f.subscribe('delayed',1000);sid(r);
 assert.ok(Date.parse(r.body.result.refreshBefore)>=f.verifiedAt()+990,'full TTL must start after verification');
});
test('R4 positive subsecond TTL is accepted',async t=>{
 const f=await subscriptionFixture(t);sid(await f.subscribe('subsecond',750));
});
test('R5 unrepresentable TTL rejected without persisting active row',async t=>{
 const f=await subscriptionFixture(t);const r=await f.subscribe('overflow',Number.MAX_SAFE_INTEGER);
 assert.ok(r.body?.error);const subs=(await f.tool('list_event_subscriptions',{})).body.result.structuredContent;
 assert.equal(subs.subscriptions.length,0,'invalid date must not leave subscription');
});

test('P2 original scope renewal exploit is absent',async t=>{
 const f=await subscriptionFixture(t);const id=sid(await f.subscribe('scope',60000));const before=await f.lease(id);
 const restricted=await f.mint('424242',f.dot,['tasks:read','tasks:work'],f.origin+'/mcp','dot');
 const denied=await f.rpc('events/subscribe',{name:'task.requested',arguments:{project:f.project}},restricted);assert.ok(denied.status===403||denied.body?.error?.code===-32003);
 await f.rpc('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'synthetic',version:'0'}},restricted);
 assert.equal((await f.lease(id)).expires,before.expires);
});
test('P2 unknown legacy TTL does not become a new one-hour grant',async t=>{
 const f=await subscriptionFixture(t);const id=sid(await f.subscribe('legacy',60000));await f.setGrant(id,null);await f.expire(id);
 const before=await f.lease(id);
 await f.rpc('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'synthetic',version:'0'}});await f.tick();
 assert.equal((await f.lease(id)).expires,before.expires);assert.equal((await f.view(id)).status,'inactive');
});

const initParams = {protocolVersion: "2025-11-25", capabilities: {}, clientInfo: {name: "synthetic", version: "0"}};
const subscriptionId = sid;
test('finite TTL is exact, uncapped, and omitted TTL uses the finite default', {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  for (const ttl of [7200123, 60001, 750, 1, undefined]) await t.test('ttlMs ' + String(ttl), async () => {
    // undefined omits the JSON property rather than sending explicit null.
    const start = Date.now();
    f.delayVerification(100);
    const reply = ttl === undefined ? await f.subscribeDefault() : await f.subscribe('finite', ttl);
    const id = subscriptionId(reply);
    const expiry = Date.parse(reply.body.result.refreshBefore);
    const duration = ttl ?? 3600000;
    assert.ok(expiry >= start + duration && expiry <= Date.now() + duration);
    assert.ok(expiry >= f.verifiedAt() + duration, 'grant starts after callback verification');
    assert.equal(Date.parse((await f.view(id)).expires_at), expiry);
    const lease=await f.lease(id);
    assert.ok(Math.abs(lease.expires * 1000 - expiry) < 1);
    assert.equal(lease.ttl_seconds, duration/1000);
  });
  for (const ttl of [0, -1, 0.5, '1000', true, Number.MAX_SAFE_INTEGER]) {
    assert.equal((await f.subscribe('invalid', ttl)).body.error?.code, -32602);
  }
});

test('finite and unlimited grants share the transactional four-active limit', {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  const old = subscriptionId(await f.subscribe('expired'));
  await f.expire(old);
  for (let i = 0; i < 4; i++) subscriptionId(await f.subscribe('slot-' + i, i % 2 ? 60000 : null));
  await f.restart();
  await Promise.all([f.rpc('initialize', initParams), f.tick()]);
  assert.equal((await f.view(old)).status, 'inactive');
  assert.equal((await f.subscribe('expired')).body.error?.code, -32602);
  assert.equal((await f.subscribe('extra', null)).body.error?.code, -32602);
  const id = subscriptionId(await f.subscribe('slot-0', null));
  await f.tool('delete_event_subscription', {subscription_id: id});
  const replacements = await Promise.all([f.subscribe('replacement-a', null), f.subscribe('replacement-b', null)]);
  assert.equal(replacements.filter(reply => reply.body?.result?.id).length, 1);
  assert.equal(replacements.filter(reply => reply.body?.error?.code === -32602).length, 1);
  assert.equal((await f.tool('list_event_subscriptions', {})).body.result.structuredContent.subscriptions.filter(row => row.status === 'active').length, 4);
});

for(const ttl of [null,60000])test('review: OAuth grant revocation stops '+(ttl===null?'unlimited':'finite')+' subscription delivery',{timeout:60000},async(t)=>{
  const f=await subscriptionFixture(t);
  const reply=await f.subscribe('revoked-grant',ttl);
  assert.ok(reply.body.result.id);
  assert.equal(await f.revokeOAuth(),1);
  assert.equal((await f.rpc('initialize',initParams)).status,401,'revoked credential must be invalid');
  const missed=await f.submit('after-oauth-revocation');
  await until(async()=>!['queued','submitting'].includes(await f.taskStatus(missed.id)));
  await f.tick();
  const delivered=f.seen.filter(e=>e.task===missed.id).length;
  console.log(JSON.stringify({case:'oauth-revocation',lifetime:ttl===null?'unlimited':'finite',revokedTokenStatus:401,postRevocationDeliveries:delivered}));
  assert.equal(delivered,0,'revoked OAuth grant must stop subsequent event delivery');
});

test('review: narrower replacement grant stops an unlimited event subscription',{timeout:60000},async(t)=>{
  const f=await subscriptionFixture(t);
  assert.ok((await f.subscribe('narrowed-grant',null)).body.result.id);
  const restricted=await f.mint('424242',f.dot,['tasks:read','tasks:work'],f.origin+'/mcp','dot');
  assert.equal((await f.rpc('initialize',initParams)).status,401);
  const denial=await f.rpc('events/subscribe',{name:'task.requested',arguments:{project:f.project}},restricted); assert.ok(denial.status===403||denial.body?.error?.code===-32003);
  const task=await f.submit('after-scope-narrowing');
  await until(async()=>!['queued','submitting'].includes(await f.taskStatus(task.id)));
  await new Promise(resolveWait=>setTimeout(resolveWait,2500));
  await f.tick();
  const delivered=f.seen.filter(e=>e.task===task.id).length;
  console.log(JSON.stringify({case:'replacement-grant-without-events-scope',oldTokenStatus:401,explicitSubscribeDenied:true,postRevocationDeliveries:delivered}));
  assert.equal(delivered,0,'replacing the grant without events scope must stop the old event subscription');
});

for (const ttl of [null, 60000]) test('OAuth revocation stops persisted retries after restart: ' + ttl, {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  assert.ok((await f.subscribe('retry', ttl)).body.result.id);
  const first = await f.submit('oauth-pending-retry');
  await until(() => f.seen.some(event => event.task === first.id));
  assert.equal(await f.revokeOAuth(), 1);
  const before = f.seen.length;
  await f.restart();
  await new Promise(resolveWait => setTimeout(resolveWait, 2500));
  await f.tick();
  assert.equal(f.seen.length, before, 'persisted retry must recheck its original grant');
  const next = await f.submit('oauth-post-restart');
  await until(async () => !['queued', 'submitting'].includes(await f.taskStatus(next.id)));
  assert.equal(await f.taskStatus(next.id), 'failed', 'revoked subscriptions cannot admit new tasks');
  assert.equal(f.seen.length, before);
});

for (const mode of ['unavailable', 'invalid', 'legacy', 'expired', 'scope', 'id', 'userId', 'clientId', 'resource']) test('grant authority fails closed: ' + mode, {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  const id = (await f.subscribe('retry', null)).body.result.id;
  assert.ok(id);
  const first = await f.submit('before-grant-failure');
  await until(() => f.seen.some(event => event.task === first.id));
  if (mode === 'legacy') await f.legacyIdentity(id);
  else await f.grantReadMode(mode);
  assert.equal((await f.view(id)).status, 'inactive');
  const before = f.seen.length;
  await new Promise(resolveWait => setTimeout(resolveWait, 2500));
  await f.tick();
  assert.equal(f.seen.length, before);
  const next = await f.submit('after-grant-failure');
  await until(async () => !['queued', 'submitting'].includes(await f.taskStatus(next.id)));
  assert.equal(await f.taskStatus(next.id), 'failed');
  assert.equal(f.seen.length, before);
  if (mode !== 'legacy') {
    const rejected = await f.subscribe('unavailable-subscribe', null);
    assert.equal(rejected.body.error.code, -32003);
  }
});

test('grant revoked during callback verification cannot persist a subscription', {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  f.delayVerification(500);
  const pending = f.subscribe('revoke-during-verification', null);
  // The callback fixture signals arrival before its deliberately delayed reply.
  await until(() => f.verifying());
  assert.equal(await f.revokeOAuth(), 1);
  const rejected = await pending;
  assert.equal(rejected.body.error.code, -32003);
});

test('revocation in an earlier callback stops later deliveries in the same batch', {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  assert.ok((await f.subscribe('first', null)).body.result.id);
  assert.ok((await f.subscribe('second', null)).body.result.id);
  f.onDelivery(() => f.revokeOAuth());
  const task = await f.submit('revoke-during-batch');
  await until(() => f.seen.some(event => event.task === task.id));
  await f.tick();
  assert.equal(f.seen.filter(event => event.task === task.id).length, 1, 'each send must read current grant authority');
});


test('replacement grant requires explicit subscribe and never inherits pending events', {timeout: 60000}, async (t) => {
  const f = await subscriptionFixture(t);
  const id = (await f.subscribe('retry', null)).body.result.id;
  const first = await f.submit('before-replacement');
  await until(() => f.seen.some(event => event.task === first.id));
  const replacement = await f.mint('424242', f.dot, ['tasks:read', 'tasks:work', 'events:subscribe'], f.origin + '/mcp', 'dot');
  assert.equal((await f.view(id, replacement)).status, 'inactive');
  f.acceptCallbacks();
  assert.equal((await f.subscribe('retry', null, replacement)).body.result.id, id);
  const before = f.seen.length;
  await new Promise(resolveWait => setTimeout(resolveWait, 2500));
  await f.tick();
  assert.equal(f.seen.length, before, 'new grant cannot inherit old pending retries');
  const next = await f.submit('after-explicit-replacement-subscribe');
  await until(() => f.seen.some(event => event.task === next.id));
  const logs = JSON.stringify(await f.logs());
  for (const privateValue of [f.dotToken, replacement, f.dotToken.split(':')[1], f.dot]) {
    assert.equal(logs.includes(privateValue), false, 'diagnostics must not expose grant credentials or identity');
  }
});

for (const action of ['expire', 'delete', 'revoke']) test('send boundary rechecks ' + action + ' after signing', async t => {
  const f = await subscriptionFixture(t);
  const id = sid(await f.subscribe('signing-' + action, 60000));
  await f.signingBoundary(id, action);
  const task = await f.submit('signing-boundary-' + action);
  await until(async () => !['queued', 'submitting'].includes(await f.taskStatus(task.id)));
  await f.tick();
  await new Promise(resolveWait => setTimeout(resolveWait, 500));
  assert.equal(f.seen.length, 0, 'no fetch after authority or lifetime ends during signing');
});

test('deleted retained ID needs a free active slot', async t => {
  const f = await subscriptionFixture(t);
  const id = sid(await f.subscribe('deleted', null));
  await f.tool('delete_event_subscription', {subscription_id: id});
  for (let i = 0; i < 4; i++) sid(await f.subscribe('replacement-' + i, i % 2 ? null : 60000));
  await f.restart();
  assert.ok((await f.subscribe('deleted', null)).body.error);
});

test('retry expiry during signing prevents a second callback', async t => {
  const f = await subscriptionFixture(t);
  const id = sid(await f.subscribe('retry', 60000));
  await f.submit('retry-signing-expiry');
  await until(async () => (await f.view(id)).last_status === 503);
  assert.equal(f.seen.length, 1);
  await f.signingBoundary(id, 'expire');
  await new Promise(resolveWait => setTimeout(resolveWait, 2200));
  await f.tick();
  await new Promise(resolveWait => setTimeout(resolveWait, 500));
  assert.equal(f.seen.length, 1, 'retry must recheck expiry after awaited signing');
});
