import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {apiConfiguration,apiModelCandidates,executeAPI,selectAPIModel} from '../src/api.mjs';
import {valueOf} from '../src/canonical.mjs';
import {decideRoute,maskProject,providerReadiness,resolveComplexity,validateRoutingConfig} from '../src/routing.mjs';

// Quant-shaped fixtures (not Personal example_* / gemini-text catalogs).
const TIERS={
  simple:{
    muse:{model:'muse-fast',reasoning_effort:'low'},
    dot:{model:'dot-fast',reasoning_effort:'low'},
    grokbot:{model:'grokbot-fast',reasoning_effort:'low'},
    'chatgpt-plan':{model:'chatgpt-plan-fast',reasoning_effort:'low'},
    gemini:{model:'gemini-flash',reasoning_effort:'low'},
  },
  standard:{
    muse:{model:'muse-default',reasoning_effort:'medium'},
    dot:{model:'dot-default',reasoning_effort:'medium'},
    grokbot:{model:'grokbot-default',reasoning_effort:'medium'},
    'chatgpt-plan':{model:'chatgpt-plan',reasoning_effort:'medium'},
    gemini:{model:'gemini-pro',reasoning_effort:'medium'},
  },
  complex:{
    muse:{model:'muse-strong',reasoning_effort:'high'},
    dot:{model:'dot-strong',reasoning_effort:'high'},
    grokbot:{model:'grokbot-strong',reasoning_effort:'high'},
    'chatgpt-plan':{model:'chatgpt-plan-strong',reasoning_effort:'high'},
    gemini:{model:'gemini-pro',reasoning_effort:'high'},
  },
};
const ROUTING={
  schema_version:'task.routing.v1',
  categories:{
    quant_review:{default_complexity:'complex'},
    quant_ops:{default_complexity:'standard'},
    quant_signals:{default_complexity:'standard'},
    quant_notify:{default_complexity:'simple'},
  },
  scenarios:{
    pr_code_review:{
      category:'quant_review',mode:'agent',providers:['muse','grokbot','dot'],
      matching:{hints:['pull-request','code-review'],min_with_image:'standard',default_complexity:'complex'},
    },
    reconciliation_anomaly:{
      category:'quant_ops',mode:'agent',providers:['muse','grokbot','dot'],
      matching:{hints:['recon-diff'],default_complexity:'complex'},
    },
    signal_context:{
      category:'quant_signals',mode:'agent',providers:['muse','grokbot','dot'],
      matching:{hints:['signal-context'],min_with_image:'standard',default_complexity:'standard'},
    },
    quick_notification:{
      category:'quant_notify',mode:'api',providers:['chatgpt-plan'],
      matching:{hints:['short-alert','status-ping'],default_complexity:'simple'},
    },
  },
  complexity_tiers:TIERS,
};

test('validateRoutingConfig rejects empty preference lists and unknown categories',()=>{
  assert.throws(()=>validateRoutingConfig({scenarios:{quick_notification:{mode:'api',providers:[]}},complexity_tiers:TIERS}));
  assert.throws(()=>validateRoutingConfig({scenarios:{orphan:{category:'missing',mode:'agent',providers:['dot']}},complexity_tiers:TIERS}));
});

test('resolveComplexity uses registry defaults then heuristics',()=>{
  // Explicit complexity is a floor: image/size heuristics may still raise.
  assert.equal(resolveComplexity({complexity:'simple',objective:'x'.repeat(80000),materials:[{type:'image'}]},'signal_context',ROUTING),'complex');
  assert.equal(resolveComplexity({objective:'review',materials:[{text:'y'.repeat(60000)}]},'signal_context',ROUTING),'complex');
  assert.equal(resolveComplexity({objective:'hi',materials:[]},'quick_notification',ROUTING),'simple');
  // Multilingual size floors use UTF-8 bytes, not JS string length.
  assert.equal(resolveComplexity({objective:'研'.repeat(20000),materials:[]},'reconciliation_anomaly',ROUTING),'complex');
  // Explicit min_with_image wins over the image-size fallback.
  assert.equal(resolveComplexity({
    objective:'x'.repeat(25000),materials:[{type:'image'}],
  },'signal_context',ROUTING),'standard');
});

