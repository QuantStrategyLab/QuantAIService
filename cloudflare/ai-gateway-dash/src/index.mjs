/** AiGateway Dashboard — Cloudflare Worker.

  GitHub OAuth 2.0 login — only QuantStrategyLab org members can access.
  Setup: create OAuth App at https://github.com/settings/developers
    - Homepage: https://quantstrategylab-ai-gateway-dash.pigbibi.workers.dev
    - Callback: https://quantstrategylab-ai-gateway-dash.pigbibi.workers.dev/callback
    - Set secrets: GITHUB_OAUTH_CLIENT_ID, GITHUB_OAUTH_CLIENT_SECRET
 */

import { loginHTML } from "./ui/login.mjs";
import { dashboardHTML } from "./ui/dashboard.mjs";
import { probeUpstream, STATUS_CACHE_MS, LEGACY_ENDPOINT_BASE } from "./upstream.mjs";

export {
  codexRemainingPercent,
  codexWindowLabel,
  codexWindowDisplay,
  codexRemainingClass,
  requiresHumanAudit,
} from "./metrics.mjs";

// ── Config ─────────────────────────────────────────────────────────────

const GITHUB_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize";
const GITHUB_OAUTH_ACCESS_TOKEN = "https://github.com/login/oauth/access_token";
const GITHUB_API_USER = "https://api.github.com/user";
const GITHUB_API_ORGS = "https://api.github.com/user/orgs";
export const REQUIRED_ORG = "QuantStrategyLab";
const COOKIE_NAME = "dash_session";
const STATE_COOKIE_NAME = "dash_oauth_state";
const COOKIE_MAX_AGE = 86400; // 24h
const SESSION_SECRET_LENGTH = 32;
const DIAGNOSIS_JOB_ID_PATTERN = /^[A-Za-z0-9_-]{32}$/;
const DIAGNOSIS_TASKS = new Map([
  ["operational_data_diagnosis", "operational"],
  ["historical_operational_diagnosis_rehearsal", "historical_rehearsal"],
]);
const DIAGNOSIS_SOURCE_REPOSITORY = "QuantStrategyLab/QuantAIService";
const DIAGNOSIS_MAX_RESPONSE_BYTES = 1_000_000;
const DASHBOARD_API_ROUTES = new Set([
  "/v1/ai/health",
  "/v1/ai/org-health",
  "/v1/ai/quota",
  "/v1/ai/changes",
  "/v1/ai/changes/effectiveness",
  "/v1/ai/feedback/shadow",
]);

// ── Helpers ────────────────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function html(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function redirect(url, headers = {}) {
  return new Response(null, { status: 302, headers: { Location: url, ...headers } });
}

function getCookie(request, name) {
  const cookie = request.headers.get("Cookie") || "";
  const match = cookie.match(new RegExp(name + "=([^;]+)"));
  return match ? match[1] : "";
}

function cookieValue(name, value, maxAge, { httpOnly = true } = {}) {
  const flags = ["Path=/", "Secure", "SameSite=Lax", "Max-Age=" + maxAge];
  if (httpOnly) flags.push("HttpOnly");
  return name + "=" + value + "; " + flags.join("; ");
}

function clearCookieValue(name) {
  return name + "=; Path=/; Secure; SameSite=Lax; HttpOnly; Max-Age=0";
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function base64UrlEncode(value) {
  const bytes = typeof value === "string" ? encoder.encode(value) : new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64UrlDecode(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return decoder.decode(bytes);
}

function constantTimeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function sessionSecret(env) {
  return String(env.DASHBOARD_SESSION_SECRET || "").trim();
}

async function hmacSha256(secret, data) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(data));
  return base64UrlEncode(signature);
}

async function githubAPI(path, token) {
  const resp = await fetch("https://api.github.com" + path, {
    headers: { Authorization: "Bearer " + token, "User-Agent": "AiGatewayDashboard", Accept: "application/vnd.github+json" },
  });
  if (!resp.ok) throw new Error("GitHub API " + resp.status);
  return resp.json();
}

