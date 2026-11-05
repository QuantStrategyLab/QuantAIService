const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const workflow = process.env.RISK_REVISION
  ? require('node:child_process').execFileSync('git', ['show', `${process.env.RISK_REVISION}:.github/workflows/risk-gate.yml`], {encoding: 'utf8'})
  : fs.readFileSync(process.env.RISK_WORKFLOW || path.join(__dirname, '../.github/workflows/risk-gate.yml'), 'utf8');
function embedded(job) {
  const section = workflow.split(`  ${job}:\n`)[1]?.split(/\n  [a-z_]+:\n/)[0];
  return section?.split('          script: |\n')[1]?.split('\n').map(x => x.slice(12)).join('\n');
}
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const execute = new AsyncFunction('github', 'context', 'core', 'process', embedded('evaluate'));
const discover = new AsyncFunction('github', 'context', 'core', 'process', embedded('discover'));
const record = embedded('record_label') && new AsyncFunction('github', 'context', 'core', 'process', embedded('record_label'));
const H = 'a'.repeat(40), B = 'b'.repeat(40);
const body = '## AI assistance\nCodex, configured model.\n## What changed\nChange ordinary risk logic.\n## How verified\nRun offline regression tests.';
function fixture() {
  return {pr: {number: 1, state: 'open', body, changed_files: 1, labels: [{name: 'agent-approved'}],
    head: {sha: H}, base: {sha: B, ref: 'main', repo: {full_name: '<PROJECT_ID>/sample'}}},
    files: [{filename: 'src/runtime/model.js', status: 'modified'}],
    comments: [{id: 1, user: {type: 'Bot', login: 'chatgpt-codex-connector[bot]'}, performed_via_github_app: {id: 12345},
      updated_at: new Date(Date.now() - 60000).toISOString(), body: marker()}],
    checks: [{id: 1, name: 'Unit tests', head_sha: H, app: {id: 23456}, status: 'completed', conclusion: 'success'}],
    events: [{id: 10, event: 'labeled', label: {name: 'agent-approved'}, actor: {login: '<PROJECT_ID>-owner'}, created_at: '2026-01-01T00:00:00Z'}],
    open: [{number: 1, head: {sha: H}}], branch: {commit: {sha: B}}, compare: {merge_base_commit: {sha: B}},
    statuses: [{id: 10, context: 'clearing-label', state: 'success', description: 'agent-approved for PR 1 at event 10 on this head'}], rules: [], policy: {codex_app_ids: [12345]},
    required: [{context: 'risk-gate', app: null},
      {context: 'pr-template', app: null}, {context: 'Unit tests', app: {databaseId: 23456}}]};
}
function marker(changes = {}) {
  return `<!-- codex-review-state:v1 ${JSON.stringify({code_review: {commit_sha: H,
    status: 'completed', has_any_findings: false, has_unresolved_findings: false, ...changes}})} -->`;
}
async function run(f = fixture()) {
  const out = {statuses: [], errors: [], reads: {}, failures: [], statusRefs: []};
  const clone = x => structuredClone(x);
  const read = key => {
    out.reads[key] = (out.reads[key] || 0) + 1;
    if (f.apiError === key) throw new Error('private diagnostic <PROJECT_ID>');
    return clone(out.reads[key] > 1 && f.changed?.[key] || f[key]);
  };
  const github = {rest: {pulls: {get: async () => ({data: read('pr')}), listFiles: 'files', list: 'open'},
    issues: {listComments: 'comments', listEvents: 'events'},
    checks: {listForRef: 'checks'}, repos: {listCommitStatusesForRef: 'statuses',
      getBranchRules: 'rules', listPullRequestsAssociatedWithCommit: 'associated',
      getBranch: async () => ({data: read('branch')}), compareCommits: async () => ({data: read('compare')}),
      getCombinedStatusForRef: async () => ({data: read('combined')}),
      getContent: async params => {out.policyRef = params.ref; return ({data: {encoding: 'base64', content: Buffer.from(JSON.stringify(read('policy'))).toString('base64')}});},
      createCommitStatus: async params => {if (f.apiError === 'publish') throw new Error('private diagnostic <PROJECT_ID>'); out.statuses.push(params);}}},
    paginate: async (method, params) => {
      if (method === 'statuses') out.statusRefs.push(params.ref);
      return read(method);
    },
    graphql: async () => {const checks = read('required'); return {repository: {pullRequest: {baseRef: {
      branchProtectionRule: f.noClassic ? null : {requiredStatusChecks: checks, requiredStatusCheckContexts: checks.map(x => x.context)}}}}};}};
  const context = {repo: {owner: '<PROJECT_ID>', repo: 'sample'}, serverUrl: 'https://example.invalid', runId: 1,
    eventName: f.eventName || 'pull_request_target', payload: f.payload || {pull_request: {number: 1}}};
  const core = {warning: text => out.errors.push(text), setFailed: text => out.failures.push(text), setOutput: (name, value) => {out[name] = value;}};
  try {
    if (f.recordOnly) { if (!record) throw new Error('Recorder absent'); await record(github, context, core, {env: {APPROVAL_OWNER: '<PROJECT_ID>-owner'}}); }
    else if (f.eventOnly) await discover(github, context, core, {env: {PR_NUMBER: '', APPROVAL_OWNER: '<PROJECT_ID>-owner'}});
    const numbers = f.recordOnly ? [] : f.eventOnly ? JSON.parse(out.prs || '[]') : [1];
    for (const number of numbers) await execute(github, context, core, {env: {PR_NUMBER: String(number), APPROVAL_OWNER: '<PROJECT_ID>-owner'}});
  } catch (e) {out.errors.push(e.message);}
  out.risk = out.statuses.filter(x => x.context === 'risk-gate').at(-1)?.state;
  return out;
}
test('Ordinary high-risk paths pass only with exact-head clean review and required checks', async () => {
  assert.equal((await run()).risk, 'success');
});
for (const mutation of ['stale', 'running', 'unresolved', 'malformed', 'duplicate', 'untrusted', 'human', 'timestamp', 'missing', 'boolean', 'newer']) {
  test(`Reject invalid review: ${mutation}`, async () => {
    const f = fixture(), c = f.comments[0];
    if (mutation === 'stale') c.body = marker({commit_sha: B});
    if (mutation === 'running') c.body = marker({status: 'running'});
    if (mutation === 'unresolved') c.body = marker({has_unresolved_findings: true});
    if (mutation === 'malformed') c.body = '<!-- codex-review-state:v1 {bad} -->';
    if (mutation === 'duplicate') c.body += marker({has_unresolved_findings: true});
    if (mutation === 'untrusted') c.performed_via_github_app.id = 54321;
    if (mutation === 'human') c.user.type = 'User';
    if (mutation === 'timestamp') c.updated_at = 'invalid';
    if (mutation === 'missing') f.comments = [];
    if (mutation === 'boolean') c.body = marker({has_unresolved_findings: 'false'});
    if (mutation === 'newer') f.comments.push({...c, id: 2, body: marker({status: 'running'})});
    assert.notEqual((await run(f)).risk, 'success');
  });
}
test('Findings already resolved do not require another owner label', async () => {
  const f = fixture(); f.comments[0].body = marker({has_any_findings: true});
  assert.equal((await run(f)).risk, 'success');
});
for (const filename of ['docs/secrets.md', 'tests/deploy/test.js', 'src/iam/policy.json',
  'src/broker_credentials.json', '.env.local', 'infra/main.tf', '.github/workflows/deploy.yml',
  'src/liveTrading.js', 'src/stop_loss.js', 'src/kelly.py', 'src/orders/run.js',
  '.github/governance/policy.json', '.github/workflows/risk-gate.yml']) {
  test(`Sensitive source path accepts head-bound agent clearance: ${filename}`, async () => {
    const f = fixture(); f.files[0].filename = filename;
    assert.equal((await run(f)).risk, 'success');
    f.pr.labels = [];
    assert.notEqual((await run(f)).risk, 'success');
  });
}
test('Renaming a sensitive file to ordinary code keeps the clearing-label requirement', async () => {
  const f = fixture(); f.files[0].status = 'renamed'; f.files[0].previous_filename = 'src/secrets.js'; f.pr.labels = [];
  assert.notEqual((await run(f)).risk, 'success');
});
for (const mutation of ['failure', 'skipped', 'neutral', 'pending', 'missing', 'wrong-app', 'stale', 'newer-failure', 'status-pending']) {
  test(`Reject incomplete required checks: ${mutation}`, async () => {
    const f = fixture(), c = f.checks[0];
    if (['failure', 'skipped', 'neutral'].includes(mutation)) c.conclusion = mutation;
    if (mutation === 'pending') c.status = 'in_progress';
    if (mutation === 'missing') f.checks = [];
    if (mutation === 'wrong-app') c.app.id = 54321;
    if (mutation === 'stale') c.head_sha = B;
    if (mutation === 'newer-failure') f.checks.push({...c, id: 2, conclusion: 'failure'});
    if (mutation === 'status-pending') {f.required[2].app = null; f.statuses = [{id: 2, context: c.name, state: 'pending'}];}
    assert.notEqual((await run(f)).risk, 'success');
  });
}
test('Legacy status responses without sha are bound to the immutable request ref', async () => {
  const f = fixture(); f.checks = []; f.required[2].app = null;
  f.statuses.push({id: 1, context: 'Unit tests', state: 'success'});
  const out = await run(f);
  assert.equal(out.risk, 'success');
  assert.deepEqual(out.statusRefs, [H, H]);
});
test('Legacy status approval rejects every non-success response', async () => {
  const f = fixture(); f.checks = []; f.required[2].app = null;
  f.statuses.push({id: 1, context: 'Unit tests', state: 'success'});
  assert.equal((await run(f)).risk, 'success');
  for (const state of ['failure', 'error', 'pending', undefined, 'unknown']) {
    f.statuses.find(x => x.context === 'Unit tests').state = state;
    assert.notEqual((await run(f)).risk, 'success');
  }
});
test('Legacy status success cannot survive a concurrent head change', async () => {
  const f = fixture(); f.checks = []; f.required[2].app = null;
  f.statuses.push({id: 1, context: 'Unit tests', state: 'success'});
  assert.equal((await run(f)).risk, 'success');
  f.changed = {pr: structuredClone(f.pr)};
  f.changed.pr.head.sha = B;
  const out = await run(f);
  assert.notEqual(out.risk, 'success');
  assert.deepEqual(out.statusRefs, [H, H]);
});
test('Ruleset checks are enforced alongside classic protection', async () => {
  const f = fixture(); f.rules = [{type: 'required_status_checks', parameters: {required_status_checks: [{context: 'Security', integration_id: 23456}]}}];
  assert.notEqual((await run(f)).risk, 'success');
});
test('No discoverable required checks cannot authorize automatic approval', async () => {
  const f = fixture(); f.noClassic = true;
  assert.notEqual((await run(f)).risk, 'success');
});
test('Bad PR template cannot pass through the self-dependency exclusion', async () => {
  const f = fixture(); f.pr.body = 'TODO'; assert.notEqual((await run(f)).risk, 'success');
});
for (const key of ['comments', 'checks', 'statuses', 'rules', 'required', 'policy', 'files', 'pr']) {
  test(`API failure is closed and redacted: ${key}`, async () => {
    const f = fixture(); f.apiError = key;
    const out = await run(f);
    assert.notEqual(out.risk, 'success');
    assert.ok(!JSON.stringify(out).includes('private diagnostic'));
  });
}
for (const key of ['pr', 'comments', 'checks', 'rules', 'required', 'files', 'statuses']) {
  test(`Evidence drift between reads fails closed: ${key}`, async () => {
    const f = fixture(); f.changed = {[key]: structuredClone(f[key])};
    if (key === 'pr') f.changed.pr.base.sha = 'c'.repeat(40);
    if (key === 'comments') f.changed.comments[0].body = marker({has_unresolved_findings: true});
    if (key === 'checks') f.changed.checks[0].conclusion = 'failure';
    if (key === 'rules') f.changed.rules.push({type: 'required_status_checks', parameters: {required_status_checks: [{context: 'New gate'}]}});
    if (key === 'required') f.changed.required.push({context: 'New gate', app: null});
    if (key === 'files') f.changed.files[0].filename = 'src/secrets.js';
    if (key === 'statuses') f.changed.statuses.push({id: 2, context: 'Unit tests', state: 'pending'});
    assert.notEqual((await run(f)).risk, 'success');
  });
}
test('Low-risk prose passes with clean review and checks', async () => {
  const f = fixture(); f.files[0].filename = 'docs/example.md'; assert.equal((await run(f)).risk, 'success');
});
test('Concurrent evaluations do not share decisions', async () => {
  const f = fixture(); f.comments = [];
  const [good, bad] = await Promise.all([run(), run(f)]);
  assert.equal(good.risk, 'success'); assert.notEqual(bad.risk, 'success');
});
test('Workflow refreshes on review and check events without new write permissions', () => {
  for (const event of ['issue_comment:', 'check_run:', 'check_suite:', 'status:']) assert.ok(workflow.includes(event));
  assert.doesNotMatch(workflow, /checkout|secrets:|issues: write|pull-requests: write|actions: write/);
});
test('A low-risk result cannot survive base drift', async () => {
  const f = fixture(); f.files[0].filename = 'docs/example.md'; f.changed = {pr: structuredClone(f.pr)};
  f.changed.pr.base.sha = 'c'.repeat(40);
  assert.notEqual((await run(f)).risk, 'success');
});
test('Status publication errors never expose API diagnostics', async () => {
  const f = fixture(); f.apiError = 'publish'; const out = await run(f);
  assert.notEqual(out.risk, 'success'); assert.ok(!JSON.stringify(out).includes('private diagnostic'));
  assert.ok(out.failures.length);
});
test('Sensitive globs escape literal dots', async () => {
  const f = fixture(); f.files[0].filename = 'src/notXkey';
  assert.equal((await run(f)).risk, 'success');
});
test('Future review timestamps cannot authorize approval', async () => {
  const f = fixture(); f.comments[0].updated_at = '2999-01-01T00:00:00Z';
  assert.notEqual((await run(f)).risk, 'success');
});
for (const event of ['check_run', 'check_suite', 'status']) {
  test(`Check event looks up only open PRs with the current event head: ${event}`, async () => {
    const f = fixture(); f.eventOnly = true; f.payload = event === 'status' ? {sha: H} : {[event]: {head_sha: H}};
    f.associated = [{number: 1, state: 'open', head: {sha: H}}, {number: 2, state: 'closed', head: {sha: H}},
      {number: 3, state: 'open', head: {sha: B}}];
    const out = await run(f); assert.equal(out.risk, 'success'); assert.equal(out.reads.pr, 2);
  });
}
test('Review comment event reads the associated PR', async () => {
  const f = fixture(); f.eventOnly = true; f.payload = {issue: {number: 1}};
  assert.equal((await run(f)).risk, 'success');
});
test('Check event lookup failure is redacted and closed', async () => {
  const f = fixture(); f.eventOnly = true; f.payload = {sha: H}; f.apiError = 'associated';
  const out = await run(f); assert.ok(out.failures.length); assert.ok(!JSON.stringify(out).includes('private diagnostic'));
});
test('The self-produced contexts cannot claim a different App identity', async () => {
  const f = fixture(); f.required[0].app = {databaseId: 54321};
  assert.notEqual((await run(f)).risk, 'success');
});
test('A ruleset without classic protection can authorize when every check is green', async () => {
  const f = fixture(); f.noClassic = true; f.rules = [{type: 'required_status_checks', parameters: {
    required_status_checks: [{context: 'Unit tests', integration_id: 23456}]}}];
  assert.equal((await run(f)).risk, 'success');
});
test('Owner confirmation requires a matching event and head-bound receipt', async () => {
  const f = fixture(); f.files[0].filename = 'src/secrets.js'; f.pr.labels = [{name: 'owner-approved'}];
  f.events = [{id: 1, event: 'labeled', label: {name: 'owner-approved'}, actor: {login: '<PROJECT_ID>-owner'}}];
  f.statuses.push({id: 11, context: 'clearing-label', state: 'success', description: 'owner-approved for PR 1 at event 1 on this head'});
  assert.equal((await run(f)).risk, 'success');
  f.events.push({id: 2, event: 'unlabeled', label: {name: 'owner-approved'}, actor: {login: '<PROJECT_ID>-owner'}});
  assert.notEqual((await run(f)).risk, 'success');
});
test('Owner-label event revocation during evaluation blocks the old approval', async () => {
  const f = fixture(); f.files[0].filename = 'src/secrets.js'; f.pr.labels = [{name: 'owner-approved'}];
  f.events = [{id: 1, event: 'labeled', label: {name: 'owner-approved'}, actor: {login: '<PROJECT_ID>-owner'}}];
  f.changed = {events: [...f.events, {id: 2, event: 'unlabeled', label: {name: 'owner-approved'}}]};
  assert.notEqual((await run(f)).risk, 'success');
});
test('Ordinary high-risk paths cannot use a legacy owner label to bypass review', async () => {
  const f = fixture(); f.pr.labels = [{name: 'owner-approved'}]; f.comments = [];
  f.events = [{id: 1, event: 'labeled', label: {name: 'owner-approved'}, actor: {login: '<PROJECT_ID>-owner'}}];
  assert.notEqual((await run(f)).risk, 'success');
});
test('Native workflow events resolve to the same PR writer', async () => {
  const f = fixture(); f.eventOnly = true; f.payload = {workflow_run: {head_sha: H}};
  f.associated = [{number: 1, state: 'open', head: {sha: H}}];
  assert.equal((await run(f)).risk, 'success');
  assert.match(workflow, /group: risk-gate-\$\{\{ github.repository \}\}-\$\{\{ matrix.pr \}\}/);
  assert.match(workflow, /cancel-in-progress: false/);
  assert.match(workflow, /PR_NUMBER: \$\{\{ matrix.pr \}\}/);
});
for (const mutation of ['missing', 'duplicate', 'rename', 'count']) {
  test(`Incomplete file evidence blocks evaluation: ${mutation}`, async () => {
    const f = fixture();
    if (mutation === 'missing') f.files = [];
    if (mutation === 'duplicate') {f.files.push(f.files[0]); f.pr.changed_files = 2;}
    if (mutation === 'rename') f.files[0].status = 'renamed';
    if (mutation === 'count') f.pr.changed_files = 2;
    assert.notEqual((await run(f)).risk, 'success');
  });
}

