// Masked delivery diagnostics. Callers pass already-split fields; this module
// drops everything else and refuses to emit a line that still carries a URL,
// secret, signature or account principal.
const MESSAGES = new Set(['event_delivery', 'subscription_subscribed', 'subscription_refreshed', 'subscription_expired', 'event_unmatched', 'subscription_verification_failed']);
const CLASSES = new Set(['timeout', 'network', 'client_error', 'rate_limited', 'server_error', 'challenge_failed']);
const HOST = /^(?:invalid-host|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?::[0-9]{1,5})?)$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const redacted = () => JSON.stringify({message: 'event_delivery_redacted'});

export function callbackHost(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || !url.hostname) return 'invalid-host';
    return url.host;
  } catch {
    return 'invalid-host';
  }
}

export function shortSubscriptionId(id) {
  if (typeof id !== 'string' || !/^sub_[0-9a-f]{64}$/.test(id)) return 'sub_unknown';
  return id.slice(0, 16);
}

export function classifyDelivery(error, status) {
  if (error) return error.name === 'TimeoutError' || error.name === 'AbortError' ? 'timeout' : 'network';
  if (status >= 200 && status < 300) return null;
  if (status === 429) return 'rate_limited';
  if (status >= 400 && status < 500) return 'client_error';
  if (status >= 500 && status <= 599) return 'server_error';
  return 'network';
}

function take(fields, key, ok) {
  if (fields[key] === undefined) return true;
  return ok;
}

export function deliveryLog(fields) {
  if (!fields || typeof fields !== 'object' || Array.isArray(fields) || !MESSAGES.has(fields.message)) return redacted();
  if (!take(fields, 'subscription', typeof fields.subscription === 'string' && /^sub_(?:unknown|[0-9a-f]{12})$/.test(fields.subscription))) return redacted();
  if (!take(fields, 'callback_host', typeof fields.callback_host === 'string' && HOST.test(fields.callback_host))) return redacted();
  if (!take(fields, 'event', fields.event === 'task.requested' || fields.event === 'verification')) return redacted();
  if (!take(fields, 'task_id', fields.task_id === 'invalid-task' || (typeof fields.task_id === 'string' && UUID.test(fields.task_id)))) return redacted();
  if (!take(fields, 'attempt', Number.isInteger(fields.attempt) && fields.attempt >= 1 && fields.attempt <= 20)) return redacted();
  if (!take(fields, 'http_status', fields.http_status === null || (Number.isInteger(fields.http_status) && fields.http_status >= 100 && fields.http_status <= 599))) return redacted();
  if (!take(fields, 'error_class', fields.error_class === null || CLASSES.has(fields.error_class))) return redacted();
  if (!take(fields, 'latency_ms', Number.isInteger(fields.latency_ms) && fields.latency_ms >= 0 && fields.latency_ms <= 120000)) return redacted();
  if (!take(fields, 'matching_subscriptions', Number.isInteger(fields.matching_subscriptions) && fields.matching_subscriptions >= 0 && fields.matching_subscriptions <= 100)) return redacted();
  const record = {message: fields.message};
  for (const key of ['subscription', 'callback_host', 'event', 'task_id', 'attempt', 'http_status', 'error_class', 'latency_ms', 'matching_subscriptions']) {
    if (fields[key] !== undefined) record[key] = fields[key];
  }
  return JSON.stringify(record);
}

export function emitEventLog(fields) {
  const line = deliveryLog(fields);
  const failure = Boolean(fields && typeof fields === 'object' && (fields.error_class || fields.message === 'event_unmatched' || fields.message === 'subscription_expired' || fields.message === 'subscription_verification_failed'));
  (failure ? console.error : console.log)(line);
  return line;
}