function generateSessionToken() {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let result = "";
  const bytes = new Uint8Array(SESSION_SECRET_LENGTH);
  crypto.getRandomValues(bytes);
  for (let i = 0; i < SESSION_SECRET_LENGTH; i++) result += chars[bytes[i] % chars.length];
  return result;
}

function sessionKV(env) {
  const kv = env && env.DASHBOARD_SESSION_KV;
  return kv && typeof kv.get === "function" && typeof kv.put === "function" && typeof kv.delete === "function" ? kv : null;
}

function sessionKVKey(jti) {
  return "dash_session:" + jti;
}

async function parseSignedSession(token, env) {
  const secret = sessionSecret(env);
  if (!secret) return null;
  const [payload, signature] = String(token || "").split(".");
  if (!payload || !signature) return null;
  const expected = await hmacSha256(secret, payload);
  if (!constantTimeEqual(signature, expected)) return null;
  const session = JSON.parse(base64UrlDecode(payload));
  if (!session || Number(session.exp) < Date.now()) return null;
  return session;
}

async function createSession(user, env) {
  const secret = sessionSecret(env);
  if (!secret) throw new Error("Dashboard session secret not configured");
  const kv = sessionKV(env);
  const session = {
    login: user.login,
    avatar_url: user.avatar_url,
    exp: Date.now() + COOKIE_MAX_AGE * 1000,
  };
  if (kv) {
    session.jti = generateSessionToken();
    await kv.put(sessionKVKey(session.jti), JSON.stringify({ login: session.login, exp: session.exp }), { expirationTtl: COOKIE_MAX_AGE });
  }
  const payload = base64UrlEncode(JSON.stringify(session));
  return payload + "." + await hmacSha256(secret, payload);
}

async function getSession(token, env) {
  try {
    const session = await parseSignedSession(token, env);
    if (!session) return null;
    const kv = sessionKV(env);
    const jti = String(session.jti || "");
    if (jti) {
      if (!kv) return null;
      const active = await kv.get(sessionKVKey(jti));
      if (!active) return null;
    } else if (kv) {
      return null;
    }
    return { login: String(session.login || ""), avatar_url: String(session.avatar_url || ""), jti };
  } catch (e) {
    return null;
  }
}

async function revokeSession(token, env) {
  const kv = sessionKV(env);
  if (!kv) return;
  const session = await parseSignedSession(token, env);
  if (session && session.jti) await kv.delete(sessionKVKey(String(session.jti)));
}

// ── API proxy ──────────────────────────────────────────────────────────

function withoutTrailingSlash(pathname) {
  return pathname.replace(/\/+$/, "");
}

function allowedDashboardApiPath(pathname) {
  const clean = withoutTrailingSlash(pathname);
  return DASHBOARD_API_ROUTES.has(clean) ? clean : "";
}

function shouldIgnoreLegacyEndpointBase(basePath, pathname) {
  return basePath === LEGACY_ENDPOINT_BASE && pathname.startsWith("/v1/ai/");
}

export function buildDashboardApiUrl(rawOrigin, pathname, search = "") {
  if (!rawOrigin || !rawOrigin.trim()) throw new Error("AI_GATEWAY_ORIGIN_URL not configured");
  const clean = allowedDashboardApiPath(pathname);
  if (!clean) throw new Error("dashboard API route is not allowed");
  const origin = new URL(rawOrigin.trim());
  if (origin.protocol !== "https:") throw new Error("AI_GATEWAY_ORIGIN_URL must use HTTPS");
  const basePath = withoutTrailingSlash(origin.pathname);
  origin.pathname = !basePath || basePath === "/" || shouldIgnoreLegacyEndpointBase(basePath, clean) ? clean : basePath + clean;
  origin.search = search;
  origin.hash = "";
  return origin.toString();
}

function originReadHeaders(env) {
  const headers = {
    Accept: "application/json",
    "User-Agent": "AiGatewayDashboard/1.0",
  };
  const token = String(env.DASHBOARD_API_TOKEN || "").trim();
  if (token) headers.Authorization = "Bearer " + token;
  return { headers, token };
}