test('decideRoute skips API providers whose fixed model mismatches the tier',()=>{
  assert.throws(()=>decideRoute(ROUTING,{mode:'api',scenario:'quick_notification',objective:'ping',materials:[],output_schema:{}},{
    project:'example-org/ExampleResearch',
    providerReady:()=>[true,'ready'],
    entryReady:()=>[false,'api_model_mismatch'],
    log:{info(){}},
  }),/no ready provider for scenario/);
});

test('decideRoute skips unready providers and accepts config-only scenario additions',()=>{
  const decision=decideRoute(ROUTING,{mode:'agent',scenario:'reconciliation_anomaly',objective:'explain the recon diff',materials:[],output_schema:{}},{
    project:'example-org/ExampleResearch',
    providerReady:name=>name==='dot'?[true,'ready']:[false,name==='muse'?'unavailable':'grok_not_ready'],
    log:{info(){}},
  });
  assert.equal(decision.provider,'dot');
  assert.equal(decision.category,'quant_ops');
  assert.equal(decision.model,'dot-strong');
  assert.match(decision.reason,/skip:muse:unavailable/);
  assert.match(decision.reason,/skip:grokbot:grok_not_ready/);
  const extended=validateRoutingConfig({
    ...ROUTING,
    scenarios:{...ROUTING.scenarios,ops_note:{category:'quant_ops',mode:'agent',providers:['dot'],matching:{default_complexity:'standard'}}},
  });
  const created=decideRoute(extended,{mode:'agent',scenario:'ops_note',objective:'batch',materials:[],output_schema:{}},{
    project:'example-org/ExampleResearch',providerReady:()=>[true,'ready'],log:{info(){}},
  });
  assert.equal(created.tier,'standard');
  assert.throws(()=>decideRoute(ROUTING,{mode:'agent',scenario:'pr_code_review',objective:'review',materials:[],output_schema:{}},{
    project:'example-org/ExampleResearch',providerReady:()=>[false,'not_ready'],log:{info(){}},
  }));
});

test('maskProject and readiness gates',()=>{
  const masked=maskProject('ExampleOrg/ExampleRepo');
  assert.equal(masked.includes('ExampleOrg'),false);
  assert.equal(masked.includes('ExampleRepo'),false);
  assert.deepEqual(providerReadiness('grokbot',{env:{},configuredProviders:{grokbot:{type:'grok_webhook',enabled:true,key_env:'AI_SERVICE_GROKBOT_WEBHOOK_KEY'}}}),[false,'grok_not_ready']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events'}},hasDotSubscription:true}),[false,'disabled']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events',enabled:false}},hasDotSubscription:true}),[false,'disabled']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events',enabled:true}},hasDotSubscription:false}),[false,'no_subscription']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events',enabled:true}},hasDotSubscription:true}),[true,'ready']);
  assert.deepEqual(providerReadiness('openai',{env:{},configuredProviders:{openai:{type:'openai_api',enabled:true,api_usage_authorized:true,key_env:'OPENAI_KEY'}}}),[false,'missing_credential']);
  assert.deepEqual(providerReadiness('openai',{env:{OPENAI_KEY:'k'},configuredProviders:{openai:{type:'openai_api',enabled:true,api_usage_authorized:true,key_env:'OPENAI_KEY'}}}),[true,'ready']);
  assert.deepEqual(providerReadiness('claude',{env:{CLAUDE_KEY:'k'},configuredProviders:{claude:{type:'anthropic_api',enabled:true,api_usage_authorized:true,key_env:'CLAUDE_KEY'}}}),[true,'ready']);
  assert.deepEqual(providerReadiness('chatgpt-plan',{
    env:{},configuredProviders:{'chatgpt-plan':{type:'chatgpt_plan_oauth',plan_usage_authorized:true,token_env:'AI_SERVICE_CHATGPT_PLAN_TOKEN'}},
  }),[false,'missing_credential']);
  assert.deepEqual(providerReadiness('muse',{configuredProviders:{muse:{type:'muse',enabled:false}}}),[false,'disabled']);
  assert.deepEqual(providerReadiness('muse',{configuredProviders:{muse:{type:'muse',enabled:true}}}),[false,'no_subscription']);
  assert.deepEqual(providerReadiness('muse',{hasMuseSubscription:true,configuredProviders:{muse:{type:'muse',enabled:true}}}),[true,'ready']);
});

