/** Pure formatting and honest-state helpers.

  These functions are intentionally dependency-free: the dashboard builder
  embeds their source into the page script, and the tests import them directly.
  A missing, null or invalid value must never be coerced to zero or to a
  healthy state.
 */

export function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function clampPercent(value) {
  const number = finiteNumber(value);
  if (number === null) return 0;
  return Math.max(0, Math.min(100, number));
}

export function percentOrNull(used, budget) {
  const usedNumber = finiteNumber(used);
  const budgetNumber = finiteNumber(budget);
  if (usedNumber === null || budgetNumber === null || budgetNumber <= 0) return null;
  return usedNumber / budgetNumber * 100;
}

export function quotaRowModel(entry) {
  const budget = finiteNumber(entry && entry.daily_budget);
  const used = finiteNumber(entry && entry.total_cost_usd);
  if (budget === null || budget <= 0) {
    return { status: "not_configured", percent: null, used: used, budget: null };
  }
  if (used === null) {
    return { status: "unknown", percent: null, used: null, budget: budget };
  }
  return { status: "observed", percent: used / budget * 100, used: used, budget: budget };
}

export function healthReadouts(health) {
  if (!health || typeof health !== "object" || !Array.isArray(health.endpoints)) return null;
  const endpoints = health.endpoints.filter(function (endpoint) {
    return !endpoint || endpoint.latency_profile !== "background_job";
  });
  if (!endpoints.length) return null;
  let requests = 0;
  let errors = 0;
  for (const endpoint of endpoints) {
    const total = finiteNumber(endpoint && endpoint.total);
    const failed = finiteNumber(endpoint && endpoint.errors);
    if (total === null || total < 0 || failed === null || failed < 0) return null;
    requests += total;
    errors += failed;
  }
  const p95Values = [];
  for (const endpoint of endpoints) {
    const value = finiteNumber(endpoint && endpoint.p95_ms);
    if (value !== null && value >= 0) p95Values.push(value);
  }
  return {
    status: String(health.status || "unknown"),
    endpointCount: endpoints.length,
    requests: requests,
    errors: errors,
    errorRate: requests > 0 ? errors / requests * 100 : null,
    p95: p95Values.length ? Math.max.apply(null, p95Values) : null,
    degradedReasons: Array.isArray(health.degradation_reasons) ? health.degradation_reasons.length : 0,
    uptimeSeconds: finiteNumber(health.uptime_seconds),
  };
}

export function tokensFromQuota(quota) {
  const summary = quota && quota.summary;
  if (!summary || typeof summary !== "object") return null;
  const combined = summary.combined;
  const combinedIn = finiteNumber(combined && combined.tokens_input);
  const combinedOut = finiteNumber(combined && combined.tokens_output);
  if (combinedIn !== null || combinedOut !== null) {
    return {
      input: combinedIn || 0,
      output: combinedOut || 0,
      total: (combinedIn || 0) + (combinedOut || 0),
    };
  }
  let input = 0;
  let output = 0;
  let found = false;
  for (const part of [summary.api_key, summary.codex]) {
    const partIn = finiteNumber(part && part.tokens_input);
    const partOut = finiteNumber(part && part.tokens_output);
    if (partIn !== null || partOut !== null) {
      found = true;
      input += partIn || 0;
      output += partOut || 0;
    }
  }
  return found ? { input: input, output: output, total: input + output } : null;
}

export function classifyLoginError(raw) {
  const message = String(raw || "").toLowerCase();
  if (!message) return "none";
  if (message.includes("成员") || message.includes("member") || message.includes("not granted")) {
    return "denied";
  }
  if (message.includes("取消") || message.includes("cancel") || message.includes("access_denied")) return "cancel";
  if (message.includes("过期") || message.includes("expired") || message.includes("撤销") || message.includes("revoked")) {
    return "expired";
  }
  return "error";
}

/** Shared display-only masking for opaque identifiers.

  Keep only a short, fixed suffix so a rendered reference is still usable for
  support correlation without exposing an account name, email or other label.
  Callers must validate shape first; masking never makes an invalid value safe
  to display. Empty input stays empty so callers can treat "no reference"
  separately from a masked one.
 */
export function maskIdentifier(value, visible = 4) {
  const text = String(value ?? "").trim();
  if (!text) return "";
  const compact = text.replace(/\s+/g, "");
  if (compact.length <= visible) return "••••";
  return "••••" + compact.slice(-visible);
}

export function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.toString() : "";
  } catch (error) {
    return "";
  }
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function jsObjectLiteral(value) {
  return JSON.stringify(value)
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e")
    .replaceAll("&", "\\u0026");
}