// Adverse cases exercise the actual embedded trusted code, with fake APIs only.
test('Newest malformed trusted marker supersedes older clean review', async () => {
  const f = fixture(); f.comments.push({...f.comments[0], id: 2,
    updated_at: new Date(Date.now() - 1000).toISOString(), body: '<!-- codex-review-state:v1 broken'});
  assert.notEqual((await run(f)).risk, 'success');
});
test('Immutable-base trust policy cannot be replaced by the candidate policy', async () => {
  const out = await run(); assert.equal(out.policyRef, B);
});
for (const late of [false, true]) {
  test(`Shared-head collision blocks all verdicts, late=${late}`, async () => {
    const f = fixture(), collision = [...f.open, {number: 2, head: {sha: H}}];
    if (late) f.changed = {open: collision}; else f.open = collision;
    const out = await run(f); assert.equal(out.risk, 'error');
    assert.match(out.statuses.filter(x => x.context === 'risk-gate').at(-1).description, /shares this head/);
  });
}
test('Closed PR cannot publish over an open sibling', async () => {
  const f = fixture(); f.pr.state = 'closed'; f.open.push({number: 2, head: {sha: H}});
  assert.equal((await run(f)).statuses.length, 0);
});
for (const mutation of ['not-contained', 'base-advanced', 'revoked', 'wrong-actor', 'stale-record', 'wrong-pr', 'unknown-approver']) {
  test(`Head-bound clearance fails closed: ${mutation}`, async () => {
    const f = fixture();
    if (mutation === 'not-contained') f.compare.merge_base_commit.sha = H;
    if (mutation === 'base-advanced') f.changed = {branch: {commit: {sha: 'c'.repeat(40)}}};
    if (mutation === 'revoked') f.events.push({...f.events[0], id: 11, event: 'unlabeled'});
    if (mutation === 'wrong-actor') f.events[0].actor.login = 'example-untrusted';
    if (mutation === 'stale-record') f.statuses[0].description = 'agent-approved for PR 1 at event 9 on this head';
    if (mutation === 'wrong-pr') f.statuses[0].description = 'agent-approved for PR 2 at event 10 on this head';
    if (mutation === 'unknown-approver') f.events[0].actor.login = '';
    assert.notEqual((await run(f)).risk, 'success');
  });
}
test('Low-risk and dependency PRs also need independent AI review', async () => {
  for (const user of [undefined, {login: 'dependabot[bot]'}]) {
    const f = fixture(); f.pr.user = user; f.files[0].filename = 'docs/example.md'; f.comments = [];
    assert.notEqual((await run(f)).risk, 'success');
  }
});
for (const key of ['events', 'branch', 'compare', 'open']) {
  test(`New evidence API failure stays closed and redacted: ${key}`, async () => {
    const f = fixture(); f.apiError = key; const out = await run(f);
    assert.notEqual(out.risk, 'success'); assert.ok(!JSON.stringify(out).includes('private diagnostic'));
  });
}
test('Label revocation between evidence reads blocks the previous clearance', async () => {
  const f = fixture(); f.changed = {events: [...f.events, {...f.events[0], id: 11, event: 'unlabeled'}]};
  assert.notEqual((await run(f)).risk, 'success');
});
for (const eventName of ['push', 'schedule', 'pull_request_target']) {
  test(`Recovery sweeps current open PRs through the serialized writer: ${eventName}`, async () => {
    const f = fixture(); f.eventOnly = true; f.eventName = eventName;
    f.payload = eventName === 'pull_request_target' ? {action: 'closed', pull_request: {number: 99}} : {};
    const out = await run(f); assert.equal(out.prs, '[1]'); assert.equal(out.risk, 'success');
  });
}
test('Synchronize refreshes only siblings with a collision error', async () => {
  const f = fixture(); f.eventOnly = true; f.payload = {action: 'synchronize', pull_request: {number: 1}};
  f.open.push({number: 2, head: {sha: B}});
  f.combined = {statuses: [{context: 'risk-gate', state: 'error', description: 'Another open pull request shares this head; statuses would collide'}]};
  assert.equal((await run(f)).prs, '[1,2]');
  f.combined.statuses[0].description = 'Unrelated API error'; assert.equal((await run(f)).prs, '[1]');
});
function recordFixture() {
  const f = fixture(); f.recordOnly = true;
  f.payload = {label: {name: 'agent-approved'}, sender: {login: '<PROJECT_ID>-owner'},
    pull_request: {number: 1, state: 'open', head: {sha: H}, updated_at: f.events[0].created_at}};
  return f;
}
test('Recorder binds payload head, PR and exact timeline event', async () => {
  const out = await run(recordFixture()); assert.equal(out.statuses.length, 1);
  assert.equal(out.statuses[0].sha, H); assert.match(out.statuses[0].description, /PR 1 at event 10/);
});
for (const mutation of ['ambiguous', 'delayed', 'wrong-sender', 'closed', 'transport']) {
  test(`Recorder rejects stale or ambiguous deliveries: ${mutation}`, async () => {
    const f = recordFixture();
    if (mutation === 'ambiguous') f.events.push({...f.events[0], id: 11});
    if (mutation === 'delayed') f.events[0].created_at = '2026-01-02T00:00:00Z';
    if (mutation === 'wrong-sender') f.payload.sender.login = 'example-untrusted';
    if (mutation === 'closed') f.payload.pull_request.state = 'closed';
    if (mutation === 'transport') f.apiError = 'events';
    const out = await run(f); assert.equal(out.statuses.length, 0);
    assert.ok(!JSON.stringify(out).includes('private diagnostic'));
  });
}
test('AI-review self-context cannot claim a different App', async () => {
  const f = fixture(); f.required.push({context: 'ai-review', app: {databaseId: 54321}});
  assert.notEqual((await run(f)).risk, 'success');
});
test('Agent can clear AI-service source after review and checks', async () => {
  const f = fixture(); f.files[0].filename = 'src/AIService/run.js';
  assert.equal((await run(f)).risk, 'success'); f.pr.labels = [];
  assert.notEqual((await run(f)).risk, 'success');
});

test('Untrusted receipt status never escapes into published descriptions', async () => {
  const f = fixture(); f.comments[0].body = marker({status: 'fixture-private-diagnostic'});
  const out = await run(f); assert.notEqual(out.risk, 'success');
  assert.ok(!JSON.stringify(out).includes('fixture-private-diagnostic'));
});