test('null schema version rejects and null complexity behaves as omitted',()=>{
  assert.throws(()=>validateRoutingConfig({...ROUTING,schema_version:null}),/unsupported routing schema_version/);
  assert.equal(resolveComplexity({complexity:null},'reconciliation_anomaly',ROUTING),
    resolveComplexity({},'reconciliation_anomaly',ROUTING));
});

test('policy validation rejects unadmitted effective scenario tier models',()=>{
  const models=Object.fromEntries(Object.keys(TIERS.simple).map(provider=>
    [provider,[...new Set(Object.values(TIERS).map(tier=>tier[provider].model))]]));
  validateRoutingConfig(ROUTING,models);
  assert.throws(()=>validateRoutingConfig(ROUTING,{...models,dot:['dot-fast','dot-default']}),/scenario tier model is not admitted/);
  const overrides=structuredClone(ROUTING);
  overrides.scenarios.quick_notification.tier_overrides={simple:{'chatgpt-plan':{model:'synthetic-unadmitted',reasoning_effort:'low'}}};
  assert.throws(()=>validateRoutingConfig(overrides,models),/scenario tier model is not admitted/);
});

test('validateRoutingConfig rejects inverted effective size thresholds',()=>{
  const bad=structuredClone(ROUTING);
  bad.scenarios.signal_context.matching={...bad.scenarios.signal_context.matching,size_thresholds:{standard:60000,complex:4000}};
  assert.throws(()=>validateRoutingConfig(bad),/effective size_thresholds must keep standard < complex/);
});

test('validateRoutingConfig requires complete tier coverage and project provider admission',()=>{
  const incomplete=structuredClone(ROUTING);
  delete incomplete.complexity_tiers.complex.dot;
  assert.throws(()=>validateRoutingConfig(incomplete),/scenario tier mapping is incomplete/);
  const models=Object.fromEntries(Object.keys(TIERS.simple).map(provider=>
    [provider,[...new Set(Object.values(TIERS).map(tier=>tier[provider].model))]]));
  assert.throws(
    ()=>validateRoutingConfig(ROUTING,models,{agent:'dot'}),
    /scenario provider is not admitted/,
  );
  validateRoutingConfig(ROUTING,models,{
    agent:'dot','agent:grokbot':'grokbot','agent:muse':'muse',api:'chatgpt-plan','api:gemini':'gemini',
  });
});

test('decideRoute checks entryReady before rejecting a requested model',()=>{
  assert.throws(()=>decideRoute(ROUTING,{
    mode:'api',scenario:'quick_notification',objective:'ping',materials:[],output_schema:{},
    model:'chatgpt-plan-fast',
  },{
    project:'example-org/ExampleResearch',
    providerReady:()=>[true,'ready'],
    entryReady:()=>[false,'api_model_mismatch'],
    log:{info(){}},
  }),/no ready provider for scenario/);
});

test('quick_notification stays single-provider chatgpt-plan without gemini fallback',()=>{
  const decision=decideRoute(ROUTING,{
    mode:'api',scenario:'quick_notification',objective:'status ping',materials:[],output_schema:{},
  },{
    project:'example-org/ExampleResearch',
    providerReady:()=>[true,'ready'],
    entryReady:()=>[true,'ready'],
    log:{info(){}},
  });
  assert.equal(decision.provider,'chatgpt-plan');
  assert.equal(decision.model,'chatgpt-plan-fast');
  assert.equal(decision.tier,'simple');
  assert.equal(ROUTING.scenarios.quick_notification.providers.length,1);
});


const complexityCases = JSON.parse(readFileSync(new URL('../../../tests/fixtures/routing-complexity-cases.json', import.meta.url)));
for (const sample of complexityCases) {
  test(sample.name, () => {
    const routing = {...ROUTING, scenarios: {
      shared_probe: {mode: 'agent', providers: ['muse'], matching: sample.matching},
    }};
    const request = {complexity: sample.requested, objective: sample.text.repeat(sample.repeat), materials: sample.materials};
    assert.equal(resolveComplexity(request, 'shared_probe', routing), sample.expected);
  });
}


