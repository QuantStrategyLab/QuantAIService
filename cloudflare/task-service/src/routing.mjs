// Abstract scenario routing registry mirrored from ai_service.routing.
// Scenarios and categories are config data; this file never hard-codes business ids.
const SCHEMA_VERSION='task.routing.v1';
const TIERS=new Set(['simple','standard','complex']);
const EFFORTS=new Set(['low','medium','high']);
const PROVIDER=/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const REGISTRY_ID=/^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const TIER_RANK={simple:0,standard:1,complex:2};
const DEFAULT_HEURISTICS={size_simple_max:1500,size_standard:4000,size_complex:50000,image_complex_size:20000,materials_complex_count:8};
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const fail=message=>{throw Object.assign(new Error(message),{name:'RoutingError'});};
const requireId=(value,label)=>{if(typeof value!=='string'||!REGISTRY_ID.test(value))fail(`invalid ${label}`);return value;};
const raiseTier=(current,floor)=>floor&&TIER_RANK[floor]>TIER_RANK[current]?floor:current;

export function maskProject(project){
  if(typeof project!=='string'||!project.includes('/'))return '[redacted]';
  return project.split('/').map(segment=>{
    if(!segment)return '***';
    if(segment.length<=2)return '*'.repeat(segment.length);
    return segment[0]+'*'.repeat(segment.length-2)+segment.at(-1);
  }).join('/');
}

function validateTier(tierName,mapping){
  if(!TIERS.has(tierName)||!object(mapping)||!Object.keys(mapping).length)fail('invalid complexity tier mapping');
  for(const [provider,entry] of Object.entries(mapping)){
    if(!PROVIDER.test(provider)||!object(entry)||Object.keys(entry).sort().join(',')!=='model,reasoning_effort'||
       typeof entry.model!=='string'||!entry.model||!EFFORTS.has(entry.reasoning_effort)){
      fail('tier entry requires model and reasoning_effort');
    }
  }
}

function validateMatching(matching){
  if(matching==null)return {};
  if(!object(matching))fail('matching must be an object');
  if(Object.keys(matching).some(k=>!['hints','default_complexity','min_with_image','size_thresholds'].includes(k)))fail('unknown matching fields');
  const hints=matching.hints??[];
  if(!Array.isArray(hints)||hints.some(item=>typeof item!=='string'||!item||item.length>64))fail('matching.hints must be short strings');
  for(const key of ['default_complexity','min_with_image']){
    if(Object.hasOwn(matching,key)&&!TIERS.has(matching[key]))fail(`${key} must be a complexity tier`);
  }
  const thresholds=matching.size_thresholds??{};
  if(!object(thresholds)||Object.keys(thresholds).some(k=>!['standard','complex'].includes(k)))fail('size_thresholds may only set standard and complex');
  for(const value of Object.values(thresholds)){
    if(!Number.isSafeInteger(value)||value<1)fail('size_thresholds values must be positive integers');
  }
  return matching;
}

function validateEffectiveThresholds(matching,heuristics){
  const thresholds=matching?.size_thresholds??{};
  const standard=thresholds.standard??heuristics.size_standard;
  const complex=thresholds.complex??heuristics.size_complex;
  if(!(standard<complex))fail('effective size_thresholds must keep standard < complex');
}

function validateHeuristics(heuristics){
  if(heuristics==null)return {...DEFAULT_HEURISTICS};
  if(!object(heuristics)||Object.keys(heuristics).some(k=>!Object.hasOwn(DEFAULT_HEURISTICS,k)))fail('unknown heuristics fields');
  const merged={...DEFAULT_HEURISTICS,...heuristics};
  for(const value of Object.values(merged)){
    if(!Number.isSafeInteger(value)||value<1)fail('heuristics values must be positive integers');
  }
  if(!(merged.size_simple_max<merged.size_standard&&merged.size_standard<merged.size_complex))fail('heuristics size thresholds must increase');
  return merged;
}

