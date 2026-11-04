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
  assert.match(page, /var ROUTES=\{/);
  assert.match(page, /health:"\/v1\/ai\/health"/);
  assert.match(page, /diagnoses:"\/diagnoses"/);
  assert.match(page, /not_configured/);
  assert.match(page, /进程内 · 5 分钟窗口/);
  assert.match(page, /process-local · 5-min window/);
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

function endpointClient() {
  const script = [...dashboardHTML().matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
  const source = script.slice(0, script.lastIndexOf('document.querySelectorAll(".js-lang")'));
  const context = vm.createContext({
    document: { documentElement: { lang: "en", getAttribute: () => "light" } },
    URL,
  });
  vm.runInContext(source, context);
  return context;
}

test("endpoint health never infers success from historical request metrics", () => {
  const client = endpointClient();
  for (const fields of [
    {}, { errors: 0 }, { errors: null }, { errors: -1 },
    { errors: "invalid" }, { errors: 9 }, { errors: 1 },
    { status: "unavailable", errors: 0 }, { status: "stale", errors: 0 },
    { status: "unknown", errors: 0 },
  ]) {
    assert.equal(client.endpointStatus({ path: "/synthetic/tasks", total: 8, ...fields }, []), "unknown");
  }
  for (const status of ["healthy", "degraded", "unhealthy", "unknown"]) {
    assert.equal(client.endpointStatus({ status }, []), status);
  }
  assert.equal(client.endpointStatus({ path: "/synthetic/tasks", total: 8 }, [
    { path: "/synthetic/tasks", reason: "error_rate" },
  ]), "degraded");
});

test("egress requirement stays unverified in both languages without observations", () => {
  const page = dashboardHTML();
  assert.equal(/<span class="lamp st-unk" aria-hidden="true"><\/span><span data-i18n="d_chip">/.test(page), true);
  assert.equal(LOCALES.en.d_chip, "egress · SG · unverified");
  assert.equal(LOCALES["zh-CN"].d_chip, "出口 · SG · 未验证");
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
