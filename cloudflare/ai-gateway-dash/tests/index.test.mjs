import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";

class FakeKV {
  constructor(options = {}) {
    this.store = new Map();
    this.failDelete = Boolean(options.failDelete);
  }

  async get(key) {
    return this.store.get(key) || null;
  }

  async put(key, value) {
    this.store.set(key, value);
  }

  async delete(key) {
    if (this.failDelete) throw new Error("delete failed");
    this.store.delete(key);
  }
}

async function authenticatedDashboard(t, upstreamFetch) {
  const originalFetch = globalThis.fetch;
  const gatewayCalls = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json({ access_token: "github-token" });
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    gatewayCalls.push({ url, init });
    return upstreamFetch(url, init);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  const env = {
    GITHUB_OAUTH_CLIENT_ID: "client",
    GITHUB_OAUTH_CLIENT_SECRET: "oauth-client-test-value",
    DASHBOARD_SESSION_SECRET: "test-session-signing-value",
    DASHBOARD_SESSION_KV: new FakeKV(),
    DASHBOARD_API_TOKEN: "dashboard-api-token",
    AI_GATEWAY_ORIGIN_URL: "https://gateway.example",
  };
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);
  return { env, cookie: "dash_session=" + session, gatewayCalls };
}

import worker, {
  REQUIRED_ORG,
  buildDashboardApiUrl,
  codexRemainingClass,
  codexRemainingPercent,
  codexWindowDisplay,
  requiresHumanAudit,
  resetUpstreamStatusCache,
} from "../src/index.mjs";
import { LOCALES } from "../src/ui/locales.mjs";
import {
  LOGIN_STATES,
  loginHTML,
  loginNotice,
  loginSteps,
  resolveLoginAttestation,
  resolveLoginOrigin,
  resolveLoginRef,
  resolveLoginState,
  stepSegments,
} from "../src/ui/login.mjs";
import { LOGIN_LOCALES } from "../src/ui/login-locales.mjs";
import { dashboardHTML } from "../src/ui/dashboard.mjs";
import { classifyProbe, probeUrl, readModel, LEGACY_ENDPOINT_BASE } from "../src/upstream.mjs";
import {
  classifyLoginError,
  escapeHtml,
  healthReadouts,
  jsObjectLiteral,
  maskIdentifier,
  quotaRowModel,
  safeHttpUrl,
  tokensFromQuota,
} from "../src/ui/format.mjs";

test("buildDashboardApiUrl preserves query strings for allowed read routes", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example/base", "/v1/ai/changes", "?days=7"),
    "https://origin.example/base/v1/ai/changes?days=7",
  );
});

test("buildDashboardApiUrl allows effectiveness route with query", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example", "/v1/ai/changes/effectiveness", "?days=90"),
    "https://origin.example/v1/ai/changes/effectiveness?days=90",
  );
});

test("buildDashboardApiUrl maps AiGateway routes at root when origin is legacy audit endpoint", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example/v1/codex-audit", "/v1/ai/health", ""),
    "https://origin.example/v1/ai/health",
  );
});

test("buildDashboardApiUrl rejects unsupported origin paths", () => {
  assert.throws(
    () => buildDashboardApiUrl("https://origin.example", "/v1/ai/execute/jobs", ""),
    /not allowed/,
  );
});

test("buildDashboardApiUrl rejects insecure origins", () => {
  assert.throws(
    () => buildDashboardApiUrl("http://origin.example", "/v1/ai/health", ""),
    /HTTPS/,
  );
});

test("buildDashboardApiUrl allows org health route", () => {
  assert.equal(
    buildDashboardApiUrl("https://origin.example", "/v1/ai/org-health", ""),
    "https://origin.example/v1/ai/org-health",
  );
});

test("codex window display shows remaining quota instead of used percent", () => {
  assert.equal(
    codexWindowDisplay({ used_percent: 44, window_duration_mins: 300 }),
    "5h 剩余 56%",
  );
});

test("codex window display prefers explicit remaining percent", () => {
  assert.equal(
    codexRemainingPercent({ used_percent: 44, remaining_percent: 57 }),
    57,
  );
  assert.equal(codexRemainingPercent({ used_percent: 44, remaining_percent: null }), 56);
});

test("codex quota severity is based on low remaining quota", () => {
  assert.equal(codexRemainingClass({ remaining_percent: 57 }), "ok");
  assert.equal(codexRemainingClass({ remaining_percent: 30 }), "warn");
  assert.equal(codexRemainingClass({ remaining_percent: 8 }), "err");
});

test("human audit display trusts backend decision fields", () => {
  assert.equal(requiresHumanAudit({ human_review_required: false, risk: "critical" }), false);
  assert.equal(requiresHumanAudit({ human_review_required: true, risk: "low" }), true);
  assert.equal(requiresHumanAudit({ human_review_required: "false", risk: "low" }), false);
  assert.equal(requiresHumanAudit({ human_review_required: null, risk: "critical" }), true);
  assert.equal(requiresHumanAudit({ state: "human_review_waiting_for_ci" }), true);
  assert.equal(requiresHumanAudit({ risk: "high" }), true);
  assert.equal(requiresHumanAudit({ risk: "low" }), false);
});

