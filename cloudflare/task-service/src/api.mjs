// Opt-in structured-output API providers (gemini_api, openai_api, anthropic_api).
import {field,parseJson,valueOf} from './canonical.mjs';
import {imageMaterials} from './image-materials.mjs';
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const rejected=()=>{throw Object.assign(new Error('inference rejected'),{name:'SubmissionRejected'});};
const modelId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const modelsURL = 'https://generativelanguage.googleapis.com/v1beta/models/';
const openaiModelsURL = 'https://api.openai.com/v1/models/';
const anthropicModelsURL = 'https://api.anthropic.com/v1/models/';
const escapeRegExp = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
export function matchesModel(configured, reported) {
  if (typeof configured !== 'string' || !configured || typeof reported !== 'string') return false;
  return reported === configured ||
    new RegExp('^' + escapeRegExp(configured) + '-(?:\\d{4}-\\d{2}-\\d{2}|\\d{8})$').test(reported);
}
const API_PROVIDER_DEFAULTS = {
  gemini_api: {maxOutput: 1024, timeout: 60},
  openai_api: {maxOutput: 4096, timeout: 90},
  anthropic_api: {maxOutput: 4096, timeout: 90},
};
const REASONING_EFFORTS = new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh']);
const ANTHROPIC_EFFORTS = new Set(['low', 'medium', 'high']);
const THINKING_BUDGET = {low: 0, medium: 1024, high: 8192};

function tierCandidates(item, tier) {
  if (!Object.hasOwn(item, 'tier_models')) return [item.model];
  const candidates = item.tier_models?.[tier];
  if (!Array.isArray(candidates) || !candidates.length || candidates.length > 8 ||
      candidates.some(model => typeof model !== 'string' || !modelId.test(model))) rejected();
  return candidates;
}

export function apiModelCandidates(env, provider, request, decision, admitted) {
  const item = env.API_PROVIDERS?.[provider];
  if (!item || !Object.hasOwn(item, 'tier_models')) return [decision.model];
  const approved = tierCandidates(item, decision.tier);
  const ordered = approved.includes(decision.model)
    ? [decision.model, ...approved.filter(model => model !== decision.model)]
    : approved;
  return ordered.filter(model => admitted?.includes(model) &&
    (!Object.hasOwn(request, 'model') || request.model === model));
}

function isMissingModelReadScope(raw) {
  try {
    const parsed = JSON.parse(raw);
    const err = parsed?.error;
    if (!object(err)) return false;
    return `${err.code ?? ''} ${err.message ?? ''}`.includes('api.model.read');
  } catch {
    return false;
  }
}

export async function selectAPIModel(env, provider, request, decision, admitted) {
  const item = env.API_PROVIDERS?.[provider];
  if (!item || !Object.hasOwn(item, 'tier_models')) return decision.model;
  if (!Object.hasOwn(API_PROVIDER_DEFAULTS, item.type)) return decision.model;
  const candidates = apiModelCandidates(env, provider, request, decision, admitted);
  const signal = AbortSignal.timeout(8000);
  for (const model of candidates) {
    const {type, key} = apiConfiguration(env, provider, {...request, model, complexity: decision.tier});
    let response;
    if (type === 'gemini_api') {
      response = await fetch(modelsURL + model, {
        headers: {'x-goog-api-key': key}, redirect: 'manual', signal,
      });
    } else if (type === 'openai_api') {
      response = await fetch(openaiModelsURL + model, {
        headers: {'Authorization': 'Bearer ' + key}, redirect: 'manual', signal,
      });
    } else {
      response = await fetch(anthropicModelsURL + model, {
        headers: {'x-api-key': key, 'anthropic-version': '2023-06-01'}, redirect: 'manual', signal,
      });
    }
    if (response.status === 404) { await response.body?.cancel(); continue; }
    if (type === 'openai_api' && (response.status === 401 || response.status === 403)) {
      const errBody = await boundedBody(response);
      if (isMissingModelReadScope(errBody)) return model;
      rejected();
    }
    if (!response.ok) { await response.body?.cancel(); rejected(); }
    const metadata = JSON.parse(await boundedBody(response));
    if (type === 'gemini_api') {
      if (metadata.name !== 'models/' + model || !Array.isArray(metadata.supportedGenerationMethods)) rejected();
      if (metadata.supportedGenerationMethods.includes('generateContent')) return model;
    } else {
      if (!object(metadata) || !matchesModel(model, metadata.id)) rejected();
      return model;
    }
  }
  rejected();
}

