import assert from "node:assert/strict";
import test from "node:test";

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
} from "../src/index.mjs";
import { LOCALES } from "../src/ui/locales.mjs";
import { loginHTML } from "../src/ui/login.mjs";
import { dashboardHTML } from "../src/ui/dashboard.mjs";
import {
  classifyLoginError,
  escapeHtml,
  healthReadouts,
  jsObjectLiteral,
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
  for (const value of ["redirect", "oversized"]) {
    scenario = value;
    const response = await worker.fetch(
      new Request("https://dash.example/api/diagnoses", { headers: { Cookie: dashboard.cookie } }),
      dashboard.env,
    );
    assert.equal(response.status, 502, value);
    assert.deepEqual(await response.json(), { status: "error", error: "诊断数据暂不可用" });
  }
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
  assert.match(html, /function codexRemainingPercent/);
  assert.match(html, /Codex 账户/);
  assert.match(html, /剩余/);

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
  assert.doesNotMatch(page, /<form\b/i);
  assert.doesNotMatch(page, /<input\b/i);
  assert.doesNotMatch(page, /<select\b/i);
  assert.doesNotMatch(page, /type="password"/i);
  assert.doesNotMatch(page, /type="email"/i);
  assert.match(page, /id="alert" role="alert"/);
  assert.match(page, /function classifyLoginError/);
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

test("dashboard keeps honest unknown states and the read adapter", () => {
  const page = dashboardHTML();
  assert.match(page, /var ROUTES=\{/);
  assert.match(page, /health:"\/v1\/ai\/health"/);
  assert.match(page, /diagnoses:"\/diagnoses"/);
  assert.doesNotMatch(page, /"\/v2\//);
  assert.doesNotMatch(page, /MOCK/);
  assert.match(page, /not_configured/);
  assert.match(page, /未配置/);
  assert.match(page, /bar na/);
  assert.match(page, /panel-error/);
  assert.match(page, /进程内 · 5 分钟窗口/);
  assert.match(page, /process-local · 5-min window/);
  assert.match(page, /t\.k_bal_f/);
  assert.doesNotMatch(page, /innerHTML\s*=/);
  assert.match(page, /textContent/);
  assert.match(page, /qg-lang/);
  assert.match(page, /qg-theme/);
  assert.match(page, /prefers-color-scheme/);
});

test("failed reads replace stale values with per-panel errors", () => {
  const page = dashboardHTML();
  for (const panel of ["health", "quota", "org", "autonomy-summary", "changes", "effectiveness", "diagnoses"]) {
    assert.ok(page.includes('panelError("' + panel + '"'), panel);
  }
  assert.match(page, /att_human\+" · "\+t\.error_title/);
  assert.match(page, /att_shadow\+" · "\+t\.error_title/);
  assert.match(page, /state\.health\.status==="error"\?t\.unavailable/);
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