function parseOriginJsonBody(body) {
  const trimmed = String(body || "").trim();
  if (!trimmed) return { ok: false, reason: "empty" };
  try {
    const parsed = JSON.parse(trimmed);
    // Dashboard consumers expect a JSON object or array (including empty).
    if (parsed === null || typeof parsed !== "object") {
      return { ok: false, reason: "invalid" };
    }
    return { ok: true };
  } catch (_error) {
    return { ok: false, reason: "invalid" };
  }
}

function proxyFailure(errorCode, error, { originStatus } = {}) {
  const payload = { status: "error", error, error_code: errorCode };
  if (Number.isFinite(originStatus)) payload.origin_status = originStatus;
  return json(payload, 502);
}

async function proxyAPI(path, search, env) {
  // Fail closed with stable machine codes so authenticated Network readback can
  // distinguish misconfiguration, origin auth, and non-JSON upstream bodies
  // without restoring retired V1 handlers or widening privileges.
  let url;
  try {
    url = buildDashboardApiUrl(env.AI_GATEWAY_ORIGIN_URL || "", path, search);
  } catch (error) {
    const message = String(error && error.message || "proxy_misconfigured");
    if (message.includes("not configured")) {
      return proxyFailure("origin_not_configured", message);
    }
    if (message.includes("HTTPS")) {
      return proxyFailure("origin_insecure", message);
    }
    if (message.includes("not allowed")) {
      return proxyFailure("route_not_allowed", message);
    }
    return proxyFailure("proxy_misconfigured", "dashboard proxy failed");
  }

  const { headers, token } = originReadHeaders(env);
  if (!token) {
    return proxyFailure(
      "origin_token_missing",
      "DASHBOARD_API_TOKEN not configured",
    );
  }

  let resp;
  try {
    // Match diagnoses: never follow redirects into private Location targets.
    resp = await fetch(url, { headers, redirect: "manual" });
  } catch (_error) {
    return proxyFailure("origin_unreachable", "origin fetch failed");
  }

  let body;
  try {
    body = await resp.text();
  } catch (_error) {
    return proxyFailure(
      "origin_response_unreadable",
      "origin response could not be read",
      { originStatus: Number.isFinite(resp.status) ? resp.status : undefined },
    );
  }

  if (resp.status === 401 || resp.status === 403) {
    return proxyFailure(
      "origin_auth_failed",
      "后端认证失败：请同步 Dashboard API Token 与服务端静态访问 token",
      { originStatus: resp.status },
    );
  }
  if (resp.status >= 300 && resp.status < 400) {
    return proxyFailure(
      "upstream_redirect_rejected",
      "origin redirect rejected",
      { originStatus: resp.status },
    );
  }
  if (!String(body || "").trim()) {
    return proxyFailure("origin_empty_body", "origin returned an empty body", {
      originStatus: resp.status,
    });
  }
  if (!parseOriginJsonBody(body).ok) {
    return proxyFailure("origin_non_json", "origin returned a non-JSON body", {
      originStatus: resp.status,
    });
  }
  // Pass through JSON success (including valid empty payloads) and JSON error
  // bodies unchanged so empty data stays distinguishable from transport failure.
  return new Response(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

function diagnosisUpstreamUrl(env, jobId = "") {
  const rawOrigin = String(env.AI_GATEWAY_ORIGIN_URL || "").trim();
  if (!rawOrigin) throw new Error("origin_unavailable");
  const origin = new URL(rawOrigin);
  if (origin.protocol !== "https:") throw new Error("origin_unavailable");
  const basePath = withoutTrailingSlash(origin.pathname);
  const path = "/v1/ai/automation/runs" + (jobId ? "/" + jobId : "");
  origin.pathname = !basePath || basePath === "/" || shouldIgnoreLegacyEndpointBase(basePath, path)
    ? path
    : basePath + path;
  origin.search = jobId ? "" : "?limit=100";
  origin.hash = "";
  return origin.toString();
}

function diagnosisFailure(code, originStatus) {
  const error = new Error(code);
  error.code = code;
  if (Number.isFinite(originStatus)) error.origin_status = originStatus;
  return error;
}

async function fetchDiagnosisUpstream(env, jobId = "") {
  const { headers, token } = originReadHeaders(env);
  if (!token) throw diagnosisFailure("origin_token_missing");
  const response = await fetch(diagnosisUpstreamUrl(env, jobId), {
    method: "GET",
    redirect: "manual",
    signal: AbortSignal.timeout(5000),
    headers,
  });
  if (response.status === 401 || response.status === 403) {
    throw diagnosisFailure("origin_auth_failed", response.status);
  }
  if (response.status >= 300 && response.status < 400) throw diagnosisFailure("upstream_redirect_rejected");
  const declaredLength = Number(response.headers.get("content-length") || 0);
  if (declaredLength > DIAGNOSIS_MAX_RESPONSE_BYTES) throw diagnosisFailure("upstream_response_too_large");
  const chunks = [];
  let size = 0;
  const reader = response.body?.getReader();
  if (!reader) throw diagnosisFailure("upstream_response_invalid");
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > DIAGNOSIS_MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw diagnosisFailure("upstream_response_too_large");
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let payload = null;
  try {
    payload = JSON.parse(new TextDecoder().decode(bytes));
  } catch (e) {
    throw diagnosisFailure("upstream_response_invalid");
  }
  if (response.status === 404) return { missing: true, payload: null };
  if (!response.ok || !payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw diagnosisFailure("upstream_unavailable", response.status);
  }
  return { missing: false, payload };
}

function diagnosisErrorResponse(error) {
  const rawCode = String((error && (error.code || error.message)) || "diagnosis_unavailable");
  const known = new Set([
    "origin_token_missing",
    "origin_auth_failed",
    "origin_unavailable",
    "upstream_redirect_rejected",
    "upstream_response_too_large",
    "upstream_response_invalid",
    "upstream_unavailable",
  ]);
  const errorCode = known.has(rawCode) ? rawCode : "diagnosis_unavailable";
  const payload = {
    status: "error",
    error: "诊断数据暂不可用",
    error_code: errorCode === "origin_unavailable" ? "origin_not_configured" : errorCode,
  };
  if (Number.isFinite(error && error.origin_status)) payload.origin_status = error.origin_status;
  return json(payload, 502);
}

function isOperationalDiagnosisRun(run, expectedId = "") {
  if (!run || typeof run !== "object" || Array.isArray(run)) return false;
  const metadata = run.metadata;
  const runId = String(run.run_id || "");
  return DIAGNOSIS_JOB_ID_PATTERN.test(runId)
    && (!expectedId || runId === expectedId)
    && DIAGNOSIS_TASKS.has(run.task_name)
    && metadata && typeof metadata === "object" && !Array.isArray(metadata)
    && metadata.origin === "service_job"
    && metadata.source_repository === DIAGNOSIS_SOURCE_REPOSITORY
    && metadata.mode === "review_only";
}

function diagnosisKind(run) {
  return DIAGNOSIS_TASKS.get(run.task_name) || "unknown";
}

function diagnosisListItem(run) {
  const status = String(run.metadata?.diagnosis_status || "unknown");
  return {
    job_id: String(run.run_id),
    status: ["queued", "running", "succeeded", "failed"].includes(status) ? status : "unknown",
    updated_at: Number.isFinite(Number(run.updated_at)) ? Number(run.updated_at) : 0,
    kind: diagnosisKind(run),
  };
}

async function listDiagnoses(env) {
  const upstream = await fetchDiagnosisUpstream(env);
  const runs = upstream.payload?.ledger?.runs;
  if (!Array.isArray(runs)) throw new Error("upstream_response_invalid");
  const diagnoses = runs
    .filter(run => isOperationalDiagnosisRun(run))
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0))
    .slice(0, 20)
    .map(diagnosisListItem);
  return json({ status: "ok", diagnoses });
}

