import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {callbackHost, classifyDelivery, deliveryLog, shortSubscriptionId} from '../src/delivery-log.mjs';

const fullId = 'sub_' + 'ab'.repeat(32);
const taskId = '11111111-1111-4111-8111-111111111111';
const secretUrl = 'https://user:keep-out@delivery-probe.example:8443/hooks/v1?access=synthetic-marker#frag';

assert.equal(callbackHost(secretUrl), 'delivery-probe.example:8443');
assert.equal(callbackHost('not a url'), 'invalid-host');
assert.equal(shortSubscriptionId(fullId), 'sub_abababababab');
assert.equal(shortSubscriptionId('github-10001:client'), 'sub_unknown');

const line = deliveryLog({
  message: 'event_delivery',
  subscription: shortSubscriptionId(fullId),
  callback_host: callbackHost(secretUrl),
  event: 'task.requested',
  task_id: taskId,
  attempt: 2,
  http_status: 410,
  error_class: 'client_error',
  latency_ms: 18,
  url: secretUrl,
  secret: 'whsec_synthetic',
  principal: 'github-10001:synthetic-client',
  signature: 'v1,aaaa',
});
const parsed = JSON.parse(line);
assert.equal(parsed.subscription, 'sub_abababababab');
assert.equal(parsed.callback_host, 'delivery-probe.example:8443');
assert.equal(parsed.http_status, 410);
assert.equal(parsed.error_class, 'client_error');
assert.equal(parsed.attempt, 2);
assert.ok(!line.includes(fullId.slice(16)));
assert.ok(!line.includes('hooks'));
assert.ok(!line.includes('synthetic-marker'));
assert.ok(!line.includes('keep-out'));
assert.ok(!line.includes('whsec_'));
assert.ok(!line.includes('github-'));
assert.ok(!line.includes('webhook-signature'));
assert.ok(!line.includes('v1,'));

const leaked = JSON.parse(deliveryLog({message: 'event_delivery', callback_host: secretUrl, event: 'task.requested'}));
assert.equal(leaked.message, 'event_delivery_redacted');
assert.ok(!JSON.stringify(leaked).includes('synthetic-marker'));
assert.ok(!JSON.stringify(leaked).includes('whsec_'));

assert.equal(classifyDelivery(Object.assign(new Error(secretUrl), {name: 'TimeoutError'}), 0), 'timeout');
assert.equal(classifyDelivery(Object.assign(new Error(secretUrl), {name: 'AbortError'}), 0), 'timeout');
assert.equal(classifyDelivery(Object.assign(new Error(secretUrl), {name: 'TypeError'}), 0), 'network');
assert.equal(classifyDelivery(null, 204), null);
assert.equal(classifyDelivery(null, 410), 'client_error');
assert.equal(classifyDelivery(null, 429), 'rate_limited');
assert.equal(classifyDelivery(null, 503), 'server_error');
assert.equal(classifyDelivery(null, 0), 'network');

const config = JSON.parse(await readFile('wrangler.example.jsonc', 'utf8'));
assert.equal(config.observability.enabled, true);
assert.equal(config.observability.logs.enabled, true);
assert.equal(config.observability.logs.head_sampling_rate, 1);
assert.equal(config.observability.logs.invocation_logs, false);
assert.equal(config.observability.traces, undefined);
const raw = await readFile('wrangler.example.jsonc', 'utf8');
assert.ok(!raw.includes('whsec_'));
assert.ok(!raw.includes('github-'));

console.log('PASS delivery log redaction and workers log config');