for (const [model, effort, thinkingConfig] of [
  ['gemini-2.5-pro', 'low', {thinkingBudget: 128}],
  ['gemini-2.5-flash-lite', 'medium', {thinkingBudget: 1024}],
  ['gemini-3.5-flash-lite', 'medium', {thinkingLevel: 'MEDIUM'}],
  ['gemini-3-pro-preview', 'medium', {thinkingLevel: 'LOW'}],
]) test(`routed API verifies the model and reasoning setting for ${model}`, async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body).generationConfig.thinkingConfig, thinkingConfig);
    return Response.json({modelVersion: model, candidates: [
      {finishReason: 'STOP', content: {parts: [{text: '{"ok":true}', thoughtSignature: 'c3ludGhldGlj'}]}},
    ]});
  });
  const env = {API_PROVIDERS: {api: {
    type: 'gemini_api', enabled: true, api_usage_authorized: true, model, key_env: 'TEST_KEY',
  }}, TEST_KEY: 'synthetic-only'};
  const receipt = await executeAPI(env, 'api', {
    model, reasoning_effort: effort, objective: 'Synthetic test', output_schema: {type: 'object'},
  }, 10);
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(receipt.model_verification, 'provider_reported');
  assert.equal(receipt.status, 'completed');
  assert.equal(JSON.stringify(receipt).includes('thoughtSignature'), false);
});

test('available model selection stays inside the project, provider and complexity tier', async (t) => {
  const item = {type: 'gemini_api', enabled: true, api_usage_authorized: true, model: 'old',
    key_env: 'TEST_KEY', tier_models: {simple: ['unapproved', 'old', 'image', 'current'], complex: ['strong']}};
  const env = {API_PROVIDERS: {api: item}, TEST_KEY: 'synthetic-only'};
  const decision = {model: 'old', tier: 'simple'};
  const models = ['old', 'image', 'current', 'strong'];
  const visited = [];
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.redirect, 'manual');
    assert.equal(options.headers['x-goog-api-key'], env.TEST_KEY);
    const id = new URL(url).pathname.split('/').at(-1);
    visited.push(id);
    if (id === 'old') return new Response(null, {status: 404});
    return Response.json({name: 'models/' + id, supportedGenerationMethods: id === 'image' ? [] : ['generateContent']});
  });
  assert.equal(await selectAPIModel(env, 'api', {}, decision, models), 'current');
  assert.deepEqual(visited, ['old', 'image', 'current']);
  visited.length = 0;
  assert.equal(await selectAPIModel(env, 'api', {}, {...decision, model: 'current'}, models), 'current');
  assert.deepEqual(visited, ['current']);
  await assert.rejects(selectAPIModel(env, 'api', {model: 'old'}, decision, models));
  fetch.mock.mockImplementation(() => new Response(null, {status: 403}));
  const count = fetch.mock.callCount();
  await assert.rejects(selectAPIModel(env, 'api', {}, decision, models));
  assert.equal(fetch.mock.callCount(), count + 1);
});

test('explicit API pins allow approved replacements after rotation', async (t) => {
  const name = 'gemini';
  const item = {type: 'gemini_api', enabled: true, api_usage_authorized: true,
    model: 'current', key_env: 'KEY', tier_models: {simple: ['unapproved', 'current']}};
  const env = {API_PROVIDERS: {[name]: item}, KEY: 'synthetic'};
  const admitted = ['retired', 'current'];
  const routing = {...ROUTING, scenarios: {...ROUTING.scenarios, rotation: {
    mode: 'api', providers: [name], matching: {default_complexity: 'simple'},
    tier_overrides: {simple: {[name]: {model: 'retired', reasoning_effort: 'low'}}},
  }}};
  t.mock.method(globalThis, 'fetch', async () =>
    Response.json({name: 'models/current', supportedGenerationMethods: ['generateContent']}));
  for (const pin of [{}, {model: 'current'}]) {
    const request = {mode: 'api', scenario: 'rotation', ...pin};
    const decision = decideRoute(routing, request, {
      providerReady: () => [true, 'ready'],
      entryReady(provider, entry, tier) {
        const models = apiModelCandidates(env, provider, request, {model: entry.model, tier}, admitted);
        return [true, 'ready', apiConfiguration(env, provider, {model: models[0], complexity: tier}).model];
      },
      log: {info(){}},
    });
    assert.equal(await selectAPIModel(env, name, request, decision, admitted), 'current');
  }
  assert.deepEqual(apiModelCandidates(env, name, {model: 'retired'}, {model: 'retired', tier: 'simple'}, admitted), []);
});