async function diagnosisDetail(env, jobId) {
  const upstream = await fetchDiagnosisUpstream(env, jobId);
  if (upstream.missing) {
    return json({ status: "ok", diagnosis: { job_id: jobId, status: "missing", message: "诊断记录已过期或不存在。", advisory: true } });
  }
  const run = upstream.payload?.run;
  if (!isOperationalDiagnosisRun(run, jobId)) return json({ status: "error", error: "diagnosis_not_found" }, 404);
  const metadata = run.metadata;
  const rawStatus = String(metadata.diagnosis_status || "unknown");
  const status = ["queued", "running", "succeeded", "failed"].includes(rawStatus) ? rawStatus : "unknown";
  const diagnosis = {
    job_id: jobId,
    status,
    updated_at: Number.isFinite(Number(run.updated_at)) ? Number(run.updated_at) : 0,
    advisory: true,
    kind: diagnosisKind(run),
  };
  if (status === "succeeded" && typeof metadata.diagnosis_summary === "string" && metadata.diagnosis_summary.trim()) {
    diagnosis.output = metadata.diagnosis_summary.slice(0, 500);
  } else if (status === "queued" || status === "running") {
    diagnosis.message = "诊断尚未完成。";
  } else if (status === "failed" || status === "blocked") {
    diagnosis.message = "诊断未完成，未提供结果摘要。";
  } else {
    diagnosis.message = "诊断摘要尚未提供。";
  }
  return json({ status: "ok", diagnosis });
}

