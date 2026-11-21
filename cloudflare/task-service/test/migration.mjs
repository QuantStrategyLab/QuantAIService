import assert from 'node:assert/strict';
import test from 'node:test';
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {randomBytes} from 'node:crypto';
import {resolve} from 'node:path';

const until = async (fn) => {
  const end = Date.now() + 10000;
  while (Date.now() < end) {
    const value = await fn();
    if (value) return value;
    await new Promise((r) => setTimeout(r, 40));
  }
  throw Error('bounded wait expired');
};

async function buildFixture() {
  return (await build({
    entryPoints: ['test/fixture.mjs'],
    bundle: true,
    format: 'esm',
    platform: 'browser',
    write: false,
    external: ['cloudflare:*'],
    plugins: [{
      name: 'browser',
      setup(b) {
        b.onResolve({filter: /^@hyperjump\/browser$/}, () => ({path: resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));
      },
    }],
  })).outputFiles[0].text;
}

function makeBindings(extra = {}) {
  const origin = 'https://tasks.example.test';
  const project = 'Quant/SyntheticProject';
  const projectId = 'quant-synthetic';
  return {
    ORGANIZATION: 'quant',
    ORGANIZATION_ID: 'Quant',
    PUBLIC_ORIGIN: origin,
    PROJECT_ID: projectId,
    TASK_STORE_NAME: 'personal-tasks-v1',
    GITHUB_CLIENT_ID: 'synthetic-client',
    GITHUB_CLIENT_SECRET: 'synthetic-only-secret',
    AUTH_CLIENTS: {},
    ADMISSIONS_JSON: '[]',
    AGENT_PROVIDERS: {muse: {type: 'muse', enabled: true}},
    PROJECT_CONTEXTS: {[projectId]: {organization: 'quant', ownerId: 'Quant', project, providers: ['muse']}},
    PROJECT_POLICIES: {[project]: {providers: {agent: 'muse'}, models: {muse: ['configured-muse']}, max_active: 20, max_timeout_seconds: 3600, max_request_bytes: 2000000}},
    ...extra,
  };
}

test('admin migration endpoints require valid MIGRATION_ADMIN_TOKEN', async (t) => {
  const script = await buildFixture();
  const mfDisabled = new Miniflare({
    cf: false,
    workers: [{name: 'svc', modules: true, script, compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: makeBindings(), kvNamespaces: {OAUTH_KV: 'kv'}, durableObjects: {TASK_STATE: {className: 'QuantFixtureStore', useSQLite: true}}, serviceBindings: {TASK_SERVICE: {name: 'svc', entrypoint: 'TaskService'}}}],
  });
  t.after(() => mfDisabled.dispose());
  const r404 = await mfDisabled.dispatchFetch('https://tasks.example.test/_admin/migration/drain-status');
  assert.equal(r404.status, 404);

  const adminToken = randomBytes(24).toString('hex');
  const mf = new Miniflare({
    cf: false,
    workers: [{name: 'svc', modules: true, script, compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings: makeBindings({MIGRATION_ADMIN_TOKEN: adminToken}), kvNamespaces: {OAUTH_KV: 'kv'}, durableObjects: {TASK_STATE: {className: 'QuantFixtureStore', useSQLite: true}}, serviceBindings: {TASK_SERVICE: {name: 'svc', entrypoint: 'TaskService'}}}],
  });
  t.after(() => mf.dispose());
  const r401 = await mf.dispatchFetch('https://tasks.example.test/_admin/migration/drain-status', {headers: {Authorization: 'Bearer wrong'}});
  assert.equal(r401.status, 401);
  const r200 = await mf.dispatchFetch('https://tasks.example.test/_admin/migration/drain-status', {headers: {Authorization: `Bearer ${adminToken}`}});
  assert.equal(r200.status, 200);
  const status = await r200.json();
  assert.equal(status.organization_id, 'Quant');
  assert.equal(status.in_flight_count, 0);
});

test('export, import, alarm reconstruction, and DISPATCH_PAUSED preserve state without re-dispatch', async (t) => {
  const script = await buildFixture();
  const adminToken = randomBytes(24).toString('hex');
  let triggerCount = 0;
  const outboundService = async () => {
    triggerCount += 1;
    return Response.json({ok: true});
  };
  const makeWorker = (bindings, kv = 'kv') => new Miniflare({
    cf: false,
    workers: [{name: 'svc', modules: true, script, compatibilityDate: '2026-07-30', compatibilityFlags: ['global_fetch_strictly_public'], bindings, kvNamespaces: {OAUTH_KV: kv}, durableObjects: {TASK_STATE: {className: 'QuantFixtureStore', useSQLite: true}}, serviceBindings: {TASK_SERVICE: {name: 'svc', entrypoint: 'TaskService'}}, outboundService}],
  });

  const nowSec = Math.floor(Date.now() / 1000);
  const reqBody = JSON.stringify({mode: 'agent', prompt: 'migration test', model: 'configured-muse', timeout_seconds: 300});
  const seedSnapshot = {
    version: 1,
    organization_id: 'Quant',
    exported_at: nowSec,
    events_seq: 2,
    tables: {
      tasks: [
        {
          id: 'task-completed-1', owner: 'Quant/SyntheticProject', idem: 'idem-1', digest: 'a'.repeat(64),
          provider: 'muse', request: reqBody, status: 'completed', handle: JSON.stringify({task_id: 'task-completed-1'}),
          output: JSON.stringify({ok: true}), artifacts: '[]', error: null,
          created: nowSec - 60, updated: nowSec - 30, deadline: nowSec + 240,
          cancel_sent: 0, worker: 'github-20649888:muse-client', attempt: 'att-1', revision: 1,
          report_digest: null, report_json: JSON.stringify({revision: 1, status: 'completed', output: {ok: true}, artifacts: []}),
          claimed_at: nowSec - 50, completed_at: nowSec - 30,
        },
        {
          id: 'task-queued-2', owner: 'Quant/SyntheticProject', idem: 'idem-2', digest: 'b'.repeat(64),
          provider: 'muse', request: reqBody, status: 'queued', handle: null,
          output: null, artifacts: '[]', error: null,
          created: nowSec - 10, updated: nowSec - 10, deadline: nowSec + 290,
          cancel_sent: 0, worker: null, attempt: null, revision: 1,
          report_digest: null, report_json: null, claimed_at: null, completed_at: null,
        },
      ],
      events: [
        {seq: 1, task_id: 'task-completed-1', status: 'completed', created: nowSec - 30, fanout_noted: 1},
        {seq: 2, task_id: 'task-queued-2', status: 'queued', created: nowSec - 10, fanout_noted: 0},
      ],
      native_notifications: [],
      messages: [],
      muse_polls: [],
      subscriptions: [],
      event_deliveries: [],
      task_metadata_cursor_key: [
        {id: 1, material_b64: Buffer.from(randomBytes(32)).toString('base64')},
      ],
    },
  };

  // 1. Source store with WRITE_FROZEN=true & DISPATCH_PAUSED=true
  const srcFrozen = makeWorker(makeBindings({MIGRATION_ADMIN_TOKEN: adminToken, WRITE_FROZEN: 'true', DISPATCH_PAUSED: 'true'}));
  t.after(() => srcFrozen.dispose());

  const seedRes = await srcFrozen.dispatchFetch('https://tasks.example.test/_admin/migration/import', {
    method: 'POST',
    headers: {Authorization: `Bearer ${adminToken}`, 'Content-Type': 'application/json'},
    body: JSON.stringify(seedSnapshot),
  });
  assert.equal(seedRes.status, 200, await seedRes.text());

  // DISPATCH_PAUSED=true keeps queued task untouched and triggerCount=0
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(triggerCount, 0);
  const preDrain = await (await srcFrozen.dispatchFetch('https://tasks.example.test/_admin/migration/drain-status', {
    headers: {Authorization: `Bearer ${adminToken}`},
  })).json();
  assert.equal(preDrain.write_frozen, true);
  assert.equal(preDrain.dispatch_paused, true);
  assert.equal(preDrain.task_counts.completed, 1);
  assert.equal(preDrain.task_counts.queued, 1);

  // Export from source
  const exportRes = await srcFrozen.dispatchFetch('https://tasks.example.test/_admin/migration/export', {
    headers: {Authorization: `Bearer ${adminToken}`},
  });
  assert.equal(exportRes.status, 200);
  const snapshot = await exportRes.json();
  assert.equal(snapshot.tables.tasks.length, 2);
  assert.equal(snapshot.tables.events.length, 2);
  assert.equal(snapshot.tables.task_metadata_cursor_key.length, 1);

  // 2. Import into fresh target store with DISPATCH_PAUSED=false
  const dst = makeWorker(makeBindings({MIGRATION_ADMIN_TOKEN: adminToken}), 'kv-dst');
  t.after(() => dst.dispose());
  const importRes = await dst.dispatchFetch('https://tasks.example.test/_admin/migration/import', {
    method: 'POST',
    headers: {Authorization: `Bearer ${adminToken}`, 'Content-Type': 'application/json'},
    body: JSON.stringify(snapshot),
  });
  assert.equal(importRes.status, 200);
  const importBody = await importRes.json();
  assert.equal(importBody.imported, true);
  assert.ok(importBody.alarm_scheduled_at > 0);

  // Re-importing without replace=1 must fail (non-empty guard -> 409 Conflict)
  const dupImport = await dst.dispatchFetch('https://tasks.example.test/_admin/migration/import', {
    method: 'POST',
    headers: {Authorization: `Bearer ${adminToken}`, 'Content-Type': 'application/json'},
    body: JSON.stringify(snapshot),
  });
  assert.equal(dupImport.status, 409);

  // Reconstructed alarm should process queued task (task-queued-2 -> failed because no live muse poll/sub),
  // while leaving task-completed-1 untouched.
  await until(async () => {
    const drain = await (await dst.dispatchFetch('https://tasks.example.test/_admin/migration/drain-status', {
      headers: {Authorization: `Bearer ${adminToken}`},
    })).json();
    return (drain.task_counts.failed ?? 0) === 1 && (drain.task_counts.completed ?? 0) === 1;
  });

  const dstExport = await (await dst.dispatchFetch('https://tasks.example.test/_admin/migration/export', {
    headers: {Authorization: `Bearer ${adminToken}`},
  })).json();
  const completedTask = dstExport.tables.tasks.find((r) => r.id === 'task-completed-1');
  assert.equal(completedTask.status, 'completed');
  assert.equal(completedTask.attempt, 'att-1');
  assert.equal(completedTask.revision, 1);
  assert.ok(dstExport.events_seq >= 2);
});