const PNG_1X1='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jrN8AAAAASUVORK5CYII=';

test('executeAPI supports openai_api and anthropic_api with relaxed bounds', async (t) => {
  const seen = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    seen.push({url, headers: options.headers, body: JSON.parse(options.body)});
    if (url.includes('openai.com')) {
      return Response.json({
        id: 'resp_1', status: 'completed', model: 'gpt-5-mini-test',
        output: [{type: 'message', content: [{type: 'output_text', text: '{"summary":"ok"}'}]}],
        usage: {input_tokens: 10, output_tokens: 4, total_tokens: 14},
      });
    }
    return Response.json({
      model: 'claude-sonnet-test',
      stop_reason: 'tool_use',
      content: [{type: 'tool_use', id: 'toolu_1', name: 'record_result', input: {translated: 'bonjour'}}],
      usage: {input_tokens: 9, output_tokens: 5},
    });
  });
  const env = {
    API_PROVIDERS: {
      openai: {
        type: 'openai_api', enabled: true, api_usage_authorized: true,
        model: 'gpt-5-mini-test', key_env: 'OPENAI_KEY', max_output_tokens: 32768, timeout_seconds: 180,
      },
      claude: {
        type: 'anthropic_api', enabled: true, api_usage_authorized: true,
        model: 'claude-sonnet-test', key_env: 'ANTHROPIC_KEY', max_output_tokens: 32768, timeout_seconds: 180,
      },
    },
    OPENAI_KEY: 'synthetic-openai',
    ANTHROPIC_KEY: 'synthetic-anthropic',
  };
  const oSchema = {type: 'object', properties: {summary: {type: 'string'}}, required: ['summary'], additionalProperties: false};
  const aSchema = {type: 'object', properties: {translated: {type: 'string'}}, required: ['translated'], additionalProperties: false};

  const oRes = await executeAPI(env, 'openai', {
    model: 'gpt-5-mini-test', objective: 'Audit', reasoning_effort: 'medium',
    materials: [{type: 'image', mime_type: 'image/png', base64: PNG_1X1}], output_schema: oSchema,
  }, 180);
  assert.equal(oRes.status, 'completed');
  assert.deepEqual(valueOf(oRes.output), {summary: 'ok'});
  assert.equal(seen[0].body.store, false);
  assert.equal(seen[0].body.input[0].content[1].type, 'input_image');

  const aRes = await executeAPI(env, 'claude', {
    model: 'claude-sonnet-test', objective: 'Translate', reasoning_effort: 'high',
    materials: [{type: 'image', mime_type: 'image/png', base64: PNG_1X1}], output_schema: aSchema,
  }, 180);
  assert.equal(aRes.status, 'completed');
  assert.deepEqual(valueOf(aRes.output), {translated: 'bonjour'});
  assert.equal(seen[1].headers['anthropic-version'], '2023-06-01');
  assert.deepEqual(seen[1].body.output_config, {effort: 'high'});
  assert.equal(seen[1].body.messages[0].content[1].type, 'image');

  await assert.rejects(() => executeAPI(env, 'claude', {
    model: 'claude-sonnet-test', objective: 'Translate', reasoning_effort: 'xhigh', output_schema: aSchema,
  }, 60), /inference rejected/);
});

test('executeAPI preserves non-object Gemini JSON outputs permitted by output_schema', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => Response.json({
    modelVersion: 'gemini-2.5-flash',
    candidates: [{finishReason: 'STOP', content: {parts: [{text: '[1,2]'}]}}],
  }));
  const env = {API_PROVIDERS: {gemini: {
    type: 'gemini_api', enabled: true, api_usage_authorized: true, model: 'gemini-2.5-flash', key_env: 'GEMINI_KEY',
  }}, GEMINI_KEY: 'synthetic-gemini'};
  const res = await executeAPI(env, 'gemini', {
    model: 'gemini-2.5-flash', objective: 'List', output_schema: {type: 'array', items: {type: 'integer'}},
  }, 60);
  assert.equal(res.status, 'completed');
  assert.deepEqual(valueOf(res.output), [1, 2]);
});