// ── Upstream status (session-gated, read-only) ─────────────────────────

// One shared cache entry. The page polls every 30s and several members can open
// it at once, so a short window keeps the probe rate bounded without turning a
// manual refresh into a stale answer.
let upstreamStatusCache = { at: 0, payload: null };
let upstreamStatusInflight = null;

export function resetUpstreamStatusCache() {
  upstreamStatusCache = { at: 0, payload: null };
  upstreamStatusInflight = null;
}

async function upstreamStatus(env) {
  const at = Date.now();
  if (upstreamStatusCache.payload && at - upstreamStatusCache.at < STATUS_CACHE_MS) {
    return { ...upstreamStatusCache.payload, cached: true };
  }
  // Several viewers can poll while the first batch is still in flight. Join it
  // instead of each issuing six probes against the shared origin.
  if (upstreamStatusInflight) {
    const payload = await upstreamStatusInflight;
    return { ...payload, cached: false, shared: true };
  }
  const pending = probeUpstream({
    origin: env.AI_GATEWAY_ORIGIN_URL,
    token: env.DASHBOARD_API_TOKEN,
  });
  upstreamStatusInflight = pending;
  try {
    const payload = await pending;
    // Only a completed check is cacheable; a failure must be retried next poll.
    if (payload.status === "ok") upstreamStatusCache = { at: Date.now(), payload };
    return { ...payload, cached: false, shared: false };
  } finally {
    if (upstreamStatusInflight === pending) upstreamStatusInflight = null;
  }
}