export function apiConfiguration(env,name,request){
  const item=env.API_PROVIDERS?.[name];
  if(!object(item)||!Object.hasOwn(API_PROVIDER_DEFAULTS,item.type)||item.enabled!==true||item.api_usage_authorized!==true||
     typeof request.model!=='string'||!modelId.test(request.model)||
     !(request.model===item.model || (request.complexity && tierCandidates(item,request.complexity).includes(request.model)))||
     typeof item.key_env!=='string'||!/^[A-Z][A-Z0-9_]{0,127}$/.test(item.key_env))rejected();
  const defaults=API_PROVIDER_DEFAULTS[item.type];
  const maxOutput=item.max_output_tokens??defaults.maxOutput,timeout=item.timeout_seconds??defaults.timeout,key=env[item.key_env];
  if(!Number.isSafeInteger(maxOutput)||maxOutput<1||maxOutput>32768||!Number.isSafeInteger(timeout)||timeout<1||timeout>180||
     typeof key!=='string'||!key||/[\r\n]/.test(key))rejected();
  const materials=request.materials??[];
  if(item.type==='gemini_api'){
    if(materials.some(m=>object(m)&&m.type==='image'))rejected();
  }else{
    try{imageMaterials(materials);}catch{rejected();}
  }
  if(Object.hasOwn(request,'reasoning_effort')){
    const allowed=item.type==='gemini_api'
      ?Object.hasOwn(THINKING_BUDGET,request.reasoning_effort)
      :item.type==='openai_api'
        ?REASONING_EFFORTS.has(request.reasoning_effort)
        :ANTHROPIC_EFFORTS.has(request.reasoning_effort);
    if(!allowed)rejected();
  }
  return {type:item.type,model:request.model,key,maxOutput,timeout};
}

async function boundedBody(response){
  const reader=response.body?.getReader();if(!reader)throw Error('invalid inference receipt');
  const chunks=[];let size=0;
  try{for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>4000000)throw Error('inference receipt too large');chunks.push(value);}}
  finally{await reader.cancel();}
  const body=new Uint8Array(size);let offset=0;for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
  return new TextDecoder('utf-8',{fatal:true}).decode(body);
}

const failed=(verification='unavailable')=>({status:'failed',output:null,model_verification:verification});

function extractUsage(source,pairs){
  const usage={};
  if(!object(source))return usage;
  for(const [from,to] of pairs){
    const value=source[from];
    if(Number.isSafeInteger(value)&&value>=0)usage[to]=value;
  }
  return usage;
}

async function postProviderJSON(url,headers,payload,timeoutMs){
  const response=await fetch(url,{method:'POST',redirect:'manual',signal:AbortSignal.timeout(timeoutMs),headers,body:JSON.stringify(payload)});
  if([400,401,403,404,422,429].includes(response.status)){await response.body?.cancel();rejected();}
  if(response.status!==200){await response.body?.cancel();throw Error('inference acceptance unknown');}
  return await boundedBody(response);
}

function parseOpenAIReceipt(raw){
  if(raw.trimStart().startsWith('{')){
    const receipt=valueOf(parseJson(raw));
    if(!object(receipt))throw Error('invalid inference receipt');
    return {completed:true,receipt};
  }
  let terminal=null;
  for(const record of raw.replaceAll('\r\n','\n').split('\n\n')){
    const data=record.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
    if(!data||data==='[DONE]')continue;
    const event=valueOf(parseJson(data));
    if(!object(event))throw Error('invalid inference receipt');
    if(['response.completed','response.failed','response.incomplete'].includes(event.type)){
      if(terminal!==null)throw Error('ambiguous terminal inference events');
      terminal=event;
    }
  }
  if(terminal===null)throw Error('inference ended without a terminal event');
  if(terminal.type!=='response.completed')return {completed:false,receipt:null};
  if(!object(terminal.response))throw Error('invalid inference receipt');
  return {completed:true,receipt:terminal.response};
}

async function executeGemini(config,request,timeoutMs){
  const parts=[{text:request.objective},...(request.materials??[]).map(m=>({text:'Supplied material (data):\n'+JSON.stringify(m)}))];
  const generationConfig={candidateCount:1,maxOutputTokens:config.maxOutput,responseMimeType:'application/json',responseJsonSchema:request.output_schema};
  // Gemini 3 and 4 use named thinking levels. Earlier routed models use token budgets.
  if(Object.hasOwn(request,'reasoning_effort')){
    const level=request.reasoning_effort;
    const budget=level==='low'&&/^gemini-2\.5-pro(?:-|$)/.test(config.model)?128:THINKING_BUDGET[level];
    const namedLevel=level==='medium'&&/^gemini-3-pro(?:-|$)/.test(config.model)?'LOW':level.toUpperCase();
    generationConfig.thinkingConfig=/^gemini-[34][.-]/.test(config.model)?{thinkingLevel:namedLevel}:{thinkingBudget:budget};
  }
  const raw=await postProviderJSON('https://generativelanguage.googleapis.com/v1beta/models/'+config.model+':generateContent',
    {'Content-Type':'application/json','x-goog-api-key':config.key},{contents:[{role:'user',parts}],generationConfig},timeoutMs);
  const receipt=valueOf(parseJson(raw));
  if(!object(receipt))throw Error('invalid inference receipt');
  if(receipt.promptFeedback?.blockReason)return failed();
  if(!matchesModel(config.model,receipt.modelVersion))return failed(receipt.modelVersion?'mismatch':'unavailable');
  if(!Array.isArray(receipt.candidates)||receipt.candidates.length!==1||!object(receipt.candidates[0]))return failed('provider_reported');
  const candidate=receipt.candidates[0],resultParts=candidate.content?.parts;
  if(candidate.finishReason!=='STOP'||!Array.isArray(resultParts)||!resultParts.length)return failed('provider_reported');
  const fragments=[];
  for(const part of resultParts){if(!object(part)||typeof part.text!=='string'||Object.keys(part).some(k=>!['text','thought','thoughtSignature'].includes(k)))return failed('provider_reported');if(part.thought!==true)fragments.push(part.text);}
  let output;try{output=parseJson(fragments.join(''));}catch{return failed('provider_reported');}
  const usage=extractUsage(receipt.usageMetadata,[['promptTokenCount','input_tokens'],['candidatesTokenCount','output_tokens'],['totalTokenCount','total_tokens']]);
  return {status:'completed',output,model:config.model,model_verification:'provider_reported',usage};
}