export function validateRoutingConfig(routing,models,providers){
  if(routing==null)return {};
  if(!object(routing))fail('routing must be an object');
  if(Object.keys(routing).some(k=>!['schema_version','categories','scenarios','complexity_tiers','heuristics'].includes(k)))fail('unknown routing fields');
  if((Object.hasOwn(routing,'schema_version')?routing.schema_version:SCHEMA_VERSION)!==SCHEMA_VERSION)fail('unsupported routing schema_version');
  const categories=routing.categories??{},scenarios=routing.scenarios??{},tiers=routing.complexity_tiers??{};
  if(!object(categories)||!object(scenarios)||!object(tiers))fail('routing maps must be objects');
  if(!Object.keys(scenarios).length)fail('routing.scenarios is required when routing is set');
  if([...TIERS].some(name=>!Object.hasOwn(tiers,name))||Object.keys(tiers).length!==3)fail('complexity_tiers must define simple, standard and complex');
  for(const [name,category] of Object.entries(categories)){
    requireId(name,'category id');
    if(!object(category)||Object.keys(category).some(k=>!['parent','default_complexity'].includes(k)))fail('unknown category fields');
    if(category.parent!=null){requireId(category.parent,'category parent');if(category.parent===name)fail('category cannot parent itself');}
    if(Object.hasOwn(category,'default_complexity')&&!TIERS.has(category.default_complexity))fail('category default_complexity must be a complexity tier');
  }
  for(const [name,category] of Object.entries(categories)){
    let parent=category.parent;const seen=new Set([name]);
    while(parent!=null){
      if(!Object.hasOwn(categories,parent))fail('category parent is not registered');
      if(seen.has(parent))fail('category parent cycle');
      seen.add(parent);parent=categories[parent].parent;
    }
  }
  for(const [name,scenario] of Object.entries(scenarios)){
    requireId(name,'scenario id');
    if(!object(scenario)||!Object.hasOwn(scenario,'mode')||!Object.hasOwn(scenario,'providers'))fail('scenario requires mode and providers');
    if(Object.keys(scenario).some(k=>!['category','mode','providers','matching','tier_overrides'].includes(k)))fail('unknown scenario fields');
    if(scenario.category!=null){requireId(scenario.category,'scenario category');if(!Object.hasOwn(categories,scenario.category))fail('scenario category is not registered');}
    if(!['api','agent'].includes(scenario.mode)||!Array.isArray(scenario.providers)||!scenario.providers.length)fail('scenario providers must be a nonempty ordered list');
    if(new Set(scenario.providers).size!==scenario.providers.length)fail('scenario providers must be unique');
    if(scenario.providers.some(p=>typeof p!=='string'||!PROVIDER.test(p)))fail('invalid provider preference');
    validateMatching(scenario.matching);
    const overrides=scenario.tier_overrides??{};
    if(!object(overrides)||Object.keys(overrides).some(k=>!TIERS.has(k)))fail('tier_overrides may only override known tiers');
    for(const [tierName,mapping] of Object.entries(overrides))validateTier(tierName,mapping);
  }
  for(const [tierName,mapping] of Object.entries(tiers))validateTier(tierName,mapping);
  for(const scenario of Object.values(scenarios)){
    for(const [tier,mapping] of Object.entries(tiers)){
      for(const provider of scenario.providers){
        const entry=scenario.tier_overrides?.[tier]?.[provider]??mapping[provider];
        if(!entry)fail('scenario tier mapping is incomplete');
        if(models!==undefined&&!models[provider]?.includes(entry.model))fail('scenario tier model is not admitted');
      }
    }
  }
  if(providers!==undefined){
    if(!object(providers))fail('project providers must be an object');
    const admitted=new Set(Object.values(providers));
    for(const scenario of Object.values(scenarios)){
      for(const provider of scenario.providers){
        if(!admitted.has(provider))fail('scenario provider is not admitted');
      }
    }
  }
  const heuristics=validateHeuristics(routing.heuristics);
  for(const scenario of Object.values(scenarios))validateEffectiveThresholds(scenario.matching,heuristics);
  return {schema_version:SCHEMA_VERSION,categories,scenarios,complexity_tiers:tiers,heuristics};
}

function categoryDefault(routing,categoryId){
  let current=categoryId;
  while(current!=null){
    const category=routing.categories?.[current];
    if(!category)break;
    if(Object.hasOwn(category,'default_complexity'))return category.default_complexity;
    current=category.parent??null;
  }
  return 'standard';
}

export function resolveComplexity(request,scenarioId,routing={}){
  routing=Object.keys(routing||{}).length?validateRoutingConfig(routing):{};
  const scenario=routing.scenarios?.[scenarioId]??{};
  const matching=scenario.matching??{};
  const heuristics=routing.heuristics??{...DEFAULT_HEURISTICS};
  const materials=Array.isArray(request.materials)?request.materials:[];
  // Size floors are UTF-8 bytes (same unit as the Python material_bytes helper).
  const size=new TextEncoder().encode(JSON.stringify({objective:request.objective??'',materials})).length;
  const hasImage=materials.some(item=>object(item)&&(item.type==='image'||Array.isArray(item.images)||
    (typeof item.mime_type==='string'&&item.mime_type.startsWith('image/'))));
  const thresholds=matching.size_thresholds??{};
  const sizeStandard=thresholds.standard??heuristics.size_standard;
  const sizeComplex=thresholds.complex??heuristics.size_complex;
  let tier;
  if(request.complexity!=null){
    if(!TIERS.has(request.complexity))fail('complexity must be simple, standard or complex');
    tier=request.complexity;
  }else{
    tier=matching.default_complexity??categoryDefault(routing,scenario.category);
  }
  // Match the shared Python helper: min_with_image wins over the image-size
  // fallback; overall size and material-count floors stay independent.
  if(hasImage){
    const floor=Object.hasOwn(matching,'min_with_image')?matching.min_with_image:
      (size>=heuristics.image_complex_size?'complex':null);
    if(floor)tier=raiseTier(tier,floor);
  }
  if(materials.length>=heuristics.materials_complex_count)tier=raiseTier(tier,'complex');
  if(size>=sizeComplex)tier=raiseTier(tier,'complex');
  else if(size>=sizeStandard)tier=raiseTier(tier,'standard');
  return tier;
}

