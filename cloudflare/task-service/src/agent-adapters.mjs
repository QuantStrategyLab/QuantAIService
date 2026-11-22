// Platform-specific asynchronous wake adapters for named agent providers.
// Triggers carry task identifiers only; agents pull materials and report via OAuth MCP.
import {callbackURL} from './webhooks.mjs';

const ENV_KEY = /^[A-Z][A-Z0-9_]{0,127}$/;
const CLAUDE_ROUTINE_PATH = /^\/v1\/claude_code\/routines\/trig_[A-Za-z0-9]+\/fire$/;
const JULES_SESSION_PATH = /^\/v1alpha\/sessions$/;

const rejected = (message = 'agent trigger rejected') => {
  throw Object.assign(new Error(message), {name: 'SubmissionRejected'});
};

const hasTriggerURL = (item) => typeof item?.url === 'string' && item.url.length > 0;
const EVENT_SLOTS = new Set(['dot', 'muse']);

export function agentDeliveryMode(env, name) {
  const item = env?.AGENT_PROVIDERS?.[name];
  if (!item || typeof item !== 'object') return 'unavailable';
  if (item.type === 'dot_events') return EVENT_SLOTS.has(name) ? 'mcp_events' : 'unavailable';
  if (item.type === 'muse') return hasTriggerURL(item) ? 'webhook_trigger' : (EVENT_SLOTS.has(name) ? 'mcp_events' : 'unavailable');
  if (item.type === 'grok_webhook') return 'webhook_trigger';
  if (item.type === 'claude_routine') return 'claude_routine';
  if (item.type === 'jules_api') return 'jules_api';
  return 'unavailable';
}

export function usesEventSubscription(env, name) {
  return agentDeliveryMode(env, name) === 'mcp_events';
}

export function usesOutboundTrigger(env, name) {
  const mode = agentDeliveryMode(env, name);
  return mode === 'webhook_trigger' || mode === 'claude_routine' || mode === 'jules_api';
}

export function validateAgentTrigger(env, name) {
  const item = env?.AGENT_PROVIDERS?.[name];
  const mode = agentDeliveryMode(env, name);
  if (!item || item.enabled !== true || (mode !== 'webhook_trigger' && mode !== 'claude_routine' && mode !== 'jules_api')) {
    rejected();
  }
  if (typeof item.key_env !== 'string' || !ENV_KEY.test(item.key_env)) rejected();
  let url;
  try {
    url = new URL(callbackURL(item.url));
  } catch {
    rejected();
  }
  if (mode === 'claude_routine' && (url.origin !== 'https://api.anthropic.com' ||
      !CLAUDE_ROUTINE_PATH.test(url.pathname) || url.search)) {
    rejected();
  }
  if (mode === 'jules_api' && (url.origin !== 'https://jules.googleapis.com' ||
      !JULES_SESSION_PATH.test(url.pathname) || url.search)) {
    rejected();
  }
  const key = env[item.key_env];
  if (typeof key !== 'string' || !key || /[\r\n]/.test(key)) rejected();
  return {mode, type: item.type, url: url.href, key};
}

export function agentReadiness(name, item, {env = {}, hasLiveSubscription = false} = {}) {
  if (!item || typeof item !== 'object') return [false, 'not_configured'];
  const kind = item.type;
  if (kind === 'claudeduet') return [false, 'unavailable'];
  if (kind === 'dot_events') {
    if (item.enabled !== true) return [false, 'disabled'];
    return hasLiveSubscription ? [true, 'ready'] : [false, 'no_subscription'];
  }
  if (kind === 'muse') {
    if (item.enabled !== true) return [false, 'disabled'];
    if (hasTriggerURL(item)) {
      if (typeof item.key_env !== 'string' || !ENV_KEY.test(item.key_env) || !env[item.key_env]) {
        return [false, 'missing_credential'];
      }
      try {
        callbackURL(item.url);
      } catch {
        return [false, 'invalid_trigger'];
      }
      return [true, 'ready'];
    }
    return hasLiveSubscription ? [true, 'ready'] : [false, 'no_subscription'];
  }
  if (kind === 'grok_webhook' || kind === 'claude_routine') {
    if (item.enabled !== true) return [false, 'disabled'];
    if (name === 'grokbot' && !env.AI_SERVICE_GROK_READY) return [false, 'grok_not_ready'];
    if (typeof item.key_env !== 'string' || !env[item.key_env]) return [false, 'missing_credential'];
    return [true, 'ready'];
  }
  if (kind === 'jules_api') {
    if (item.enabled !== true) return [false, 'disabled'];
    if (typeof item.key_env !== 'string' || !ENV_KEY.test(item.key_env) || !env[item.key_env]) {
      return [false, 'missing_credential'];
    }
    try {
      const url = new URL(callbackURL(item.url));
      if (url.origin !== 'https://jules.googleapis.com' || !JULES_SESSION_PATH.test(url.pathname) || url.search) {
        return [false, 'invalid_trigger'];
      }
    } catch {
      return [false, 'invalid_trigger'];
    }
    return [true, 'ready'];
  }
  return [false, 'unsupported'];
}

function buildWakeRequest(config, envelope) {
  if (config.mode === 'jules_api') {
    return {
      headers: {'Content-Type': 'application/json', 'X-Goog-Api-Key': config.key},
      body: JSON.stringify({title: `${envelope.project}:${envelope.task_id}`, prompt: JSON.stringify(envelope)}),
      acceptance: 'jules_started',
    };
  }
  const headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config.key};
  if (config.mode === 'claude_routine') {
    headers['anthropic-version'] = '2026-10-21';
    return {headers, body: JSON.stringify({text: JSON.stringify(envelope)}), acceptance: 'routine_started'};
  }
  const acceptance = config.type === 'muse' ? 'webhook_triggered' : 'routine_started';
  return {headers, body: JSON.stringify(envelope), acceptance};
}

export async function wakeAgent(env, name, event, remainingSeconds = 30, _existingHandle = null) {
  const config = validateAgentTrigger(env, name);
  const envelope = {type: 'task.requested', ...event, resource: env.PUBLIC_ORIGIN + '/mcp'};
  const {headers, body, acceptance} = buildWakeRequest(config, envelope);
  const reply = await fetch(config.url, {
    method: 'POST',
    headers,
    redirect: 'manual',
    signal: AbortSignal.timeout(Math.max(1, Math.floor(Math.min(30, remainingSeconds) * 1000))),
    body,
  });
  await reply.body?.cancel();
  if (reply.status >= 200 && reply.status < 300) {
    return {task_id: event.task_id, acceptance, model_verification: 'unavailable'};
  }
  // 5xx/timeouts may follow an accepted run; never blindly resubmit.
  if (reply.status >= 500) throw Error('agent trigger acceptance unknown');
  rejected();
}