// ── Main ───────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // ── OAuth callback ─────────────────────────────────────────────
    if (path === "/callback") {
      const code = url.searchParams.get("code");
      const state = url.searchParams.get("state") || "";
      const expectedState = getCookie(request, STATE_COOKIE_NAME);
      if (!code) return redirect("/?error=" + encodeURIComponent("缺少授权码"));
      if (!state || !expectedState || state !== expectedState) {
        return redirect("/?error=" + encodeURIComponent("OAuth state 校验失败"));
      }

      try {
        // Exchange code for access token
        const tokenResp = await fetch(GITHUB_OAUTH_ACCESS_TOKEN, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({
            client_id: env.GITHUB_OAUTH_CLIENT_ID,
            client_secret: env.GITHUB_OAUTH_CLIENT_SECRET,
            code,
          }),
        });
        const tokenData = await tokenResp.json();
        if (tokenData.error) throw new Error(tokenData.error_description || tokenData.error);

        const accessToken = tokenData.access_token;

        // Get user info + org membership
        const [user, orgs] = await Promise.all([
          githubAPI("/user", accessToken),
          githubAPI("/user/orgs", accessToken),
        ]);

        const isMember = orgs.some(o => o.login === REQUIRED_ORG);
        if (!isMember) {
          return redirect("/?error=" + encodeURIComponent("仅限 " + REQUIRED_ORG + " 组织成员访问"));
        }

        // Create session
        const sessionToken = await createSession(user, env);
        const headers = new Headers({ Location: "/" });
        headers.append("Set-Cookie", cookieValue(COOKIE_NAME, sessionToken, COOKIE_MAX_AGE));
        headers.append("Set-Cookie", clearCookieValue(STATE_COOKIE_NAME));
        return new Response(null, {
          status: 302,
          headers,
        });
      } catch (e) {
        return redirect("/?error=" + encodeURIComponent("登录失败: " + e.message));
      }
    }

    // ── Login redirect ─────────────────────────────────────────────
    if (path === "/login") {
      const clientId = env.GITHUB_OAUTH_CLIENT_ID;
      if (!clientId) return html("<h1>OAuth 未配置</h1><p>请设置 GITHUB_OAUTH_CLIENT_ID 和 GITHUB_OAUTH_CLIENT_SECRET 环境变量</p>", 500);
      const state = generateSessionToken();
      const authUrl = GITHUB_OAUTH_AUTHORIZE
        + "?client_id=" + encodeURIComponent(clientId)
        + "&redirect_uri=" + encodeURIComponent(url.origin + "/callback")
        + "&scope=read:org"
        + "&state=" + encodeURIComponent(state);
      return redirect(authUrl, { "Set-Cookie": cookieValue(STATE_COOKIE_NAME, state, 600) });
    }

    // ── Logout ─────────────────────────────────────────────────────
    if (path === "/logout") {
      try {
        await revokeSession(getCookie(request, COOKIE_NAME), env);
      } catch (e) {
        return json({ error: "session_revocation_failed" }, 503);
      }
      return new Response(null, {
        status: 302,
        headers: {
          Location: "/",
          "Set-Cookie": clearCookieValue(COOKIE_NAME),
        },
      });
    }

    // ── User info (for dashboard header) ───────────────────────────
    if (path === "/api/user") {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
      if (!session) return json({ error: "unauthorized" }, 401);
      return json({ login: session.login, avatar_url: session.avatar_url });
    }

    // ── Bounded diagnosis read API (requires session) ─────────────
    if (path === "/api/diagnoses" || path.startsWith("/api/diagnoses/")) {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
      if (!session) return json({ status: "error", error: "unauthorized" }, 401);
      if (request.method !== "GET") {
        return new Response(null, { status: 405, headers: { Allow: "GET" } });
      }
      try {
        if (path === "/api/diagnoses") return await listDiagnoses(env);
        const jobId = path.slice("/api/diagnoses/".length);
        if (!DIAGNOSIS_JOB_ID_PATTERN.test(jobId)) {
          return json({ status: "error", error: "diagnosis_not_found" }, 404);
        }
        return await diagnosisDetail(env, jobId);
      } catch (e) {
        return diagnosisErrorResponse(e);
      }
    }

    // ── Upstream status (requires session, read-only) ──────────────
    if (path === "/api/upstream-status") {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
      if (!session) return json({ status: "error", error: "unauthorized" }, 401);
      if (request.method !== "GET") {
        return new Response(null, { status: 405, headers: { Allow: "GET" } });
      }
      try {
        const payload = await upstreamStatus(env);
        return json(payload, payload.status === "ok" ? 200 : 502);
      } catch (_error) {
        // Never echo exception text: synthetic host/token strings must not appear.
        return json({ status: "error", error: "status_unavailable", error_code: "probe_failed" }, 502);
      }
    }

    // ── API proxy (requires session) ───────────────────────────────
    if (path.startsWith("/api/")) {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
      if (!session) return json({ status: "error", error: "unauthorized" }, 401);

      const apiPath = path.slice(4);
      try {
        return await proxyAPI(apiPath, url.search, env);
      } catch (_error) {
        // Never echo exception text: synthetic host/token strings must not appear.
        return proxyFailure("proxy_misconfigured", "dashboard proxy failed");
      }
    }

    // ── Dashboard HTML (requires session) ──────────────────────────
    if (path === "/" || path === "") {
      const token = getCookie(request, COOKIE_NAME);
      const session = await getSession(token, env);
      if (!session) return html(loginHTML());
      return html(dashboardHTML());
    }

    return new Response("Not found", { status: 404 });
  },
};
