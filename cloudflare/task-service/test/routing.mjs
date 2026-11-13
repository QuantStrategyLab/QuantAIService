import assert from 'node:assert/strict';
import test from 'node:test';
import {decideRoute,maskProject,providerReadiness,resolveComplexity,validateRoutingConfig} from '../src/routing.mjs';

const TIERS={
  simple:{dot:{model:'dot-fast',reasoning_effort:'low'},grokbot:{model:'grokbot-fast',reasoning_effort:'low'},'gemini-text':{model:'gemini-flash',reasoning_effort:'low'}},
  standard:{dot:{model:'dot-default',reasoning_effort:'medium'},grokbot:{model:'grokbot-default',reasoning_effort:'medium'},'gemini-text':{model:'gemini-pro',reasoning_effort:'medium'}},
  complex:{dot:{model:'dot-strong',reasoning_effort:'high'},grokbot:{model:'grokbot-strong',reasoning_effort:'high'},'gemini-text':{model:'gemini-pro',reasoning_effort:'high'}},
};
const ROUTING={
  schema_version:'task.routing.v1',
  categories:{
    media:{default_complexity:'standard'},
    interactive_text:{default_complexity:'simple'},
    short_api:{default_complexity:'simple'},
  },
  scenarios:{
    example_media:{category:'media',mode:'agent',providers:['dot','grokbot'],matching:{hints:['image'],min_with_image:'standard'}},
    example_support_text:{category:'interactive_text',mode:'agent',providers:['grokbot','dot'],matching:{hints:['support']}},
    example_short_api:{category:'short_api',mode:'api',providers:['gemini-text'],matching:{hints:['qa']}},
  },
  complexity_tiers:TIERS,
};

test('validateRoutingConfig rejects empty preference lists and unknown categories',()=>{
  assert.throws(()=>validateRoutingConfig({scenarios:{example_short_api:{mode:'api',providers:[]}},complexity_tiers:TIERS}));
  assert.throws(()=>validateRoutingConfig({scenarios:{orphan:{category:'missing',mode:'agent',providers:['dot']}},complexity_tiers:TIERS}));
});

test('resolveComplexity uses registry defaults then heuristics',()=>{
  // Explicit complexity is a floor: image/size heuristics may still raise.
  assert.equal(resolveComplexity({complexity:'simple',objective:'x'.repeat(80000),materials:[{type:'image'}]},'example_media',ROUTING),'complex');
  assert.equal(resolveComplexity({objective:'review',materials:[{text:'y'.repeat(60000)}]},'example_media',ROUTING),'complex');
  assert.equal(resolveComplexity({objective:'hi',materials:[]},'example_short_api',ROUTING),'simple');
  // Multilingual size floors use UTF-8 bytes, not JS string length.
  assert.equal(resolveComplexity({objective:'研'.repeat(20000),materials:[]},'example_support_text',ROUTING),'complex');
  // Explicit min_with_image wins over the image-size fallback.
  assert.equal(resolveComplexity({
    objective:'x'.repeat(25000),materials:[{type:'image'}],
  },'example_media',ROUTING),'standard');
});

test('decideRoute skips API providers whose fixed model mismatches the tier',()=>{
  assert.throws(()=>decideRoute(ROUTING,{mode:'api',scenario:'example_short_api',objective:'ping',materials:[],output_schema:{}},{
    project:'example-org/ExampleApi',
    providerReady:()=>[true,'ready'],
    entryReady:()=>[false,'api_model_mismatch'],
    log:{info(){}},
  }),/no ready provider for scenario/);
});

test('decideRoute skips unready providers and accepts config-only scenario additions',()=>{
  const decision=decideRoute(ROUTING,{mode:'agent',scenario:'example_support_text',objective:'answer the caller',materials:[],output_schema:{}},{
    project:'example-org/ExampleSupport',
    providerReady:name=>name==='dot'?[true,'ready']:[false,'grok_not_ready'],
    log:{info(){}},
  });
  assert.equal(decision.provider,'dot');
  assert.equal(decision.category,'interactive_text');
  assert.equal(decision.model,'dot-fast');
  assert.match(decision.reason,/skip:grokbot:grok_not_ready/);
  const extended=validateRoutingConfig({
    ...ROUTING,
    scenarios:{...ROUTING.scenarios,example_new:{category:'media',mode:'agent',providers:['dot'],matching:{default_complexity:'complex'}}},
  });
  const created=decideRoute(extended,{mode:'agent',scenario:'example_new',objective:'batch',materials:[],output_schema:{}},{
    project:'example-org/ExampleProject',providerReady:()=>[true,'ready'],log:{info(){}},
  });
  assert.equal(created.tier,'complex');
  assert.throws(()=>decideRoute(ROUTING,{mode:'agent',scenario:'example_media',objective:'draw',materials:[],output_schema:{}},{
    project:'example-org/ExampleMedia',providerReady:()=>[false,'not_ready'],log:{info(){}},
  }));
});

test('maskProject and readiness gates',()=>{
  const masked=maskProject('ExampleOrg/ExampleRepo');
  assert.equal(masked.includes('ExampleOrg'),false);
  assert.equal(masked.includes('ExampleRepo'),false);
  assert.deepEqual(providerReadiness('grokbot',{env:{},configuredProviders:{grokbot:{type:'grok_webhook',enabled:true,key_env:'AI_SERVICE_GROKBOT_WEBHOOK_KEY'}}}),[false,'grok_not_ready']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events'}},hasDotSubscription:false}),[false,'no_subscription']);
  assert.deepEqual(providerReadiness('dot',{configuredProviders:{dot:{type:'dot_events'}},hasDotSubscription:true}),[true,'ready']);
});


test('null schema version rejects and null complexity behaves as omitted',()=>{
  assert.throws(()=>validateRoutingConfig({...ROUTING,schema_version:null}),/unsupported routing schema_version/);
  assert.equal(resolveComplexity({complexity:null},'example_support_text',ROUTING),
    resolveComplexity({},'example_support_text',ROUTING));
});

test('policy validation rejects unadmitted effective scenario tier models',()=>{
  const models=Object.fromEntries(Object.keys(TIERS.simple).map(provider=>
    [provider,[...new Set(Object.values(TIERS).map(tier=>tier[provider].model))]]));
  validateRoutingConfig(ROUTING,models);
  assert.throws(()=>validateRoutingConfig(ROUTING,{...models,dot:['dot-fast','dot-default']}),/scenario tier model is not admitted/);
  const overrides=structuredClone(ROUTING);
  overrides.scenarios.example_short_api.tier_overrides={simple:{'gemini-text':{model:'synthetic-unadmitted',reasoning_effort:'low'}}};
  assert.throws(()=>validateRoutingConfig(overrides,models),/scenario tier model is not admitted/);
});
