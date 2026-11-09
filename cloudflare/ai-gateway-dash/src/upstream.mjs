/** Upstream status probe for the AiGateway dashboard.

  The dashboard used to render six panels fed by `/v1/ai/*`. Those handlers were
  removed from the AI service ("feat(ai): adopt project-scoped v2 tasks and
  retire fixed service workflows", 611c129), so every panel reported a proxy
  failure while the page still claimed to show traffic, quota and changes.

  This module replaces that with the one thing the dashboard can honestly
  measure: what the live origin answers right now. It issues bounded, read-only
  probes and reports the status code, the latency and a machine verdict. It does
  not infer counts, quotas or balances, and it never renders a placeholder as if
  it were data.

  Two probes hit `/v1/tasks/stats`: one anonymous, one with the dashboard's
  static token. The pair is the evidence for `read_model.reason_code` — it shows
  that the route exists but only accepts an OAuth bearer, which is the real
  reason task counts are not on the page.
 */

export const PROBE_TIMEOUT_MS = 4000;
export const STATUS_CACHE_MS = 15000;

// Fixed list: no caller-supplied path ever reaches the origin.
export const UPSTREAM_PROBES = [
  { id: "tasks", path: "/v1/tasks", auth: "none", resource: "task_api" },
  { id: "tasks_anonymous_stats", path: "/v1/tasks/stats", auth: "none", resource: "task_stats" },
  { id: "tasks_dashboard_stats", path: "/v1/tasks/stats", auth: "dashboard", resource: "task_stats" },
  { id: "mcp", path: "/mcp", auth: "none", resource: "mcp" },
  { id: "oauth_metadata", path: "/.well-known/oauth-protected-resource", auth: "none", resource: "oauth_metadata" },
  { id: "retired_health", path: "/v1/ai/health", auth: "dashboard", resource: "retired_v1_health" },
];

const VERDICTS = new Set([
  "reachable",
  "auth_required",
  "not_found",
  "redirect",
  "rate_limited",
  "server_error",
  "unexpected_status",
  "unreachable",
  "skipped",
]);

function withoutTrailingSlash(path) {
  return path.replace(/\/+$/, "");
}

/** Build a probe URL. Deliberately stricter than the proxy allowlist: the probe
    set is fixed above, so any other path never gets here. */
export function probeUrl(rawOrigin, path) {
  const trimmed = String(rawOrigin || "").trim();
  if (!trimmed) throw new Error("origin_not_configured");
  let origin;
  try {
    origin = new URL(trimmed);
  } catch (_error) {
    throw new Error("origin_not_configured");
  }
  if (origin.protocol !== "https:") throw new Error("origin_insecure");
  const base = withoutTrailingSlash(origin.pathname);
  origin.pathname = !base || base === "/" ? path : base + path;
  origin.search = "";
  origin.hash = "";
  return origin.toString();
}

export function classifyProbe(status) {
  if (!Number.isFinite(status)) return "unreachable";
  if (status >= 200 && status < 300) return "reachable";
  if (status === 401 || status === 403) return "auth_required";
  if (status === 404) return "not_found";
  if (status === 429) return "rate_limited";
  if (status >= 300 && status < 400) return "redirect";
  if (status >= 500) return "server_error";
  return "unexpected_status";
}

/** Why the page cannot show task counts. Every branch is decided by an
    observed verdict, never by an assumption about the deployment. */
export function readModel(probes) {
  const byId = new Map(probes.map((probe) => [probe.id, probe]));
  const tokenProbe = byId.get("tasks_dashboard_stats");
  const anonymousProbe = byId.get("tasks_anonymous_stats");
  if (!tokenProbe) return { task_counts: false, reason_code: "not_probed" };
  switch (tokenProbe.verdict) {
    case "reachable":
      return { task_counts: true, reason_code: null };
    case "skipped":
      return { task_counts: false, reason_code: "dashboard_token_missing" };
    case "not_found":
      return { task_counts: false, reason_code: "stats_route_absent" };
    case "unreachable":
      return { task_counts: false, reason_code: "origin_unreachable" };
    case "auth_required":
      return {
        task_counts: false,
        reason_code: anonymousProbe && anonymousProbe.verdict === "auth_required"
          ? "oauth_bearer_required"
          : "dashboard_token_rejected",
      };
    default:
      return { task_counts: false, reason_code: "unexpected_status" };
  }
}

export function overallState(probes) {
  const measured = probes.filter((probe) => probe.verdict !== "skipped");
  if (!measured.length) return "unknown";
  if (measured.every((probe) => probe.verdict === "unreachable")) return "unreachable";
  if (measured.some((probe) => probe.verdict === "reachable" || probe.verdict === "auth_required")) return "online";
  return "partial";
}

async function runProbe(definition, { origin, token, fetchImpl, now }) {
  const started = now();
  const base = { id: definition.id, path: definition.path, auth: definition.auth, resource: definition.resource };
  if (definition.auth === "dashboard" && !token) {
    return { ...base, verdict: "skipped", http_status: null, latency_ms: 0 };
  }
  let url;
  try {
    url = probeUrl(origin, definition.path);
  } catch (error) {
    return { ...base, verdict: "skipped", http_status: null, latency_ms: 0, probe_error: String(error.message || "") };
  }
  const headers = { Accept: "application/json" };
  if (definition.auth === "dashboard") headers.Authorization = "Bearer " + token;
  let response;
  try {
    response = await fetchImpl(url, {
      method: "GET",
      redirect: "manual",
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      headers,
    });
  } catch (_error) {
    return { ...base, verdict: "unreachable", http_status: null, latency_ms: Math.round(now() - started) };
  }
  const status = Number(response.status);
  // The body is never read: this is a reachability probe, not a data read.
  if (response.body && typeof response.body.cancel === "function") {
    try {
      await response.body.cancel();
    } catch (_error) {
      // A cancelled body is the normal path; nothing to report.
    }
  }
  const verdict = VERDICTS.has(classifyProbe(status)) ? classifyProbe(status) : "unexpected_status";
  return { ...base, verdict, http_status: status, latency_ms: Math.round(now() - started) };
}

export async function probeUpstream({
  origin,
  token,
  fetchImpl = fetch,
  now = () => Date.now(),
  checkedAt = new Date(),
} = {}) {
  let probes;
  try {
    // Validate the origin once so a missing or insecure origin is a single
    // machine code rather than one skipped row per probe.
    probeUrl(origin, "/");
    probes = await Promise.all(
      UPSTREAM_PROBES.map((definition) => runProbe(definition, {
        origin,
        token: String(token || "").trim(),
        fetchImpl,
        now,
      })),
    );
  } catch (error) {
    const code = String(error && error.message || "origin_not_configured");
    return {
      status: "error",
      error: "status_unavailable",
      error_code: code === "origin_insecure" ? "origin_insecure" : "origin_not_configured",
      checked_at: checkedAt.toISOString(),
      probes: [],
      read_model: { task_counts: false, reason_code: "origin_unavailable" },
      overall: "unknown",
    };
  }
  return {
    status: "ok",
    checked_at: checkedAt.toISOString(),
    overall: overallState(probes),
    probes,
    read_model: readModel(probes),
  };
}