test("diagnosis API requires a session and rejects writes or invalid ids without upstream calls", async (t) => {
  const unauthenticated = await worker.fetch(
    new Request("https://dash.example/api/diagnoses"),
    {},
  );
  assert.equal(unauthenticated.status, 401);

  const dashboard = await authenticatedDashboard(t, async () => {
    throw new Error("upstream must not be called");
  });
  const write = await worker.fetch(
    new Request("https://dash.example/api/diagnoses", { method: "POST", headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(write.status, 405);
  const invalid = await worker.fetch(
    new Request("https://dash.example/api/diagnoses/not-a-job", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(invalid.status, 404);
  assert.equal(dashboard.gatewayCalls.length, 0);
});

test("diagnosis list exposes only bounded AIAuditBridge operational jobs", async (t) => {
  const validId = "A".repeat(32);
  const dashboard = await authenticatedDashboard(t, async (url) => {
    assert.match(url, /\/v1\/ai\/automation\/runs\?limit=100$/);
    return Response.json({ status: "ok", ledger: { runs: [
      { run_id: validId, task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 10,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/QuantAIService", mode: "review_only", diagnosis_status: "succeeded" } },
      { run_id: "F".repeat(32), task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 9,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/QuantAIService", mode: "review_only" } },
      { run_id: "B".repeat(32), task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 20,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/Other", mode: "review_only", private: "do-not-leak" } },
      { run_id: "C".repeat(32), task_name: "other", task_state: "failed", updated_at: 30,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/QuantAIService", mode: "review_only" } },
      { run_id: "R".repeat(32), task_name: "historical_operational_diagnosis_rehearsal", task_state: "reviewed", updated_at: 11,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/QuantAIService", mode: "review_only", diagnosis_status: "succeeded" } },
    ] } });
  });
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    status: "ok",
    diagnoses: [
      { job_id: "R".repeat(32), status: "succeeded", updated_at: 11, kind: "historical_rehearsal" },
      { job_id: validId, status: "succeeded", updated_at: 10, kind: "operational" },
      { job_id: "F".repeat(32), status: "unknown", updated_at: 9, kind: "operational" },
    ],
  });
});

test("diagnosis detail verifies ledger and job identity before exposing successful text", async (t) => {
  const jobId = "D".repeat(32);
  const output = "<img src=x onerror=alert(1)> read-only advice";
  const dashboard = await authenticatedDashboard(t, async (url) => {
    if (url.endsWith("/v1/ai/automation/runs/" + jobId)) {
      return Response.json({ status: "ok", run: {
        run_id: jobId, task_name: "operational_data_diagnosis", task_state: "reviewed", updated_at: 40,
        metadata: { origin: "service_job", source_repository: "QuantStrategyLab/QuantAIService", mode: "review_only",
          diagnosis_status: "succeeded", diagnosis_summary: output },
      } });
    }
    throw new Error("unexpected upstream " + url);
  });
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses/" + jobId, { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    status: "ok",
    diagnosis: { job_id: jobId, status: "succeeded", updated_at: 40, output, advisory: true, kind: "operational" },
  });

  const html = await worker.fetch(
    new Request("https://dash.example/", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  const body = await html.text();
  assert.match(body, /数据故障诊断/);
  assert.match(body, /历史故障诊断演练/);
  assert.match(body, /diagnosis-output/);
  assert.match(body, /textContent/);
});

test("historical diagnosis rehearsal detail remains separate and read only", async (t) => {
  const jobId = "H".repeat(32);
  const dashboard = await authenticatedDashboard(t, async () => Response.json({ status: "ok", run: {
    run_id: jobId, task_name: "historical_operational_diagnosis_rehearsal", task_state: "reviewed", updated_at: 60,
    metadata: { origin: "service_job", source_repository: "QuantStrategyLab/QuantAIService", mode: "review_only",
      diagnosis_status: "succeeded", diagnosis_summary: "<script>历史演练摘要</script>" },
  } }));
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses/" + jobId, { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.deepEqual(await response.json(), { status: "ok", diagnosis: {
    job_id: jobId, status: "succeeded", updated_at: 60, advisory: true,
    kind: "historical_rehearsal", output: "<script>历史演练摘要</script>",
  } });
});

test("diagnosis detail hides mismatched, running, failed, missing, and upstream error data", async (t) => {
  const jobId = "E".repeat(32);
  let scenario = "mismatch";
  const dashboard = await authenticatedDashboard(t, async (url) => {
    if (url.includes("/automation/runs/")) {
      if (scenario === "upstream-error") return new Response("private upstream error", { status: 500 });
      if (scenario === "missing") return Response.json({ status: "error", error: "private missing detail" }, { status: 404 });
      return Response.json({ status: "ok", run: {
        run_id: jobId, task_name: "operational_data_diagnosis", task_state: scenario === "failed" ? "failed" : "running", updated_at: 50,
        metadata: { origin: "service_job", source_repository: scenario === "mismatch" ? "QuantStrategyLab/Other" : "QuantStrategyLab/QuantAIService", mode: "review_only",
          diagnosis_status: scenario, diagnosis_summary: "private output" },
      } });
    }
    throw new Error("unexpected upstream " + url);
  });

  for (const [value, expectedStatus] of [["mismatch", 404], ["running", 200], ["failed", 200], ["missing", 200], ["upstream-error", 502]]) {
    scenario = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/diagnoses/" + jobId, { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, expectedStatus, value);
    const text = await response.text();
    assert.doesNotMatch(text, /private|traceback|prompt/, value);
  }
});

test("diagnosis list returns an empty result without claiming successful automation", async (t) => {
  const dashboard = await authenticatedDashboard(t, async () => Response.json({ status: "ok", ledger: { runs: [] } }));
  const response = await worker.fetch(
    new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.deepEqual(await response.json(), { status: "ok", diagnoses: [] });
});

test("diagnosis API rejects redirects and oversized upstream responses", async (t) => {
  let scenario = "redirect";
  const dashboard = await authenticatedDashboard(t, async () => scenario === "redirect"
    ? new Response(null, { status: 302, headers: { Location: "https://private.example/detail" } })
    : new Response("{}", { headers: { "Content-Length": "1000001" } }));
  for (const [value, errorCode] of [
    ["redirect", "upstream_redirect_rejected"],
    ["oversized", "upstream_response_too_large"],
  ]) {
    scenario = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 502, value);
    assert.deepEqual(await response.json(), {
      status: "error",
      error: "诊断数据暂不可用",
      error_code: errorCode,
    });
  }
});

const PANEL_PROXY_ROUTES = [
  "/api/v1/ai/health",
  "/api/v1/ai/org-health",
  "/api/v1/ai/quota",
  "/api/v1/ai/changes?days=7",
  "/api/v1/ai/changes/effectiveness?days=90",
  "/api/v1/ai/feedback/shadow",
];

const EMPTY_PANEL_PAYLOADS = {
  "/v1/ai/health": { status: "ok", endpoints: [], degradation_reasons: [] },
  "/v1/ai/org-health": {
    status: "ok",
    provider: { status: "unavailable" },
    summary: {},
    repositories: [],
  },
  "/v1/ai/quota": { status: "ok", quota: { repos: {}, summary: {} } },
  "/v1/ai/changes": { status: "ok", changes: [] },
  "/v1/ai/changes/effectiveness": {
    status: "ok",
    report: { evaluated: 0, pending: 0, total_changes: 0, improvement_rate: null },
  },
  "/v1/ai/feedback/shadow": { status: "ok", disagreements: [] },
};

function panelUpstreamPath(url) {
  const parsed = new URL(url);
  return parsed.pathname.replace(/\/+$/, "");
}

test("panel proxy passes valid empty JSON payloads without inventing errors", async (t) => {
  const dashboard = await authenticatedDashboard(t, async (url, init) => {
    assert.match(String(init.headers.Authorization || ""), /^Bearer /);
    const path = panelUpstreamPath(url);
    const payload = EMPTY_PANEL_PAYLOADS[path];
    assert.ok(payload, path);
    return Response.json(payload);
  });
  for (const route of PANEL_PROXY_ROUTES) {
    const response = await worker.fetch(
      new Request("https://dash.example" + route, { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 200, route);
    assert.match(response.headers.get("Content-Type") || "", /application\/json/);
    const body = await response.json();
    assert.notEqual(body.status, "error", route);
    assert.equal(body.error_code, undefined, route);
  }
});

test("panel proxy counterexamples label auth HTML and empty-body failures without claiming production cause", async (t) => {
  // Offline fixtures only: these prove Worker error categories, not live root cause.
  let mode = "auth-401";
  const dashboard = await authenticatedDashboard(t, async () => {
    if (mode === "auth-401") return Response.json({ status: "error", error: "denied" }, { status: 401 });
    if (mode === "auth-403") return Response.json({ status: "error", error: "forbidden" }, { status: 403 });
    if (mode === "html-login") {
      return new Response("<!doctype html><title>login</title>", {
        status: 200,
        headers: { "Content-Type": "text/html" },
      });
    }
    if (mode === "empty") return new Response("", { status: 200 });
    if (mode === "server-json") {
      return Response.json({ status: "error", error: "upstream boom" }, { status: 500 });
    }
    throw new Error("unexpected mode " + mode);
  });

  const cases = [
    ["auth-401", 502, "origin_auth_failed", 401],
    ["auth-403", 502, "origin_auth_failed", 403],
    ["html-login", 502, "origin_non_json", 200],
    ["empty", 502, "origin_empty_body", 200],
  ];
  for (const [value, status, errorCode, originStatus] of cases) {
    mode = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/v1/ai/health", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, status, value);
    assert.deepEqual(await response.json(), {
      status: "error",
      error: errorCode === "origin_auth_failed"
        ? "后端认证失败：请同步 Dashboard API Token 与服务端静态访问 token"
        : errorCode === "origin_non_json"
          ? "origin returned a non-JSON body"
          : "origin returned an empty body",
      error_code: errorCode,
      origin_status: originStatus,
    });
  }

  mode = "server-json";
  const serverJson = await worker.fetch(
    new Request("https://dash.example/api/v1/ai/quota", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(serverJson.status, 500);
  assert.deepEqual(await serverJson.json(), { status: "error", error: "upstream boom" });
});

test("panel proxy and diagnoses omit empty Authorization and report missing token", async (t) => {
  const dashboard = await authenticatedDashboard(t, async () => {
    throw new Error("origin must not be called without a dashboard token");
  });
  dashboard.env.DASHBOARD_API_TOKEN = "   ";
  for (const route of ["/api/v1/ai/health", "/api/diagnoses"]) {
    const response = await worker.fetch(
      new Request("https://dash.example" + route, { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 502, route);
    const body = await response.json();
    assert.equal(body.status, "error", route);
    assert.equal(body.error_code, "origin_token_missing", route);
  }
});

test("diagnoses auth failures use the same origin_auth_failed category as panel proxy", async (t) => {
  let status = 401;
  const dashboard = await authenticatedDashboard(t, async () => Response.json({ error: "nope" }, { status }));
  for (const value of [401, 403]) {
    status = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 502, String(value));
    assert.deepEqual(await response.json(), {
      status: "error",
      error: "诊断数据暂不可用",
      error_code: "origin_auth_failed",
      origin_status: value,
    });
  }
});

test("unauthenticated panel and diagnoses routes stay 401 JSON unauthorized", async () => {
  for (const route of [...PANEL_PROXY_ROUTES, "/api/diagnoses"]) {
    const response = await worker.fetch(new Request("https://dash.example" + route), {});
    assert.equal(response.status, 401, route);
    assert.deepEqual(await response.json(), { status: "error", error: "unauthorized" });
  }
});

test("panel proxy rejects truncated JSON that only looks like an object or array", async (t) => {
  // Adversarial fixtures: first-char heuristics must not classify these as JSON.
  const samples = ["{broken", "{", "[", "  {broken", "\n[1,2,"];
  let index = 0;
  const dashboard = await authenticatedDashboard(t, async () => {
    return new Response(samples[index], {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  for (const sample of samples) {
    const response = await worker.fetch(
      new Request("https://dash.example/api/v1/ai/health", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 502, sample);
    assert.deepEqual(await response.json(), {
      status: "error",
      error: "origin returned a non-JSON body",
      error_code: "origin_non_json",
      origin_status: 200,
    });
    index += 1;
  }
});

test("panel proxy still accepts valid empty object and array JSON bodies", async (t) => {
  let body = "{}";
  const dashboard = await authenticatedDashboard(t, async () => new Response(body, {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }));
  for (const sample of ["{}", "[]", " {\n}\n", "\n[]\n"]) {
    body = sample;
    const response = await worker.fetch(
      new Request("https://dash.example/api/v1/ai/quota", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 200, sample);
    assert.deepEqual(await response.json(), JSON.parse(sample));
  }
});

test("panel proxy rejects redirects with the same bounded contract as diagnoses", async (t) => {
  const dashboard = await authenticatedDashboard(t, async () => new Response(null, {
    status: 302,
    headers: { Location: "https://private.example/login?token=should-not-leak" },
  }));
  const response = await worker.fetch(
    new Request("https://dash.example/api/v1/ai/health", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(response.status, 502);
  const payload = await response.json();
  assert.deepEqual(payload, {
    status: "error",
    error: "origin redirect rejected",
    error_code: "upstream_redirect_rejected",
    origin_status: 302,
  });
  assert.doesNotMatch(JSON.stringify(payload), /private\.example|token=|should-not-leak/);
  const panelFetch = dashboard.gatewayCalls.find((call) => String(call.url).includes("/v1/ai/health"));
  assert.ok(panelFetch);
  assert.equal(panelFetch.init.redirect, "manual");
});

test("panel proxy response-body read failures stay generic and never echo secrets", async (t) => {
  const secret = "syn-secret-host.example/token=leak-value";
  const dashboard = await authenticatedDashboard(t, async () => ({
    status: 200,
    headers: { get() { return "application/json"; } },
    async text() {
      throw new Error(secret);
    },
  }));
  const response = await worker.fetch(
    new Request("https://dash.example/api/v1/ai/changes?days=7", { headers: { Cookie: dashboard.cookie } }),
    dashboard.env,
  );
  assert.equal(response.status, 502);
  const payload = await response.json();
  assert.equal(payload.status, "error");
  assert.equal(payload.error_code, "origin_response_unreadable");
  assert.doesNotMatch(JSON.stringify(payload), /syn-secret-host|leak-value|token=/);
});

test("authenticated dashboard html ships codex remaining quota display", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const env = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
    ["DASHBOARD_SESSION_KV", new FakeKV()],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const user = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(user.status, 200);
  assert.equal((await user.json()).login, "operator");

  const dashboard = await worker.fetch(
    new Request("https://dash.example/", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  const html = await dashboard.text();
  assert.equal(dashboard.status, 200);
  assert.match(html, /id="probe-table"/);
  assert.match(html, /上游状态/);
  assert.match(html, /id="read-model"/);

  const tampered = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session + "x" } }),
    env,
  );
  assert.equal(tampered.status, 401);

  const logout = await worker.fetch(
    new Request("https://dash.example/logout", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(logout.status, 302);

  const revoked = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(revoked.status, 401, "logout revokes the active signed session when KV is bound");
});

test("dashboard callback requires dedicated session signing secret", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const env = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  assert.equal(callback.status, 302);
  assert.match(callback.headers.get("location") || "", /error=/);
  assert.doesNotMatch(callback.headers.get("set-cookie") || "", /dash_session=/);
});

test("sessions issued before KV binding require re-login", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const baseEnv = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    baseEnv,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const beforeKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    baseEnv,
  );
  assert.equal(beforeKv.status, 200);

  const kvEnv = Object.fromEntries([...Object.entries(baseEnv), ["DASHBOARD_SESSION_KV", new FakeKV()]]);
  const afterKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    kvEnv,
  );
  assert.equal(afterKv.status, 401);
});

test("revocable sessions fail closed when KV binding is removed", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const baseEnv = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
  ]);
  const kvEnv = Object.fromEntries([...Object.entries(baseEnv), ["DASHBOARD_SESSION_KV", new FakeKV()]]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    kvEnv,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const withKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    kvEnv,
  );
  assert.equal(withKv.status, 200);

  const withoutKv = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    baseEnv,
  );
  assert.equal(withoutKv.status, 401);
});

test("logout surfaces KV revocation failures", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.includes("/login/oauth/access_token")) {
      return Response.json(Object.fromEntries([["access_token", "github-token"]]));
    }
    if (url.endsWith("/user")) {
      return Response.json({ login: "operator", avatar_url: "https://example.test/avatar.png" });
    }
    if (url.endsWith("/user/orgs")) {
      return Response.json([{ login: REQUIRED_ORG }]);
    }
    throw new Error("unexpected fetch " + url);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const env = Object.fromEntries([
    ["GITHUB_OAUTH_CLIENT_ID", "client"],
    ["GITHUB_OAUTH_CLIENT_SECRET", "oauth-client-test-value"],
    ["DASHBOARD_SESSION_SECRET", "test-session-signing-value"],
    ["DASHBOARD_SESSION_KV", new FakeKV({ failDelete: true })],
  ]);
  const callback = await worker.fetch(
    new Request("https://dash.example/callback?code=code&state=state", {
      headers: { Cookie: "dash_oauth_state=state" },
    }),
    env,
  );
  const session = /dash_session=([^;]+)/.exec(callback.headers.get("set-cookie") || "")?.[1];
  assert.ok(session);

  const logout = await worker.fetch(
    new Request("https://dash.example/logout", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(logout.status, 503);
  assert.equal((await logout.json()).error, "session_revocation_failed");

  const stillActive = await worker.fetch(
    new Request("https://dash.example/api/user", { headers: { Cookie: "dash_session=" + session } }),
    env,
  );
  assert.equal(stillActive.status, 200);
});

test("locales keep zh-CN and en key parity with plain-text values", () => {
  const zhKeys = Object.keys(LOCALES["zh-CN"]).sort();
  const enKeys = Object.keys(LOCALES.en).sort();
  assert.deepEqual(enKeys, zhKeys);
  for (const [locale, dictionary] of Object.entries(LOCALES)) {
    for (const [key, value] of Object.entries(dictionary)) {
      assert.equal(typeof value, "string", locale + "." + key);
      assert.doesNotMatch(value, /[<>]/, locale + "." + key);
    }
  }
  assert.equal(LOCALES["zh-CN"].remaining, "剩余");
  assert.equal(LOCALES["zh-CN"].k_p95_f, "进程内 · 5 分钟窗口");
  assert.equal(LOCALES.en.k_p95_f, "process-local · 5-min window");
});

test("login page exposes GitHub OAuth as the only sign-in action", () => {
  const page = loginHTML();
  assert.equal((page.match(/href="\/login"/g) || []).length, 1);
  assert.match(page, /class="gh" id="gh" href="\/login"/);
  assert.doesNotMatch(page, /<(?:form|input|select|textarea)\b/i);
  assert.doesNotMatch(page, /type="(?:password|email)"/i);
  assert.doesNotMatch(page, /autocomplete="(?:current-password|username)"/i);
  assert.doesNotMatch(page, /mailto:|name="token"/i);
  assert.doesNotMatch(page, /<(?:link|img|iframe|object|embed)\b/i);
  assert.doesNotMatch(page, /(?:href|src)\s*=\s*["']?https?:/i);
  assert.doesNotMatch(page, /@import/i);
  assert.match(page, /<html lang="zh-CN" data-theme="light">/);
  assert.match(page, /<title>使用 GitHub 登录 · QSL 网关<\/title>/);
  assert.match(page, /id="note" role="status" aria-live="polite"/);
  assert.match(page, /aria-labelledby="flow-h"/);
  assert.match(page, /class="legal"><svg viewBox="0 0 12 12" aria-hidden="true">/);
  assert.doesNotMatch(page, /st-ok|lamp/);
  assert.match(page, /function classifyLoginError/);
  assert.match(page, /function resolveLoginState/);
  assert.match(page, /function stepSegments/);
  assert.match(page, /resolveLoginState\(location\.search\)/);
  assert.doesNotMatch(page, /innerHTML/);
  assert.match(page, /textContent/);
  assert.match(page, /qg-lang/);
  assert.match(page, /qg-theme/);
  const storageKeys = [...page.matchAll(/localStorage\.(?:getItem|setItem)\("([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual([...new Set(storageKeys)].sort(), ["qg-lang", "qg-theme"]);
  assert.match(page, /root\.lang=lang/);
  assert.match(page, /document\.title=t\.l_title\+" · "\+t\.brand/);
  assert.match(page, /gh\.tabIndex=-1/);
  assert.match(page, /gh\.removeAttribute\("href"\)/);
  assert.match(page, /aria-disabled/);
  assert.match(page, /notice\.tone==="bad"/);
  assert.match(page, /setAttribute\("role","alert"\)/);
  assert.match(page, /\/v1\/ai\/\*/);
  assert.doesNotMatch(page, /\/v2\/ai/);
  assert.doesNotMatch(page, /read:user/);
  assert.match(page, /<code>read:org<\/code>/);
  assert.match(page, /<code>dashboard:read<\/code>/);
});

test("login states match the approved nine-state table", () => {
  assert.deepEqual(Object.keys(LOGIN_STATES), [
    "idle", "redirecting", "verifying", "denied", "cancel", "error", "expired", "signedout", "unavailable",
  ]);
  const expected = {
    idle: { tone: null, btn: "go", steps: ["na", "na", "na"] },
    redirecting: { tone: "info", btn: "busy", steps: ["run", "wait", "wait"] },
    verifying: { tone: "info", btn: "verify", steps: ["done", "run", "wait"] },
    denied: { tone: "bad", btn: "retry", steps: ["done", "fail", "na"] },
    cancel: { tone: "warn", btn: "go", steps: ["stop", "na", "na"] },
    error: { tone: "bad", btn: "retry", steps: ["fail", "na", "na"] },
    expired: { tone: "warn", btn: "go", steps: ["na", "na", "na"] },
    signedout: { tone: "info", btn: "go", steps: ["na", "na", "na"] },
    unavailable: { tone: "unk", btn: "off", steps: ["na", "na", "na"] },
  };
  assert.deepEqual(LOGIN_STATES, expected);
  for (const spec of Object.values(LOGIN_STATES)) {
    assert.equal(spec.ref, undefined, "references are runtime-only, never invented");
  }
});

test("login error classification never reflects raw query text", () => {
  assert.equal(resolveLoginState(""), "idle");
  assert.equal(resolveLoginState("?error=not_a_member"), "denied");
  assert.equal(resolveLoginState("?error=access_denied"), "cancel");
  assert.equal(resolveLoginState("?error=session_expired"), "expired");
  assert.equal(resolveLoginState("?error=%3Cscript%3Ealert(1)%3C%2Fscript%3E"), "error");
  assert.equal(resolveLoginState("?state=unavailable&error=not_a_member"), "denied");
});

test("login state resolution accepts the nine design states and fails closed", () => {
  assert.equal(resolveLoginState("?state=idle"), "idle");
  for (const state of ["redirecting", "verifying", "denied", "cancel", "error", "expired", "signedout", "unavailable"]) {
    assert.equal(resolveLoginState("?state=" + state), state);
  }
  for (const state of ["unknown", "__proto__", "constructor", "toString", "0", "idle "]) {
    assert.equal(resolveLoginState("?state=" + state), "error", state);
  }
  assert.equal(resolveLoginState("?state="), "idle");
  assert.equal(resolveLoginState("?state=verifying&error=access_denied"), "cancel");
});

test("unattested URL states render as an unverified preview, never as server facts", () => {
  assert.equal(resolveLoginAttestation(""), "client");
  assert.equal(resolveLoginAttestation("?state=idle"), "client");
  assert.equal(resolveLoginAttestation("?state=verifying"), "client");
  assert.equal(resolveLoginAttestation("?state=signedout"), "client");
  assert.equal(resolveLoginAttestation("?state=unknown"), "client");
  assert.equal(resolveLoginAttestation("?error=not_a_member"), "client");
  assert.equal(resolveLoginAttestation("?state=verifying&error=access_denied"), "client");

  for (const locale of ["zh-CN", "en"]) {
    const dictionary = LOGIN_LOCALES[locale];
    const verifying = loginNotice("verifying", "client", dictionary);
    assert.equal(verifying.attested, false, locale);
    assert.match(verifying.title, locale === "zh-CN" ? /未验证/ : /unverified/, locale);
    assert.doesNotMatch(verifying.detail, /服务端校验组织成员身份|Checking organisation membership on the server/, locale);

    const signedout = loginNotice("signedout", "client", dictionary);
    assert.equal(signedout.attested, false, locale);
    assert.doesNotMatch(signedout.detail, /服务端会话已清除|server session was cleared/, locale);
    assert.match(signedout.detail, /尚未经服务端确认|not been confirmed by the server/, locale);

    // The renderer can still draw server copy, but only when an authenticated
    // source passes "server" explicitly. No URL resolver returns it.
    const denied = loginNotice("denied", "server", dictionary);
    assert.equal(denied.attested, true, locale);
    assert.equal(denied.detail, dictionary.n_denied, locale);
  }
  assert.equal(loginNotice("idle", "client", LOGIN_LOCALES.en), null);
  assert.equal(loginNotice("__proto__", "client", LOGIN_LOCALES.en).tone, "bad");
  assert.deepEqual(loginSteps("__proto__", "server"), ["fail", "na", "na"]);

  assert.deepEqual(loginSteps("verifying", "client"), ["unverified", "unverified", "unverified"]);
  assert.deepEqual(loginSteps("verifying", "server"), ["done", "run", "wait"]);
  assert.deepEqual(loginSteps("denied", "client"), ["unverified", "unverified", "unverified"]);
  assert.deepEqual(loginSteps("denied", "server"), ["done", "fail", "na"]);
  for (const locale of ["zh-CN", "en"]) {
    for (const key of ["preview_busy", "preview_verify", "preview_off"]) {
      assert.equal(typeof LOGIN_LOCALES[locale][key], "string", locale + "." + key);
    }
  }
});

test("writable error and state queries never attest a server outcome", () => {
  const adverse = "?state=verifying&error=not_a_member";
  assert.equal(resolveLoginState(adverse), "denied");
  assert.equal(resolveLoginAttestation(adverse), "client");
  for (const locale of ["zh-CN", "en"]) {
    const dictionary = LOGIN_LOCALES[locale];
    const notice = loginNotice(resolveLoginState(adverse), resolveLoginAttestation(adverse), dictionary);
    assert.equal(notice.attested, false, locale);
    assert.match(notice.title, locale === "zh-CN" ? /未验证/ : /unverified/, locale);
    assert.match(notice.detail, locale === "zh-CN" ? /尚未经服务端确认/ : /not been confirmed by the server/, locale);
    assert.doesNotMatch(
      notice.detail,
      locale === "zh-CN"
        ? /不在授权的组织中|服务端会话已清除|正在服务端校验/
        : /not in an authorised organisation|server session was cleared|Checking organisation membership on the server/,
      locale,
    );
    assert.deepEqual(
      loginSteps(resolveLoginState(adverse), resolveLoginAttestation(adverse)),
      ["unverified", "unverified", "unverified"],
      locale,
    );
  }
  // A plain error query is equally writable and must not be trusted either.
  assert.equal(resolveLoginAttestation("?error=not_a_member"), "client");
  assert.equal(resolveLoginAttestation("?error=access_denied"), "client");
  assert.equal(resolveLoginAttestation("?error=session_expired"), "client");
});

test("client-attested steps show an unverified marker, never not-started", () => {
  // F1 regression: before this fix every non-idle URL state returned all-"na"
  // and rendered "Not started/未开始" even after a real callback denial.
  for (const state of ["redirecting", "verifying", "denied", "cancel", "error", "expired", "signedout", "unavailable"]) {
    const steps = loginSteps(state, "client");
    assert.deepEqual(steps, ["unverified", "unverified", "unverified"], state);
    assert.equal(steps.includes("na"), false, state);
  }
  // Only an attested state may render a genuine "na" (not started) step.
  assert.deepEqual(loginSteps("denied", "server"), ["done", "fail", "na"]);
  assert.deepEqual(loginSteps("expired", "server"), ["na", "na", "na"]);
  for (const locale of ["zh-CN", "en"]) {
    assert.equal(LOGIN_LOCALES[locale].s_unverified, locale === "zh-CN" ? "未核实" : "Unverified");
    assert.equal(LOGIN_LOCALES[locale].s_na, locale === "zh-CN" ? "未开始" : "Not started");
  }
  const page = loginHTML();
  assert.match(page, /unverified:t\.s_unverified/);
  assert.match(page, /var key=steps\[index\]\|\|"na"/);
});

test("worker callback errors use production copy while design states stay previews", () => {
  // F2 regression: a real Worker `?error=` outcome must not say "Preview" and
  // must carry the no-session wording; a design-only `?state=` keeps preview.
  const memberDenied = "?error=" + encodeURIComponent("仅限 ORG 组织成员访问");
  const missingCode = "?error=" + encodeURIComponent("缺少授权码");
  assert.equal(resolveLoginState(memberDenied), "denied");
  assert.equal(resolveLoginOrigin(memberDenied), "callback");
  assert.equal(resolveLoginState(missingCode), "error");
  assert.equal(resolveLoginOrigin(missingCode), "callback");
  assert.equal(resolveLoginOrigin("?state=denied"), "design");
  assert.equal(resolveLoginOrigin(""), "none");

  for (const locale of ["zh-CN", "en"]) {
    const dictionary = LOGIN_LOCALES[locale];
    const callback = loginNotice("denied", "client", dictionary, resolveLoginOrigin(memberDenied));
    assert.equal(callback.attested, false, locale);
    assert.equal(callback.title, dictionary.n_denied_t, locale);
    assert.equal(callback.detail, dictionary.callback_detail, locale);
    assert.doesNotMatch(callback.title, /Preview|预览|unverified|未验证/i, locale);
    assert.match(callback.detail, locale === "zh-CN" ? /未创建会话/ : /No session was created/, locale);
    assert.match(callback.detail, locale === "zh-CN" ? /登录回调/ : /sign-in callback/, locale);

    const preview = loginNotice("denied", "client", dictionary, "design");
    assert.equal(preview.attested, false, locale);
    assert.match(preview.title, locale === "zh-CN" ? /预览（未验证）/ : /Preview \(unverified\)/, locale);
    assert.equal(preview.detail, dictionary.preview_detail, locale);
  }

  // A future authenticated source keeps the approved per-state copy.
  assert.equal(
    loginNotice("denied", "server", LOGIN_LOCALES.en, "callback").detail,
    LOGIN_LOCALES.en.n_denied,
  );
});

test("login renderer uses notice provenance instead of reading server copy directly", () => {
  const page = loginHTML();
  assert.match(page, /var attestation=resolveLoginAttestation\(location\.search\)/);
  assert.match(page, /var origin=resolveLoginOrigin\(location\.search\)/);
  assert.match(page, /function resolveLoginAttestation/);
  assert.match(page, /function resolveLoginOrigin/);
  assert.match(page, /function loginNotice/);
  assert.match(page, /function loginSteps/);
  assert.match(page, /loginNotice\(state,attestation,t,origin\)/);
  assert.match(page, /loginSteps\(state,attestation\)/);
  assert.match(page, /unverified:t\.s_unverified/);
  assert.match(page, /t\["preview_"\+spec\.btn\]/);
  assert.match(page, /attestation==="server"&&\(spec\.btn==="busy"\|\|spec\.btn==="verify"\)/);
  assert.match(page, /attestation!=="server"&&\(spec\.btn==="busy"\|\|spec\.btn==="verify"\)/);
  assert.doesNotMatch(page, /var key=spec\.steps\[index\]/);
  assert.doesNotMatch(page, /setText\(document\.getElementById\("note-d"\),t\["n_"\+state\]\)/);
  assert.doesNotMatch(page, /setText\(document\.getElementById\("note-t"\),t\["n_"\+state\+"_t"\]\)/);
});

test("login references accept only a validated opaque correlation token", () => {
  assert.equal(resolveLoginRef(""), "");
  assert.equal(resolveLoginRef("?ref=AUTH-DENIED · 7F3A-DEMO"), "••••DEMO");
  assert.equal(resolveLoginRef("?ref=AUTH-CONFIG · 0000-DEMO"), "••••DEMO");
  assert.equal(resolveLoginRef("?ref=octocat"), "");
  assert.equal(resolveLoginRef("?ref=user@example.test"), "");
  assert.equal(resolveLoginRef("?ref=<script>alert(1)</script>"), "");
  assert.equal(resolveLoginRef("?ref=" + "A".repeat(200)), "");
  assert.equal(resolveLoginRef("?ref=auth-denied · 7f3a-demo"), "");
});

test("shape-valid identity and secret-label references are masked before render", () => {
  for (const raw of [
    "ACCT-0001 · 7F3A-9C2D",
    "GITHUB-TOKEN · 7F3A-9C2D",
    "OPERATOR · 7F3A-9C2D",
  ]) {
    const masked = resolveLoginRef("?ref=" + encodeURIComponent(raw));
    assert.match(masked, /^••••/, raw);
    assert.notEqual(masked, raw, raw);
    assert.doesNotMatch(masked, /ACCT-0001|GITHUB-TOKEN|OPERATOR|7F3A/, raw);
  }
  // The shared helper is the only masking path used by the login renderer.
  assert.equal(maskIdentifier("ACCT-0001 · 7F3A-9C2D"), "••••9C2D");
  assert.equal(maskIdentifier(""), "");
});

test("login locales keep zh-CN and en key, text and placeholder parity", () => {
  const zhKeys = Object.keys(LOGIN_LOCALES["zh-CN"]).sort();
  const enKeys = Object.keys(LOGIN_LOCALES.en).sort();
  assert.deepEqual(enKeys, zhKeys);
  for (const [locale, dictionary] of Object.entries(LOGIN_LOCALES)) {
    for (const [key, value] of Object.entries(dictionary)) {
      assert.equal(typeof value, "string", locale + "." + key);
      assert.doesNotMatch(value, /[<>]/, locale + "." + key);
      assert.doesNotMatch(value, /innerHTML|@/, locale + "." + key);
    }
  }
  for (const key of zhKeys) {
    const zhPlaceholders = [...LOGIN_LOCALES["zh-CN"][key].matchAll(/\{([^}]+)\}/g)].map((match) => match[1]);
    const enPlaceholders = [...LOGIN_LOCALES.en[key].matchAll(/\{([^}]+)\}/g)].map((match) => match[1]);
    assert.deepEqual(enPlaceholders, zhPlaceholders, key);
  }
  for (const state of Object.keys(LOGIN_STATES)) {
    if (state === "idle") continue;
    assert.equal(typeof LOGIN_LOCALES["zh-CN"]["n_" + state], "string", state);
    assert.equal(typeof LOGIN_LOCALES["zh-CN"]["n_" + state + "_t"], "string", state);
    assert.equal(typeof LOGIN_LOCALES.en["n_" + state], "string", state);
    assert.equal(typeof LOGIN_LOCALES.en["n_" + state + "_t"], "string", state);
  }
});

test("failure notices state that no session was created in both languages", () => {
  for (const locale of ["zh-CN", "en"]) {
    for (const state of ["denied", "cancel", "error"]) {
      const copy = LOGIN_LOCALES[locale]["n_" + state];
      assert.match(copy, locale === "zh-CN" ? /未创建会话/ : /No session was created/, locale + "." + state);
    }
  }
});

test("login renders references only from the validated ref parameter", () => {
  const page = loginHTML();
  assert.match(page, /var ref=resolveLoginRef\(location\.search\)/);
  assert.match(page, /id="note-r"/);
  assert.match(page, /if\(ref\)\{setText\(document\.getElementById\("note-r"\),t\.ref\+" "\+ref\);\}/);
  assert.doesNotMatch(page, /DEMO|AUTH-DENIED|AUTH-CALLBACK|AUTH-CONFIG/);
});

test("step copy renders placeholders as code elements, never as markup", () => {
  assert.deepEqual(stepSegments("no placeholders"), [{ type: "text", value: "no placeholders" }]);
  assert.deepEqual(stepSegments("only {read:user}"), [
    { type: "text", value: "only " },
    { type: "code", value: "read:user" },
  ]);
  assert.deepEqual(stepSegments("{a} and {b}"), [
    { type: "code", value: "a" },
    { type: "text", value: " and " },
    { type: "code", value: "b" },
  ]);
  assert.deepEqual(stepSegments(""), []);
  const page = loginHTML();
  const second = /<p data-i18n-rich="f2p">([\s\S]*?)<\/p>/.exec(page);
  assert.ok(second);
  assert.match(second[1], /只请求 <code>read:org<\/code>/);
  assert.doesNotMatch(second[1], /\{/);
  const third = /<p data-i18n-rich="f3p">([\s\S]*?)<\/p>/.exec(page);
  assert.ok(third);
  assert.match(third[1], /仅授予 <code>dashboard:read<\/code>/);
  assert.doesNotMatch(third[1], /\{/);
});

test("login redesign leaves the dashboard output untouched", () => {
  const page = dashboardHTML();
  assert.match(page, /var ROUTES=\{status:"\/upstream-status"\}/);
  // Retired routes may still be named in the "not shown" copy; the client must
  // not fetch them.
  assert.doesNotMatch(page.split("var ROUTES=")[1].split(";")[0], /v1\/ai/);
  assert.match(page, /qg-lang/);
  assert.match(page, /qg-theme/);
  assert.doesNotMatch(page, /innerHTML\s*=/);
  // The shared dictionary deliberately includes legacy login copy. Check
  // visible dashboard markup, rather than rejecting its embedded dictionary.
  assert.doesNotMatch(page.split("var I18N=")[0], /每一次调用/);
  assert.doesNotMatch(page, /class="gh" id="gh"/);
  assert.doesNotMatch(page, /AUTH-DENIED/);
});

test("OAuth error redirects are classified without reflecting raw text", () => {
  assert.equal(classifyLoginError(""), "none");
  assert.equal(classifyLoginError("not_a_member"), "denied");
  assert.equal(classifyLoginError("Not a member of the organisation"), "denied");
  assert.equal(classifyLoginError("access_denied"), "cancel");
  assert.equal(classifyLoginError("session_expired"), "expired");
  assert.equal(classifyLoginError("<script>alert(1)</script>"), "error");
});

test("quota rows never turn a missing budget into unlimited or zero usage", () => {
  assert.deepEqual(quotaRowModel({}), { status: "not_configured", percent: null, used: null, budget: null });
  assert.equal(quotaRowModel({ daily_budget: 0, total_cost_usd: 3 }).status, "not_configured");
  assert.deepEqual(quotaRowModel({ daily_budget: 10 }), { status: "unknown", percent: null, used: null, budget: 10 });
  const observed = quotaRowModel({ daily_budget: 10, total_cost_usd: 0 });
  assert.equal(observed.status, "observed");
  assert.equal(observed.percent, 0);
  assert.equal(quotaRowModel({ daily_budget: 10, total_cost_usd: 2.5 }).percent, 25);
});

test("health readouts keep unknown values unknown and exclude background latency", () => {
  assert.equal(healthReadouts({}), null);
  assert.equal(healthReadouts({ endpoints: [] }), null);
  assert.equal(healthReadouts({ endpoints: [null] }), null);
  assert.equal(healthReadouts({ endpoints: [{ total: 100, errors: 0, p95_ms: 9999, latency_profile: "background_job" }] }), null);
  assert.equal(healthReadouts({ endpoints: [{ total: 4, errors: null }] }), null);
  const readouts = healthReadouts({
    status: "healthy",
    endpoints: [
      { total: 3, errors: 1, p95_ms: 120 },
      { total: 5, errors: 0, p95_ms: 90 },
      { total: 100, errors: 0, p95_ms: 9999, latency_profile: "background_job" },
    ],
  });
  assert.equal(readouts.requests, 8);
  assert.equal(readouts.errors, 1);
  assert.equal(readouts.errorRate, 12.5);
  assert.equal(readouts.p95, 120);
  const idle = healthReadouts({ status: "healthy", endpoints: [{ total: 0, errors: 0, p95_ms: 0 }] });
  assert.equal(idle.errorRate, null);
  assert.equal(idle.p95, 0);
});

test("token readout prefers combined usage and never fabricates numbers", () => {
  assert.equal(tokensFromQuota(null), null);
  assert.equal(tokensFromQuota({ summary: {} }), null);
  assert.deepEqual(tokensFromQuota({ summary: { combined: { tokens_input: 10, tokens_output: 5 } } }), {
    input: 10,
    output: 5,
    total: 15,
  });
  assert.deepEqual(
    tokensFromQuota({
      summary: { api_key: { tokens_input: 2, tokens_output: 3 }, codex: { tokens_input: 4, tokens_output: 1 } },
    }),
    { input: 6, output: 4, total: 10 },
  );
  assert.equal(tokensFromQuota({ summary: { api_key: { tokens_input: null, tokens_output: null } } }), null);
});

test("dynamic values are escaped for HTML and script contexts", () => {
  assert.equal(escapeHtml(`<img src=x onerror="x">&'`), "&lt;img src=x onerror=&quot;x&quot;&gt;&amp;&#39;");
  const literal = jsObjectLiteral({ value: "</script><script>alert(1)</script>&" });
  assert.doesNotMatch(literal, /<\/script>/i);
  assert.doesNotMatch(literal, /<script/i);
  assert.equal(JSON.parse(literal).value, "</script><script>alert(1)</script>&");
  assert.equal(safeHttpUrl("https://example.test/a?b=1"), "https://example.test/a?b=1");
  assert.equal(safeHttpUrl("javascript:alert(1)"), "");
  assert.equal(safeHttpUrl("http://example.test/a"), "");
  assert.equal(safeHttpUrl("not a url"), "");
});

test("dashboard keeps honest unknown states and one read adapter", () => {
  const page = dashboardHTML();
  assert.match(page, /var ROUTES=\{/);
  assert.match(page, /status:"\/upstream-status"/);
  assert.doesNotMatch(page, /"\/v2\//);
  assert.doesNotMatch(page, /MOCK/);
  // Unknown is rendered as unknown, never as zero or a fabricated value.
  assert.match(page, /t\.s_state_unknown/);
  assert.match(page, /panel-error/);
  assert.match(page, /t\.v_unknown_status/);
  assert.doesNotMatch(page, /innerHTML\s*=/);
  assert.match(page, /textContent/);
  assert.match(page, /qg-lang/);
  assert.match(page, /qg-theme/);
  assert.match(page, /prefers-color-scheme/);
});

test("a failed probe read replaces the table with the machine code", () => {
  const { client } = toastClient({ lang: "en", extraIds: ["probe-table"] });
  client.state.status = { status: "error", error: "status: 502", code: "origin_not_configured", httpStatus: 502, sessionExpired: false };
  client.renderProbes();
  const text = client.document.getElementById("probe-table").textContent;
  assert.match(text, /Probe did not complete/);
  assert.match(text, /origin_not_configured/);
  assert.doesNotMatch(text, /ms/, "a failed probe shows no latency");
});

test("the probe table renders what the origin answered, and nothing else", () => {
  const { client } = toastClient({ lang: "en", extraIds: ["probe-table", "read-model"] });
  client.state.status = { status: "ok", data: structuredClone(STATUS_FIXTURE) };
  client.renderProbes();
  client.renderReadModel();
  const table = client.document.getElementById("probe-table");
  const rows = [];
  (function walk(node) {
    if (node.tagName === "tr") rows.push(node);
    for (const child of node.children || []) walk(child);
  })(table);
  assert.equal(rows.length, 3, "one header row plus one row per probe");
  const text = table.textContent;
  assert.match(text, /\/v1\/tasks/);
  assert.match(text, /401/);
  assert.match(text, /404/);
  // The read model is stated as an observed reason, not rendered as a count,
  // and never infers which credential the service would have accepted.
  const note = client.document.getElementById("read-model").textContent;
  assert.match(note, /rejected/);
  assert.match(note, /no task counts/);
  assert.doesNotMatch(note, /OAuth/i);
  assert.doesNotMatch(note, /task counts?\s*:\s*\d+/);
});

test("embedded client scripts parse without export leakage", () => {
  function lastScript(page) {
    const matches = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)];
    return matches.length ? matches[matches.length - 1][1] : "";
  }
  for (const page of [loginHTML(), dashboardHTML()]) {
    const source = lastScript(page);
    assert.ok(source.length > 100);
    assert.doesNotMatch(source, /^export\s/m);
    assert.doesNotThrow(() => new Function(source));
  }
});

test("dashboard proxy allowlist still rejects V2 and write routes", () => {
  assert.throws(() => buildDashboardApiUrl("https://origin.example", "/v2/ai/health", ""), /not allowed/);
  assert.throws(() => buildDashboardApiUrl("https://origin.example", "/v1/ai/execute/jobs", ""), /not allowed/);
});

test("endpoint health never infers success from historical request metrics", () => {
  // The retired health read is gone with its panels. What replaced it is a
  // probe, so the equivalent guarantee is that a verdict is derived from the
  // status code alone.
  for (const [status, verdict] of [
    [200, "reachable"], [204, "reachable"], [401, "auth_required"], [403, "auth_required"],
    [404, "not_found"], [429, "rate_limited"], [302, "redirect"], [500, "server_error"],
    [503, "server_error"], [418, "unexpected_status"], [null, "unreachable"],
  ]) {
    assert.equal(classifyProbe(status === null ? NaN : status), verdict, String(status));
  }
});

test("header drops the unobserved egress chip and keeps the health-driven status", () => {
  const page = dashboardHTML();
  // The static egress indicator carried no observation; it is not rendered and
  // no replacement probe is added.
  assert.equal(/d_chip|出口 · SG|egress · SG/.test(page), false);
  for (const dict of [LOCALES.en, LOCALES["zh-CN"]]) assert.equal(dict.d_chip, undefined);
  // The status lamp and timer that the health API drives stay in place.
  assert.match(page, /<span class="lamp st-unk" id="pulse" aria-hidden="true"><\/span><span id="status-text"/);
  assert.match(page, /<span id="last-refresh">/);
  assert.match(page, /data-i18n="updated"/);
});

test("account block and read-only footer note are gone; sign-out stays in the header", () => {
  const page = dashboardHTML();
  assert.equal(/account-panel|account-h/.test(page), false, "no account panel or dead anchor");
  assert.equal(/footer_readonly/.test(page), false, "footer carries the refresh note only");
  for (const key of ["acct_panel_title", "acct_scope_title", "acct_scope_body", "acct_signed_in", "footer_readonly", "nav_account"]) {
    assert.equal(LOCALES.en[key], undefined, key);
    assert.equal(LOCALES["zh-CN"][key], undefined, key);
  }
  const header = page.slice(page.indexOf("<header"), page.indexOf("</header>"));
  assert.equal((header.match(/href="\/logout"/g) || []).length, 1, "sign-out sits in the header account entry");
  assert.match(header, /class="user"/);
  assert.equal((page.match(/href="\/logout"/g) || []).length, 1, "sign-out exists once, in the header");
});

test("login never publishes invented diagnostic references", () => {
  assert.equal(/DEMO|AUTH-DENIED|AUTH-CALLBACK|AUTH-CONFIG/.test(loginHTML()), false);
  for (const spec of Object.values(LOGIN_STATES)) assert.equal(spec.ref, undefined);
});

test("login presentation states never grant a session and unavailable fails closed", () => {
  const page = loginHTML();
  // The page has one navigation target and no credential or session write.
  assert.equal((page.match(/href="\/login"/g) || []).length, 1);
  assert.doesNotMatch(page, /dash_session|sessionStorage|document\.cookie/i);
  // Unavailable removes the only action from the tab order and from navigation.
  assert.match(page, /spec\.btn==="off"/);
  assert.match(page, /gh\.removeAttribute\("href"\)/);
  assert.match(page, /gh\.tabIndex=-1/);
  assert.match(page, /aria-disabled/);
  // Busy states are non-navigating; a click is suppressed.
  assert.match(page, /spec\.btn==="busy"\|\|spec\.btn==="verify"/);
  assert.match(page, /event\.preventDefault\(\)/);
  // No state definition claims a successful session.
  for (const spec of Object.values(LOGIN_STATES)) {
    assert.ok(["go", "retry", "busy", "verify", "off"].includes(spec.btn));
    assert.notEqual(spec.btn, "done");
  }
});

test("scope copy matches the existing minimal OAuth request in both dictionaries", async () => {
  const response = await worker.fetch(new Request("https://dash.example/login"), { GITHUB_OAUTH_CLIENT_ID: "synthetic-client" });
  const scope = new URL(response.headers.get("Location")).searchParams.get("scope");
  assert.equal(scope, "read:org");
  for (const locale of ["zh-CN", "en"]) {
    for (const copy of [LOGIN_LOCALES[locale].f2p, LOCALES[locale].login_f2p]) {
      assert.equal(copy.includes(scope), true);
      assert.equal(/read:user|团队|team/.test(copy), false);
    }
  }
});

test("login small text and brand mark meet AA contrast in both themes", async () => {
  const { LOGIN_CSS } = await import("../src/ui/login-styles.mjs");
  const luminance = (hex) => hex.match(/[0-9a-f]{2}/gi).map(c => {
    const v = parseInt(c, 16) / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
  for (const theme of ["light", "dark"]) {
    const block = LOGIN_CSS.match(new RegExp("\\[data-theme=" + theme + "\\]\\{([^}]+)"))[1];
    const tokens = Object.fromEntries([...block.matchAll(/(--[a-z0-9]+):(#\w{6})/gi)].map(m => [m[1], m[2]]));
    const warningBackground = "#" + [1, 3, 5].map(i => Math.round(
      parseInt(tokens["--warn"].slice(i, i + 2), 16) * 0.09 +
      parseInt(tokens["--panel"].slice(i, i + 2), 16) * 0.91
    ).toString(16).padStart(2, "0")).join("");
    for (const [fg, bg] of [[tokens["--warn"], warningBackground], [tokens["--ink3"], tokens["--bg"]], [tokens["--ink3"], tokens["--panel"]], [tokens["--sig"], tokens["--panel"]], [theme === "light" ? "#ffffff" : "#160a02", tokens["--sig"]]]) {
      const a = luminance(fg), b = luminance(bg);
      assert.ok((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05) >= 4.5, theme + " small text contrast");
    }
  }
});

test("decorative login art never intercepts clicks and tools stay above it", async () => {
  const { LOGIN_CSS } = await import("../src/ui/login-styles.mjs");
  const art = LOGIN_CSS.match(/\.l-art svg\.routes\{([^}]+)\}/)[1];
  assert.ok(art.includes("pointer-events:none"), "decorative routes svg must ignore pointer events");
  const tools = LOGIN_CSS.match(/\.tools\{([^}]+)\}/)[1];
  assert.ok(/z-index:\s*[1-9]/.test(tools), "theme/language tools must paint above the art");
});

// --- Global error toast (F1/F2) and minor consistency (F3) -----------------
// Offline fake DOM only: no browser, network, session or production data.

class FakeNode {
  constructor(doc, tag, id) {
    this.ownerDocument = doc; this.tagName = tag; this.id = id || "";
    this.children = []; this.parentNode = null; this.hidden = false;
    this._text = ""; this.attrs = {}; this.listeners = {}; this.className = ""; this.style = {};
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; return child; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return Object.hasOwn(this.attrs, key) ? this.attrs[key] : null; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, init = {}) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...init };
    for (let node = this; node; node = node.parentNode) for (const fn of node.listeners[type] || []) fn.call(node, event);
    return event;
  }
  contains(node) { for (let cur = node; cur; cur = cur.parentNode) if (cur === this) return true; return false; }
  focus() { this.ownerDocument.activeElement = this; }
}

function toastClient({ lang = "en", fetchImpl, extraIds = [] } = {}) {
  const script = [...dashboardHTML().matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
  const source = script.slice(0, script.lastIndexOf('document.querySelectorAll(".js-lang")'));
  const ids = {};
  const document = {
    activeElement: null,
    documentElement: { lang, getAttribute: () => "light", setAttribute() {} },
    getElementById: (id) => ids[id] || null,
    createElement: (tag) => new FakeNode(document, tag),
    createTextNode: (value) => ({ textContent: String(value) }),
    querySelectorAll: () => [],
  };
  const make = (id, parent) => { const node = new FakeNode(document, "div", id); ids[id] = node; if (parent) parent.appendChild(node); return node; };
  const body = make("body");
  const title = make("main-title", body);
  const toast = make("toast", body);
  make("toast-text", toast);
  make("toast-dismiss", toast);
  const live = make("toast-live", body);
  for (const id of ["v-p95", "f-p95", "v-req", "f-req", "v-err", "f-err"]) make(id, body);
  for (const id of extraIds) make(id, body);
  document.activeElement = body;
  const context = vm.createContext({
    document, URL, Promise,
    fetch: fetchImpl || (() => Promise.reject(new Error("offline fixture: unavailable"))),
    localStorage: { getItem: () => null, setItem() {} },
  });
  vm.runInContext(source, context);
  if (typeof context.initToast === "function") context.initToast();
  return { client: context, document, ids, toast, live, title, body };
}

// One read source: the page has a single probe endpoint, so a failure episode
// has exactly one key to track.
const TOAST_KEYS = ["status"];
const STATUS_FIXTURE = {
  status: "ok",
  checked_at: "2026-11-09T15:00:00.000Z",
  overall: "online",
  probes: [
    { id: "tasks", path: "/v1/tasks", auth: "none", resource: "task_api", verdict: "auth_required", http_status: 401, latency_ms: 120 },
    { id: "retired_health", path: "/v1/ai/health", auth: "dashboard", resource: "retired_v1_health", verdict: "not_found", http_status: 404, latency_ms: 90 },
  ],
  read_model: { task_counts: false, reason_code: "read_credentials_rejected" },
};
function setFailures(client, failing) {
  client.state.status = failing.length
    ? { status: "error", error: "offline fixture", code: "network", httpStatus: 0, sessionExpired: false }
    : { status: "ok", data: structuredClone(STATUS_FIXTURE) };
}

test("F1: global toast sits in the content column after the header, not over it", () => {
  const page = dashboardHTML();
  const header = page.indexOf("</header>");
  const toastAt = page.indexOf('id="toast"');
  assert.ok(page.indexOf("<main") < header && header < toastAt && toastAt < page.indexOf('id="signal"'),
    "toast must follow the header controls inside main (DOM and tab order)");
  assert.doesNotMatch(page, /\.toast\{position:fixed;top:16px;right:16px/);
  assert.match(page, /<button[^>]*id="toast-dismiss"[^>]*data-i18n-aria="toast_dismiss"/);
  assert.match(page, /id="toast-live" role="status" aria-live="polite"><\/div>/, "live region exists empty before any message");
  assert.doesNotMatch(page, /role="alert"/);
  assert.match(page, /id="main-title" tabindex="-1"/);
});

test("F2: toast and dismiss strings exist and are localized in both dictionaries", () => {
  for (const key of ["toast_error", "toast_dismiss"]) {
    assert.equal(typeof LOCALES.en[key], "string");
    assert.equal(typeof LOCALES["zh-CN"][key], "string");
    assert.ok(LOCALES.en[key].trim() && LOCALES["zh-CN"][key].trim(), key);
    assert.notEqual(LOCALES["zh-CN"][key], LOCALES.en[key], key);
  }
});

test("F1: error episodes show once, survive refreshes and respect dismissal", () => {
  const { client } = toastClient();
  const step = (prev, failed) => client.toastEpisode(prev, "cycle", failed);
  // R20: no failures, no toast and no announcement.
  let s = step(null, []);
  assert.equal(s.visible, false); assert.equal(s.announce, false);
  // R01/R09: one episode, announced once over repeated identical cycles.
  s = step(null, ["health", "quota"]);
  assert.equal(s.visible, true); assert.equal(s.announce, true);
  for (let i = 0; i < 3; i++) { s = step(s, ["quota", "health"]); assert.equal(s.visible, true); assert.equal(s.announce, false); }
  // R10: dismissed stays hidden across identical cycles.
  s = client.toastEpisode(s, "dismiss");
  assert.equal(s.visible, false);
  for (let i = 0; i < 3; i++) { s = step(s, ["health", "quota"]); assert.equal(s.visible, false); assert.equal(s.announce, false); }
  // R13: partial recovery never re-shows.
  s = step(s, ["health"]);
  assert.equal(s.visible, false); assert.equal(s.announce, false);
  // R11: a newly failing source (not failing at dismissal) re-shows once.
  s = step(s, ["health", "org"]);
  assert.equal(s.visible, true); assert.equal(s.announce, true);
  s = step(s, ["health", "org"]);
  assert.equal(s.announce, false);
  // R12: recovery removes silently; the next episode shows once.
  s = client.toastEpisode(s, "dismiss");
  s = step(s, []);
  assert.equal(s.visible, false); assert.equal(s.announce, false);
  s = step(s, ["health"]);
  assert.equal(s.visible, true); assert.equal(s.announce, true);
  // R21: a fresh page (no in-memory state) shows the toast again.
  assert.equal(step(null, ["health"]).visible, true);
  assert.doesNotMatch(dashboardHTML().split("var CLIENT")[0], /toast.*localStorage/);
});

test("F1/F2: toast renders from a key, re-localizes in place and dismisses accessibly", async () => {
  const { client, document, ids, toast, live, title } = toastClient({ lang: "en" });
  await client.loadAll();
  assert.equal(toast.hidden, false);
  assert.equal(ids["toast-text"].textContent, LOCALES.en.toast_error);
  assert.equal(live.textContent, LOCALES.en.toast_error);
  assert.equal(document.activeElement, ids.body, "showing the toast does not move focus");
  // R16: same instance re-renders in zh-CN; one announcement at most.
  client.lang = "zh-CN"; client.applyI18n();
  assert.equal(ids["toast-text"].textContent, LOCALES["zh-CN"].toast_error);
  assert.doesNotMatch(ids["toast-text"].textContent, /Some data/);
  // R09: further refreshes with the same failures do not re-announce.
  live.textContent = "";
  await client.loadAll();
  assert.equal(live.textContent, "");
  assert.equal(toast.hidden, false);
  // R07: Escape outside the toast does nothing.
  ids.body.dispatch("keydown", { key: "Escape" });
  assert.equal(toast.hidden, false);
  // R06/R05: Escape inside dismisses and moves focus to the main heading.
  ids["toast-dismiss"].focus();
  ids["toast-dismiss"].dispatch("keydown", { key: "Escape" });
  assert.equal(toast.hidden, true);
  assert.equal(document.activeElement, title);
  // R17: locale switch after dismissal does not bring it back.
  client.lang = "en"; client.applyI18n();
  assert.equal(toast.hidden, true);
  await client.loadAll();
  assert.equal(toast.hidden, true);
});

test("F1: dismiss button click hides the toast and keeps panel errors", async () => {
  const { client, document, ids, toast } = toastClient({ lang: "zh-CN" });
  await client.loadAll();
  assert.equal(toast.hidden, false);
  const before = TOAST_KEYS.map((key) => client.state[key].status);
  document.activeElement = ids.body;
  ids["toast-dismiss"].dispatch("click");
  assert.equal(toast.hidden, true);
  assert.equal(document.activeElement, ids.body, "pointer dismissal from outside leaves focus unchanged");
  assert.deepEqual(TOAST_KEYS.map((key) => client.state[key].status), before);
  setFailures(client, ["health"]);
});

test("F3a/F3b: the removed panels are named, with the retired route, in both languages", () => {
  const page = dashboardHTML();
  for (const key of ["o_traffic_body", "o_quota_body", "o_org_body", "o_changes_body", "o_shadow_body", "o_diag_body"]) {
    assert.ok(LOCALES["zh-CN"][key] && LOCALES.en[key], key);
  }
  // Each omitted panel names the route that no longer exists.
  for (const route of ["/v1/ai/health", "/v1/ai/quota", "/v1/ai/org-health", "/v1/ai/changes", "/v1/ai/feedback/shadow", "/v1/ai/automation/runs"]) {
    assert.ok(page.includes(route), route);
  }
  assert.match(page, /611c129/);
});

// --- Navigation highlight and own-session expiry (t1505) --------------------
// Offline fake DOM only: no browser, network, session or production data.

test("a failed probe names the Worker code in both languages, never a value", () => {
  for (const lang of ["en", "zh-CN"]) {
    const { client } = toastClient({ lang, extraIds: ["probe-table"] });
    client.state.status = { status: "error", error: "status: 502", code: "origin_not_configured", httpStatus: 502, sessionExpired: false };
    client.renderProbes();
    const text = client.document.getElementById("probe-table").textContent;
    assert.match(text, new RegExp(LOCALES[lang].s_failed.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.match(text, /origin_not_configured/);
    assert.doesNotMatch(text, /—/, "no placeholder cell is rendered for a failed read");
  }
});

test("an expired session keeps the re-login notice and adds no cause line", () => {
  const { client } = toastClient({ lang: "en", extraIds: ["probe-table"] });
  client.state.status = { status: "error", error: "unauthorized", code: "", httpStatus: 401, sessionExpired: true };
  client.renderProbes();
  const text = client.document.getElementById("probe-table").textContent;
  assert.match(text, /No probe was sent/);
  assert.doesNotMatch(text, /http_401/);
});

// t1599: the dashboard's own expired session must not read as an upstream
// failure. The panel and status pill name the session; upstream failures keep
// the probe-failure copy and their machine code.
test("t1599: own-session 401 panel and status never blame the upstream, in both languages", () => {
  for (const lang of ["en", "zh-CN"]) {
    const L = LOCALES[lang];
    const { client } = toastClient({ lang, extraIds: ["probe-table", "pulse", "status-text"] });
    client.state.status = { status: "error", error: "unauthorized", code: "", httpStatus: 401, sessionExpired: true };
    client.renderProbes();
    client.renderStatus();
    const panel = client.document.getElementById("probe-table").textContent;
    assert.ok(panel.includes(L.s_session_title), lang + ": session title shown");
    assert.ok(panel.includes(L.s_session_hint), lang + ": session hint shown");
    assert.ok(!panel.includes(L.s_failed), lang + ": no probe-failure title");
    assert.ok(!panel.includes(L.s_failed_hint), lang + ": no upstream-failure hint");
    assert.ok(!panel.includes(L.s_error_code), lang + ": no cause line");
    assert.equal(client.document.getElementById("status-text").textContent, L.s_state_session);
    assert.match(client.document.getElementById("pulse").className, /st-warn/);
  }
});

test("t1599: an upstream failure keeps the probe-failure copy, not the session copy", () => {
  for (const lang of ["en", "zh-CN"]) {
    const L = LOCALES[lang];
    const { client } = toastClient({ lang, extraIds: ["probe-table", "pulse", "status-text"] });
    client.state.status = { status: "error", error: "status: 502", code: "origin_not_configured", httpStatus: 502, sessionExpired: false };
    client.renderProbes();
    client.renderStatus();
    const panel = client.document.getElementById("probe-table").textContent;
    assert.ok(panel.includes(L.s_failed));
    assert.ok(panel.includes("origin_not_configured"));
    assert.ok(!panel.includes(L.s_session_title));
    assert.equal(client.document.getElementById("status-text").textContent, L.s_state_error);
    assert.match(client.document.getElementById("pulse").className, /st-bad/);
  }
});

test("t1599: a 401 read through loadAll renders the session panel, an origin 401 verdict does not", async () => {
  const { client } = toastClient({ lang: "en", extraIds: ["probe-table", "pulse", "status-text"], fetchImpl: SESSION_401 });
  await client.loadAll();
  assert.match(client.document.getElementById("probe-table").textContent, /No probe was sent/);
  const ok = toastClient({ lang: "en", extraIds: ["probe-table", "pulse", "status-text"], fetchImpl: () => Promise.resolve(jsonResponse(200, STATUS_FIXTURE)) });
  await ok.client.loadAll();
  const text = ok.client.document.getElementById("probe-table").textContent;
  assert.match(text, /401/, "the origin's own 401 stays a probe row");
  assert.doesNotMatch(text, /No probe was sent/);
});

function jsonResponse(status, body) {
  const raw = body === undefined ? "" : JSON.stringify(body);
  return { ok: status >= 200 && status < 300, status, text: () => Promise.resolve(raw) };
}
const SESSION_401 = () => Promise.resolve(jsonResponse(401, { status: "error", error: "unauthorized" }));

test("401: own expired session shows one bilingual re-login notice with the login entry", async () => {
  for (const lang of ["en", "zh-CN"]) {
    let calls = 0;
    const { client, ids, toast, live } = toastClient({ lang, fetchImpl: () => { calls += 1; return SESSION_401(); } });
    client.applyI18n();
    live.textContent = "";
    await client.loadAll();
    assert.ok(calls >= 1, "the status read ran and returned 401");
    assert.equal(toast.hidden, false);
    const text = ids["toast-text"];
    assert.equal(text.textContent, LOCALES[lang].toast_session);
    assert.equal(text.children.length, 1, "concurrent 401s collapse into one notice");
    const link = text.children[0];
    assert.equal(link.tagName, "a");
    assert.equal(link.href, "/login", "reuses the existing OAuth login entry");
    assert.equal(live.textContent, LOCALES[lang].toast_session);
    // A later refresh that still returns 401 does not announce again.
    live.textContent = "";
    await client.loadAll();
    assert.equal(live.textContent, "");
    assert.equal(text.children.length, 1);
  }
  assert.equal(LOCALES["zh-CN"].toast_session, "登录已失效，请重新登录");
  assert.equal(LOCALES.en.toast_session, "Login expired, please log in again");
});

test("401: re-login notice re-localizes in place and dismisses like the existing toast", async () => {
  const { client, ids, toast } = toastClient({ lang: "en", fetchImpl: SESSION_401 });
  await client.loadAll();
  client.lang = "zh-CN"; client.applyI18n();
  assert.equal(ids["toast-text"].textContent, LOCALES["zh-CN"].toast_session);
  ids["toast-dismiss"].dispatch("click");
  assert.equal(toast.hidden, true);
  await client.loadAll();
  assert.equal(toast.hidden, true, "dismissed notice stays hidden while the session stays expired");
});

test("401: generic notice becomes a re-login notice and recovery clears it", async () => {
  let mode = "network";
  const fetchImpl = () => mode === "network" ? Promise.reject(new Error("offline fixture")) : mode === "401" ? SESSION_401() : Promise.resolve(jsonResponse(200, STATUS_FIXTURE));
  const { client, ids, toast, live } = toastClient({ lang: "en", fetchImpl });
  await client.loadAll();
  assert.equal(ids["toast-text"].textContent, LOCALES.en.toast_error);
  mode = "401"; live.textContent = "";
  await client.loadAll();
  assert.equal(ids["toast-text"].textContent, LOCALES.en.toast_session);
  assert.equal(live.textContent, LOCALES.en.toast_session, "switching to the re-login notice is announced once");
  mode = "ok";
  await client.loadAll();
  assert.equal(toast.hidden, true);
});

test("401: ordinary failures and upstream authentication errors keep the generic notice", async () => {
  const cases = [
    () => Promise.reject(new Error("offline fixture")),
    () => Promise.resolve(jsonResponse(502, { status: "error", error: "origin auth", error_code: "origin_auth_failed", origin_status: 401 })),
    () => Promise.resolve(jsonResponse(502, { status: "error", error: "诊断数据暂不可用", error_code: "origin_auth_failed", origin_status: 403 })),
    () => Promise.resolve(jsonResponse(401, { status: "error", error: "upstream_token_rejected" })),
    () => Promise.resolve(jsonResponse(401)),
    () => Promise.resolve(jsonResponse(403, { status: "error", error: "unauthorized" })),
    () => Promise.resolve(jsonResponse(500, { status: "error", error: "unauthorized" })),
  ];
  for (const fetchImpl of cases) {
    const { client, ids, toast } = toastClient({ lang: "en", fetchImpl });
    await client.loadAll();
    assert.equal(toast.hidden, false);
    assert.equal(ids["toast-text"].textContent, LOCALES.en.toast_error);
    assert.equal(ids["toast-text"].children.length, 0, "no login link for non-session failures");
  }
});

test("401: the Worker's own session check is the only source of the re-login contract", async () => {
  const env = {
    DASHBOARD_SESSION_SECRET: "test-session-signing-value",
    DASHBOARD_SESSION_KV: new FakeKV(),
    DASHBOARD_API_TOKEN: "dashboard-api-token",
    AI_GATEWAY_ORIGIN_URL: "https://gateway.example",
  };
  for (const path of ["/api/v1/ai/quota", "/api/v1/ai/changes?days=7", "/api/diagnoses"]) {
    const response = await worker.fetch(new Request("https://dash.example" + path), env);
    assert.equal(response.status, 401);
    assert.equal((await response.json()).error, "unauthorized");
  }
});

function navClient({ hash = "", viewport = 800, scrollHeight = 4000 } = {}) {
  const harness = toastClient({ lang: "en" });
  const { client, document } = harness;
  // Two panels: the probe panel and the "not shown" panel.
  const order = ["signal", "omitted"];
  const tops = { signal: 84, omitted: 400 };
  const panels = {};
  for (const id of order) {
    panels[id] = new FakeNode(document, "section", id);
    panels[id].getBoundingClientRect = () => ({ top: tops[id] - win.scrollY, bottom: tops[id] + 300 - win.scrollY });
  }
  const makeLinks = (ids) => ids.map((id) => {
    const link = new FakeNode(document, "a");
    link.setAttribute("href", "#" + id);
    link.className = id === "signal" ? "on" : "";
    link.classList = {
      toggle(name, on) { const set = new Set(link.className.split(" ").filter(Boolean)); if (on) set.add(name); else set.delete(name); link.className = [...set].join(" "); },
    };
    return link;
  });
  const rail = makeLinks(order);
  const mob = makeLinks(order.slice(0, 4));
  const listeners = {};
  const win = {
    scrollY: 0, pageYOffset: 0, innerHeight: viewport,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    requestAnimationFrame(fn) { fn(); return 1; },
  };
  const loc = { hash };
  const baseGet = document.getElementById;
  document.getElementById = (id) => panels[id] || baseGet(id);
  document.querySelectorAll = (sel) => sel.includes(".rail") ? [...rail, ...mob] : [];
  document.documentElement.scrollHeight = scrollHeight;
  document.documentElement.clientHeight = viewport;
  client.window = win; client.location = loc;
  const scrollTo = (y, newHash) => {
    win.scrollY = win.pageYOffset = Math.min(y, scrollHeight - viewport);
    if (newHash !== undefined && newHash !== loc.hash) { loc.hash = newHash; for (const fn of listeners.hashchange || []) fn({}); }
    for (const fn of listeners.scroll || []) fn({});
  };
  const input = (type = "wheel") => { for (const fn of listeners[type] || []) fn({}); };
  const active = (links) => links.filter((link) => /\bon\b/.test(link.className)).map((link) => link.getAttribute("href"));
  client.initNav();
  return { client, rail, mob, scrollTo, active, listeners, tops, panels, input };
}

test("nav: active item follows the panel scrolled to, on desktop and mobile navs", () => {
  const nav = navClient();
  assert.deepEqual(nav.active(nav.rail), ["#signal"]);
  for (const id of ["omitted"]) {
    nav.scrollTo(nav.tops[id], "#" + id);
    assert.deepEqual(nav.active(nav.rail), ["#" + id], "rail highlights " + id);
    assert.deepEqual(nav.active(nav.mob), ["#" + id], "390px nav highlights " + id);
    assert.equal(nav.rail.find((link) => link.getAttribute("href") === "#" + id).getAttribute("aria-current"), "location");
    assert.equal(nav.rail[0].getAttribute("aria-current"), null, "the upstream panel is no longer current");
  }
  // Plain scrolling after a user gesture releases the pin the loop left behind.
  nav.input("mousedown");
  nav.scrollTo(0);
  assert.deepEqual(nav.active(nav.rail), ["#signal"]);
  nav.scrollTo(500);
  assert.deepEqual(nav.active(nav.rail), ["#omitted"]);
});

test("nav: direct hash link and back/forward select the matching item", () => {
  // Direct load: the browser has already scrolled to the hash target.
  const nav = navClient({ hash: "#omitted" });
  nav.scrollTo(nav.tops.omitted);
  assert.deepEqual(nav.active(nav.rail), ["#omitted"]);
  // Back to the top, then forward again.
  nav.scrollTo(0, "#signal");
  assert.deepEqual(nav.active(nav.rail), ["#signal"]);
  nav.scrollTo(nav.tops.omitted, "#omitted");
  assert.deepEqual(nav.active(nav.mob), ["#omitted"]);
  // Back to the bare page.
  nav.scrollTo(0, "");
  assert.deepEqual(nav.active(nav.rail), ["#signal"]);
});

test("nav: initial render highlights from the current scroll position", () => {
  const harnessHash = "#omitted";
  // Viewport short enough that the second panel is still below the fold.
  const nav = navClient({ hash: harnessHash, viewport: 300 });
  // initNav ran with scroll 0 (no hash-driven scroll yet): the top panel is correct.
  assert.deepEqual(nav.active(nav.rail), ["#signal"]);
  assert.equal(nav.rail.filter((link) => /\bon\b/.test(link.className)).length, 1);
});

test("nav: markup keeps the existing appearance hooks", () => {
  const page = dashboardHTML();
  assert.match(page, /<nav class="rail"[^>]*>[^]*?<a class="on" href="#signal">/);
  assert.match(page, /<nav class="mobnav"[^>]*>\s*<a class="on" href="#signal">/);
  assert.match(page, /\.rail a\.on,\.rail a:focus-visible\{/);
  assert.match(page, /\.mobnav a\.on,\.mobnav a:focus-visible\{/);
});

test("nav: back/forward with a browser-restored scroll keeps the hash target while it is visible", () => {
  const nav = navClient();
  // Chromium can restore y=0 for a same-document #omitted entry; the panel is
  // still on screen, so the restored hash stays highlighted.
  nav.scrollTo(0, "#omitted");
  assert.deepEqual(nav.active(nav.rail), ["#omitted"]);
  assert.deepEqual(nav.active(nav.mob), ["#omitted"]);
  // Restored far past the hash target: follow the actual view instead.
  nav.scrollTo(1000, "#signal");
  assert.deepEqual(nav.active(nav.rail), ["#omitted"], "restored past the target: the view decides");
  nav.scrollTo(0);
  assert.deepEqual(nav.active(nav.rail), ["#signal"], "pinned target off screen does not stay lit");
});

test("nav: user scroll input releases the hash pin and the view decides", () => {
  const nav = navClient();
  nav.scrollTo(0, "#omitted");
  assert.deepEqual(nav.active(nav.rail), ["#omitted"]);
  for (const type of ["wheel", "touchstart", "keydown", "mousedown"]) {
    nav.scrollTo(0, "#omitted");
    nav.scrollTo(nav.tops.omitted);
    nav.input(type);
    nav.scrollTo(0);
    assert.deepEqual(nav.active(nav.rail), ["#signal"], type + " releases the pin");
    nav.scrollTo(1, "");
  }
});

test("nav: activating the current fragment re-pins its target", () => {
  const nav = navClient();
  // The pin is the only thing keeping a target lit when it cannot reach the line.
  nav.tops.omitted = 2950;
  nav.scrollTo(99999, "#omitted");
  assert.deepEqual(nav.active(nav.rail), ["#omitted"]);
  assert.equal(nav.client.navPin, "omitted");
  nav.input("mousedown");
  assert.equal(nav.client.navPin, "", "pointer press releases the pin");
  nav.rail.find((link) => link.getAttribute("href") === "#omitted").dispatch("click");
  assert.equal(nav.client.navPin, "omitted", "same-fragment activation pins the clicked target");
  assert.deepEqual(nav.active(nav.rail), ["#omitted"]);
  for (const fn of nav.listeners.scroll || []) fn({});
  assert.deepEqual(nav.active(nav.rail), ["#omitted"], "a later scroll event keeps the new pin");
});

test("401: refresh keeps the focused re-login link in place", async () => {
  const { client, ids } = toastClient({ lang: "en", fetchImpl: SESSION_401 });
  await client.loadAll();
  const link = ids["toast-text"].children[0];
  link.focus();
  assert.equal(client.document.activeElement, link);
  await client.loadAll();
  assert.equal(ids["toast-text"].children[0], link, "the same anchor survives the refresh");
  assert.equal(client.document.activeElement, link, "focus stays on the login action");
  client.lang = "zh-CN"; client.applyI18n();
  assert.equal(ids["toast-text"].children[0], link, "locale changes update the anchor in place");
  assert.equal(link.textContent, LOCALES["zh-CN"].toast_session);
});

// The rail costs 76px and the page adds 48px of padding, so the single-row
// header cannot fit at 768px. These assertions keep the narrow layout and the
// intermediate two-row header in place without a browser in CI.
test("layout: the narrow breakpoint drops the rail and wraps the header", async () => {
  const { CSS } = await import("../src/ui/styles.mjs");
  const narrow = CSS.match(/@media \(max-width:1000px\)\{[\s\S]*?\.rail\{display:none\}[\s\S]*?\.top\{height:auto;padding:14px 0 12px;flex-wrap:wrap;gap:10px\}/);
  assert.ok(narrow, "the narrow block hides the rail and lets the header wrap");
  assert.ok(!/@media \(max-width:[0-9]+px\)\{[^}]*\.rail\{display:block/.test(CSS));
});

test("layout: the intermediate breakpoint gives the title its own row", async () => {
  const { CSS } = await import("../src/ui/styles.mjs");
  const mid = CSS.match(/@media \(max-width:1080px\)\{([\s\S]*?\.top \.grow\{display:none\})/);
  assert.ok(mid, "an intermediate breakpoint exists");
  assert.match(mid[1], /\.top\{[^}]*flex-wrap:wrap/);
  assert.match(mid[1], /\.top h1\{flex:1 1 100%\}/);
  assert.match(mid[1], /\.top \.grow\{display:none\}/);
});

test("layout: header controls never wrap their own label", async () => {
  const { CSS } = await import("../src/ui/styles.mjs");
  const rule = (selector) => CSS.match(new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\{([^}]*)\\}"));
  for (const selector of [".chip", ".seg button", ".user a"]) {
    const found = rule(selector);
    assert.ok(found, selector + " is styled");
    assert.match(found[1], /white-space:nowrap/, selector + " keeps its label on one line");
  }
});

test("layout: the probe table has fixed columns and readable verdict notes", async () => {
  const { CSS } = await import("../src/ui/styles.mjs");
  for (const column of [1, 2, 3, 4, 5]) {
    assert.match(CSS, new RegExp("#probe-table th:nth-child\\(" + column + "\\),#probe-table td:nth-child\\(" + column + "\\)\\{width:[0-9]+%\\}"), "column " + column);
  }
  assert.match(CSS, /\.probe-note\{[^}]*text-transform:none/);
  assert.match(CSS, /\.probe-note\{[^}]*white-space:normal/);
  assert.match(CSS, /\.table-wrap\{overflow:auto\}/);
});

test("layout: the probe table wraps instead of scrolling sideways at 768px", async () => {
  const { CSS } = await import("../src/ui/styles.mjs");
  const narrow = CSS.slice(CSS.indexOf("@media (max-width:1000px)"));
  assert.ok(narrow, "the narrow block exists");
  assert.match(narrow, /#probe-table th,#probe-table td\{padding:9px 7px\}/);
  assert.match(narrow, /#probe-table td\{white-space:normal\}/);
  assert.match(narrow, /#probe-table td:nth-child\(4\),#probe-table td:nth-child\(5\)\{white-space:nowrap\}/);
});

test("t1599: at phone width the probe chip wraps so HTTP and latency stay on screen", async () => {
  const { CSS } = await import("../src/ui/styles.mjs");
  const at = CSS.indexOf("@media (max-width:560px)");
  assert.ok(at > CSS.indexOf("@media (max-width:1000px)"), "phone block follows the 1000px block");
  const phone = CSS.slice(at);
  assert.match(phone, /#probe-table \.chip\{[^}]*white-space:normal/);
  assert.match(phone, /#probe-table \.chip \.lamp\{[^}]*flex:none/);
  // The global chip rule elsewhere keeps its one-line label.
  assert.match(CSS, /(^|\n)\.chip\{[^}]*white-space:nowrap/);
});

// --- Upstream status endpoint (GET /api/upstream-status) --------------------
// The page's only read. These cases pin what the probe is allowed to claim.

test("upstream status: requires the dashboard session and an explicit GET", async (t) => {
  const anonymous = await worker.fetch(
    new Request("https://dash.example/api/upstream-status"),
    { AI_GATEWAY_ORIGIN_URL: "https://gateway.example", DASHBOARD_API_TOKEN: "dashboard-api-token" },
  );
  assert.equal(anonymous.status, 401);
  assert.equal((await anonymous.json()).error, "unauthorized");

  const { env, cookie, gatewayCalls } = await authenticatedDashboard(t, async () => new Response("", { status: 401 }));
  resetUpstreamStatusCache();
  const post = await worker.fetch(
    new Request("https://dash.example/api/upstream-status", { method: "POST", headers: { Cookie: cookie } }),
    env,
  );
  assert.equal(post.status, 405);
  assert.equal(post.headers.get("allow"), "GET");
  assert.equal(gatewayCalls.length, 0, "a non-GET never reaches the origin");
});

test("upstream status: reports each verdict and why task counts are absent", async (t) => {
  const seen = [];
  const { env, cookie } = await authenticatedDashboard(t, async (url, init = {}) => {
    seen.push({ url, authorization: (init.headers || {}).Authorization });
    const path = new URL(url).pathname;
    if (path === "/v1/ai/health") return new Response("missing", { status: 404 });
    if (path === "/.well-known/oauth-protected-resource") return Response.json({ resource: "synthetic" });
    return new Response("", { status: 401 });
  });
  resetUpstreamStatusCache();
  const response = await worker.fetch(
    new Request("https://dash.example/api/upstream-status", { headers: { Cookie: cookie } }),
    env,
  );
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(body.status, "ok");
  assert.equal(body.overall, "online");
  const byId = Object.fromEntries(body.probes.map((probe) => [probe.id, probe]));
  assert.equal(byId.tasks.verdict, "auth_required");
  assert.equal(byId.tasks.http_status, 401);
  assert.equal(byId.oauth_metadata.verdict, "reachable");
  assert.equal(byId.retired_health.verdict, "not_found");
  assert.deepEqual(body.read_model, { task_counts: false, reason_code: "read_credentials_rejected" });
  // Credentialed probes carry the token; anonymous ones never do.
  assert.equal(seen.filter((call) => call.authorization === "Bearer dashboard-api-token").length, 2);
  // The payload names paths and codes, never the token or the origin host.
  const text = JSON.stringify(body);
  assert.ok(!text.includes("dashboard-api-token"));
  assert.ok(!text.includes("gateway.example"));
});

test("upstream status: a second read inside the cache window does not re-probe", async (t) => {
  let calls = 0;
  const { env, cookie } = await authenticatedDashboard(t, async () => {
    calls += 1;
    return new Response("", { status: 401 });
  });
  resetUpstreamStatusCache();
  const read = async () => worker.fetch(
    new Request("https://dash.example/api/upstream-status", { headers: { Cookie: cookie } }),
    env,
  ).then((response) => response.json());
  const first = await read();
  const afterFirst = calls;
  const second = await read();
  assert.equal(first.cached, false);
  assert.equal(second.cached, true);
  assert.equal(calls, afterFirst, "the cache window serves the second read");
});

test("upstream status: a missing origin is a machine code, not an empty table", async (t) => {
  const { env, cookie } = await authenticatedDashboard(t, async () => {
    throw new Error("the origin must not be contacted without a URL");
  });
  const withoutOrigin = { ...env, AI_GATEWAY_ORIGIN_URL: "" };
  resetUpstreamStatusCache();
  const response = await worker.fetch(
    new Request("https://dash.example/api/upstream-status", { headers: { Cookie: cookie } }),
    withoutOrigin,
  );
  assert.equal(response.status, 502);
  const body = await response.json();
  assert.equal(body.status, "error");
  assert.equal(body.error_code, "origin_not_configured");
  assert.deepEqual(body.probes, []);
  assert.equal(body.read_model.reason_code, "origin_unavailable");
});

test("upstream status: without a dashboard token the credentialed probes are skipped", async (t) => {
  const authorizations = [];
  const { env, cookie } = await authenticatedDashboard(t, async (url, init = {}) => {
    authorizations.push((init.headers || {}).Authorization);
    return new Response("", { status: 401 });
  });
  const { DASHBOARD_API_TOKEN, ...withoutToken } = env;
  resetUpstreamStatusCache();
  const body = await worker.fetch(
    new Request("https://dash.example/api/upstream-status", { headers: { Cookie: cookie } }),
    withoutToken,
  ).then((response) => response.json());
  const byId = Object.fromEntries(body.probes.map((probe) => [probe.id, probe]));
  assert.equal(byId.tasks_dashboard_stats.verdict, "skipped");
  assert.equal(byId.retired_health.verdict, "skipped");
  assert.equal(byId.tasks_anonymous_stats.verdict, "auth_required");
  assert.equal(body.read_model.reason_code, "dashboard_token_missing");
  assert.ok(authorizations.every((value) => value === undefined), "no probe is sent with an empty bearer");
});

test("upstream status: an unreachable origin degrades every probe and says so", async (t) => {
  const { env, cookie } = await authenticatedDashboard(t, async () => {
    throw new TypeError("synthetic network failure");
  });
  resetUpstreamStatusCache();
  const body = await worker.fetch(
    new Request("https://dash.example/api/upstream-status", { headers: { Cookie: cookie } }),
    env,
  ).then((response) => response.json());
  assert.equal(body.overall, "unreachable");
  assert.ok(body.probes.length > 0);
  for (const probe of body.probes) {
    assert.equal(probe.verdict, "unreachable");
    assert.equal(probe.http_status, null, "no status is invented for a timeout");
  }
  assert.equal(body.read_model.reason_code, "origin_unreachable");
});

// --- Review findings on the upstream probe ---------------------------------

test("probeUrl drops the legacy /v1/codex-audit endpoint base for every probe", () => {
  assert.equal(LEGACY_ENDPOINT_BASE, "/v1/codex-audit");
  const legacy = "https://gateway.example" + LEGACY_ENDPOINT_BASE + "/";
  for (const path of ["/v1/tasks", "/v1/tasks/stats", "/mcp", "/.well-known/oauth-protected-resource", "/v1/ai/health"]) {
    assert.equal(probeUrl(legacy, path), "https://gateway.example" + path, path);
  }
  // A real base path is still a prefix; only the legacy endpoint is dropped.
  assert.equal(probeUrl("https://gateway.example/service/", "/v1/tasks"), "https://gateway.example/service/v1/tasks");
  assert.throws(() => probeUrl("http://gateway.example", "/v1/tasks"), /origin_insecure/);
});

test("readModel never infers which credentials the service accepts", () => {
  const probe = (id, verdict) => ({ id, verdict });
  for (const status of [401, 403]) {
    assert.equal(classifyProbe(status), "auth_required");
    const model = readModel([
      probe("tasks", "auth_required"),
      probe("tasks_anonymous_stats", "auth_required"),
      probe("tasks_dashboard_stats", "auth_required"),
    ]);
    assert.deepEqual(model, { task_counts: false, reason_code: "read_credentials_rejected" });
  }
  // A valid workload credential without tasks:list can produce exactly this
  // pair of verdicts; the copy must not point the operator at OAuth.
  for (const lang of ["en", "zh-CN"]) {
    const copy = LOCALES[lang].rm_read_credentials_rejected;
    assert.ok(copy, lang + " has an explanation");
    assert.ok(!/oauth/i.test(copy), lang + " does not claim an OAuth-only service");
  }
  assert.equal(readModel([
    probe("tasks_anonymous_stats", "reachable"),
    probe("tasks_dashboard_stats", "auth_required"),
  ]).reason_code, "dashboard_token_rejected");
});

test("upstream status: concurrent viewers share one in-flight probe batch", async (t) => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let calls = 0;
  const { env, cookie, gatewayCalls } = await authenticatedDashboard(t, async () => {
    calls += 1;
    await gate;
    return new Response("", { status: 401 });
  });
  resetUpstreamStatusCache();
  const reads = Array.from({ length: 10 }, () => worker.fetch(
    new Request("https://dash.example/api/upstream-status", { headers: { Cookie: cookie } }),
    env,
  ).then((response) => response.json()));
  // Let the first batch start before releasing it; every later read must join it.
  await new Promise((resolve) => setTimeout(resolve, 0));
  release();
  const bodies = await Promise.all(reads);
  // With no in-flight sharing this is 60 calls: all ten requests see an empty
  // cache while the first batch is blocked on the gate. The exact mix of
  // in-flight joins and short-cache hits is scheduling-dependent, so only the
  // upstream call count is asserted.
  assert.equal(calls, 6, "one six-probe batch, not one per viewer");
  assert.equal(gatewayCalls.length, 6);
  for (const body of bodies) assert.equal(body.status, "ok");
});

test("a denied probe never tells the operator which credential the service accepts", () => {
  for (const lang of ["en", "zh-CN"]) {
    const { client } = toastClient({ lang, extraIds: ["probe-table"] });
    client.state.status = { status: "ok", data: structuredClone(STATUS_FIXTURE) };
    client.renderProbes();
    const text = client.document.getElementById("probe-table").textContent;
    assert.ok(!/oauth/i.test(text), lang + " verdict copy does not claim OAuth-only access");
    assert.ok(text.length > 0, lang + " still explains the denied read");
  }
  for (const lang of ["en", "zh-CN"]) {
    for (const key of ["v_auth_required", "m_auth_required"]) {
      assert.ok(LOCALES[lang][key], lang + " " + key + " exists");
      assert.ok(!/oauth/i.test(LOCALES[lang][key]), lang + " " + key);
    }
  }
});