function tierEntry(routing,scenarioId,tier,provider){
  const override=routing.scenarios[scenarioId].tier_overrides?.[tier]?.[provider];
  if(override)return override;
  const entry=routing.complexity_tiers?.[tier]?.[provider];
  if(!entry)fail('no tier mapping for provider');
  return entry;
}

export function decideRoute(routing,request,{project,providerReady,entryReady,log}={}){
  routing=validateRoutingConfig(routing);
  const scenarioId=request.scenario;
  if(typeof scenarioId!=='string'||!Object.hasOwn(routing.scenarios,scenarioId))fail('scenario is not configured for this project');
  const configured=routing.scenarios[scenarioId];
  if(request.mode!==configured.mode)fail('request mode does not match scenario mode');
  const tier=resolveComplexity(request,scenarioId,routing);
  const category=configured.category??null;
  const skips=[];
  for(const provider of configured.providers){
    const [ready,detail]=providerReady(provider);
    if(!ready){skips.push(`skip:${provider}:${detail}`);continue;}
    let entry;
    try{entry=tierEntry(routing,scenarioId,tier,provider);}
    catch(e){skips.push(`skip:${provider}:${e.message}`);continue;}
    if(typeof entryReady==='function'){
      const [ok,why]=entryReady(provider,entry);
      if(!ok){skips.push(`skip:${provider}:${why}`);continue;}
    }
    if(Object.hasOwn(request,'model')&&request.model!==entry.model)fail('model does not match complexity tier');
    let reason=`scenario:${scenarioId};tier:${tier}`;
    if(category)reason=`category:${category};`+reason;
    if(skips.length)reason+=';'+skips.join(';');
    const decision={provider,scenario:scenarioId,category,tier,model:entry.model,reasoning_effort:entry.reasoning_effort,mode:configured.mode,reason};
    const payload={event:'task_routing',project_masked:maskProject(project),provider:decision.provider,category:decision.category,
      tier:decision.tier,model:decision.model,effort:decision.reasoning_effort,reason:decision.reason,scenario:decision.scenario};
    (log??console).info?.(JSON.stringify(payload));
    return decision;
  }
  fail('no ready provider for scenario');
}

export function providerReadiness(name,{env={},configuredProviders={},hasDotSubscription=false}={}){
  const item=configuredProviders[name];
  if(!item)return [false,'not_configured'];
  const kind=item.type;
  if(kind==='dot_events')return hasDotSubscription?[true,'ready']:[false,'no_subscription'];
  if(kind==='grok_webhook'||kind==='claude_routine'){
    if(item.enabled!==true)return [false,'disabled'];
    if(name==='grokbot'&&!env.AI_SERVICE_GROK_READY)return [false,'grok_not_ready'];
    if(typeof item.key_env!=='string'||!env[item.key_env])return [false,'missing_credential'];
    return [true,'ready'];
  }
  if(kind==='gemini_api'){
    if(item.enabled!==true||item.api_usage_authorized!==true)return [false,'disabled'];
    if(typeof item.key_env!=='string'||!env[item.key_env])return [false,'missing_credential'];
    return [true,'ready'];
  }
  if(kind==='chatgpt_plan_oauth'){
    if(item.plan_usage_authorized!==true)return [false,'disabled'];
    if(typeof item.token_env!=='string'||!env[item.token_env])return [false,'missing_credential'];
    return [true,'ready'];
  }
  if(kind==='muse'){
    if(item.enabled!==true)return [false,'disabled'];
    if(!env.AI_SERVICE_MUSE_READY)return [false,'muse_not_ready'];
    if(typeof item.key_env!=='string'||!env[item.key_env])return [false,'missing_credential'];
    return [true,'ready'];
  }
  if(kind==='claudeduet')return [false,'unavailable'];
  return [false,'unsupported'];
}

export function admittedProviders(policy){
  const names=new Set(Object.values(policy.providers??{}));
  for(const scenario of Object.values(policy.routing?.scenarios??{})){
    for(const provider of scenario.providers??[])names.add(provider);
  }
  return names;
}
