import assert from 'node:assert/strict';
import test from 'node:test';
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
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events'}},hasDotSubscription:false}),[false,'no_subscription']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events'}},hasDotSubscription:true}),[true,'ready']);
  assert.deepEqual(providerReadiness('chatgpt-plan',{
    env:{},configuredProviders:{'chatgpt-plan':{type:'chatgpt_plan_oauth',plan_usage_authorized:true,token_env:'AI_SERVICE_CHATGPT_PLAN_TOKEN'}},
  }),[false,'missing_credential']);
  assert.deepEqual(providerReadiness('muse',{configuredProviders:{muse:{type:'muse',enabled:false}}}),[false,'unavailable']);
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