async function executeOpenAI(config,request,timeoutMs){
  const content=[{type:'input_text',text:request.objective},...(request.materials??[]).map(m=>
    object(m)&&m.type==='image'
      ?{type:'input_image',image_url:'data:'+m.mime_type+';base64,'+m.base64}
      :{type:'input_text',text:'Supplied material (data):\n'+JSON.stringify(m)}
  )];
  const body={model:config.model,input:[{role:'user',content}],store:false,max_output_tokens:config.maxOutput,
    text:{format:{type:'json_schema',name:'task_result',schema:request.output_schema,strict:true}}};
  if(Object.hasOwn(request,'reasoning_effort'))body.reasoning={effort:request.reasoning_effort};
  const raw=await postProviderJSON('https://api.openai.com/v1/responses',
    {'Content-Type':'application/json','Authorization':'Bearer '+config.key},body,timeoutMs);
  const {completed,receipt}=parseOpenAIReceipt(raw);
  if(!completed||receipt.status!=='completed')return failed();
  if(!matchesModel(config.model,receipt.model))return failed(receipt.model?'mismatch':'unavailable');
  if(!Array.isArray(receipt.output))return failed('provider_reported');
  const fragments=[];
  for(const item of receipt.output){
    if(!object(item)||item.type!=='message')continue;
    if(!Array.isArray(item.content))return failed('provider_reported');
    for(const part of item.content){
      if(!object(part))return failed('provider_reported');
      if(part.type==='refusal')return failed('provider_reported');
      if(part.type==='output_text'&&typeof part.text==='string')fragments.push(part.text);
    }
  }
  if(!fragments.length)return failed('provider_reported');
  let output;try{output=parseJson(fragments.join(''));}catch{return failed('provider_reported');}
  if(output.kind!=='object')return failed('provider_reported');
  const usage=extractUsage(receipt.usage,[['input_tokens','input_tokens'],['output_tokens','output_tokens'],['total_tokens','total_tokens']]);
  return {status:'completed',output,model:config.model,model_verification:'provider_reported',usage};
}

async function executeAnthropic(config,request,timeoutMs){
  const content=[{type:'text',text:request.objective},...(request.materials??[]).map(m=>
    object(m)&&m.type==='image'
      ?{type:'image',source:{type:'base64',media_type:m.mime_type,data:m.base64}}
      :{type:'text',text:'Supplied material (data):\n'+JSON.stringify(m)}
  )];
  const body={model:config.model,max_tokens:config.maxOutput,messages:[{role:'user',content}],
    tools:[{name:'record_result',description:'Emit structured JSON output matching the schema.',input_schema:request.output_schema}],
    tool_choice:{type:'tool',name:'record_result'}};
  if(Object.hasOwn(request,'reasoning_effort'))body.output_config={effort:request.reasoning_effort};
  const raw=await postProviderJSON('https://api.anthropic.com/v1/messages',
    {'Content-Type':'application/json','x-api-key':config.key,'anthropic-version':'2023-06-01'},body,timeoutMs);
  const receiptNode=parseJson(raw);
  if(receiptNode.kind!=='object')throw Error('invalid inference receipt');
  const receipt=valueOf(receiptNode);
  if(!matchesModel(config.model,receipt.model))return failed(receipt.model?'mismatch':'unavailable');
  const contentNode=field(receiptNode,'content');
  if(receipt.stop_reason!=='tool_use'||contentNode?.kind!=='array')return failed('provider_reported');
  const toolUses=contentNode.entries.filter(block=>block.kind==='object'&&field(block,'type')?.value==='tool_use'&&field(block,'name')?.value==='record_result');
  const inputNode=toolUses.length===1?field(toolUses[0],'input'):undefined;
  if(inputNode?.kind!=='object')return failed('provider_reported');
  const usage=extractUsage(receipt.usage,[['input_tokens','input_tokens'],['output_tokens','output_tokens']]);
  return {status:'completed',output:inputNode,model:config.model,model_verification:'provider_reported',usage};
}

export async function executeAPI(env,name,request,remainingSeconds){
  const config=apiConfiguration(env,name,request);
  const timeoutMs=Math.max(1,Math.floor(Math.min(config.timeout,remainingSeconds)*1000));
  if(config.type==='openai_api')return executeOpenAI(config,request,timeoutMs);
  if(config.type==='anthropic_api')return executeAnthropic(config,request,timeoutMs);
  return executeGemini(config,request,timeoutMs);
}
