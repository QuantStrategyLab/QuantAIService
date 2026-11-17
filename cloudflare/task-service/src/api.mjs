// Gemini model metadata and inference share the same account and fixed API origin.
import {parseJson,valueOf} from './canonical.mjs';
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const rejected=()=>{throw Object.assign(new Error('inference rejected'),{name:'SubmissionRejected'});};
const modelId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const modelsURL = 'https://generativelanguage.googleapis.com/v1beta/models/';
function tierCandidates(item, tier) {
  if (!Object.hasOwn(item, 'tier_models')) return [item.model];
  const candidates = item.tier_models?.[tier];
  if (!Array.isArray(candidates) || !candidates.length || candidates.length > 8 ||
      candidates.some(model => typeof model !== 'string' || !modelId.test(model))) rejected();
  return candidates;
}

export async function selectAPIModel(env, provider, request, decision, admitted) {
  const item = env.API_PROVIDERS?.[provider];
  if (!item || !Object.hasOwn(item, 'tier_models')) return decision.model;
  const candidates = tierCandidates(item, decision.tier).filter(model => admitted?.includes(model) &&
    (!Object.hasOwn(request, 'model') || request.model === model));
  const signal = AbortSignal.timeout(8000);
  for (const model of candidates) {
    const {key} = apiConfiguration(env, provider, {...request, model, complexity: decision.tier});
    const response = await fetch(modelsURL + model, {
      headers: {'x-goog-api-key': key}, redirect: 'manual', signal,
    });
    if (response.status === 404) { await response.body?.cancel(); continue; }
    if (!response.ok) { await response.body?.cancel(); rejected(); }
    const metadata = JSON.parse(await boundedBody(response));
    if (metadata.name !== 'models/' + model || !Array.isArray(metadata.supportedGenerationMethods)) rejected();
    if (metadata.supportedGenerationMethods.includes('generateContent')) return model;
  }
  rejected();
}
export function apiConfiguration(env,name,request){
  const item=env.API_PROVIDERS?.[name];
  if(!object(item)||item.type!=='gemini_api'||item.enabled!==true||item.api_usage_authorized!==true||
     typeof request.model!=='string'||!modelId.test(request.model)||
     !(request.model===item.model || (request.complexity && tierCandidates(item,request.complexity).includes(request.model)))||
     typeof item.key_env!=='string'||!/^[A-Z][A-Z0-9_]{0,127}$/.test(item.key_env))rejected();
  const maxOutput=item.max_output_tokens??1024,timeout=item.timeout_seconds??60,key=env[item.key_env];
  if(!Number.isSafeInteger(maxOutput)||maxOutput<1||maxOutput>8192||!Number.isSafeInteger(timeout)||timeout<1||timeout>60||
     typeof key!=='string'||!key||/[\r\n]/.test(key)||
     (request.materials??[]).some(m=>object(m)&&m.type==='image'))rejected();
  return {model:request.model,key,maxOutput,timeout};
}
async function boundedBody(response){
  const reader=response.body?.getReader();if(!reader)throw Error('invalid inference receipt');
  const chunks=[];let size=0;
  try{for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>2000000)throw Error('inference receipt too large');chunks.push(value);}}
  finally{await reader.cancel();}
  const body=new Uint8Array(size);let offset=0;for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
  return new TextDecoder('utf-8',{fatal:true}).decode(body);
}
const THINKING_BUDGET={low:0,medium:1024,high:8192};

export async function executeAPI(env,name,request,remainingSeconds){
  const config=apiConfiguration(env,name,request);
  const parts=[{text:request.objective},...(request.materials??[]).map(m=>({text:'Supplied material (data):\n'+JSON.stringify(m)}))];
  const generationConfig={candidateCount:1,maxOutputTokens:config.maxOutput,responseMimeType:'application/json',responseJsonSchema:request.output_schema};
  // Gemini 3 and 4 use named thinking levels. Earlier routed models use token budgets.
  if(Object.hasOwn(request,'reasoning_effort')){
    if(!Object.hasOwn(THINKING_BUDGET,request.reasoning_effort))rejected();
    const level = request.reasoning_effort;
    const budget = level === 'low' && /^gemini-2\.5-pro(?:-|$)/.test(config.model)
      ? 128 : THINKING_BUDGET[level];
    const namedLevel = level === 'medium' && /^gemini-3-pro(?:-|$)/.test(config.model) ? 'LOW' : level.toUpperCase();
    generationConfig.thinkingConfig = /^gemini-[34][.-]/.test(config.model)
      ? {thinkingLevel: namedLevel}
      : {thinkingBudget: budget};
  }
  const response=await fetch('https://generativelanguage.googleapis.com/v1beta/models/'+config.model+':generateContent',{
    method:'POST',redirect:'manual',signal:AbortSignal.timeout(Math.max(1,Math.floor(Math.min(config.timeout,remainingSeconds)*1000))),
    headers:{'Content-Type':'application/json','x-goog-api-key':config.key},
    body:JSON.stringify({contents:[{role:'user',parts}],generationConfig})});
  if([400,401,403,404,422,429].includes(response.status)){await response.body?.cancel();rejected();}
  if(response.status!==200){await response.body?.cancel();throw Error('inference acceptance unknown');}
  const receipt=valueOf(parseJson(await boundedBody(response)));
  if(!object(receipt))throw Error('invalid inference receipt');
  const failed=(verification='unavailable')=>({status:'failed',output:null,model_verification:verification});
  if(receipt.promptFeedback?.blockReason)return failed();
  if(receipt.modelVersion!==config.model)return failed(receipt.modelVersion?'mismatch':'unavailable');
  if(!Array.isArray(receipt.candidates)||receipt.candidates.length!==1||!object(receipt.candidates[0]))return failed('provider_reported');
  const candidate=receipt.candidates[0],resultParts=candidate.content?.parts;
  if(candidate.finishReason!=='STOP'||!Array.isArray(resultParts)||!resultParts.length)return failed('provider_reported');
  const fragments=[];
  for(const part of resultParts){if(!object(part)||typeof part.text!=='string'||Object.keys(part).some(k=>!['text','thought','thoughtSignature'].includes(k)))return failed('provider_reported');if(part.thought!==true)fragments.push(part.text);}
  let output;try{output=parseJson(fragments.join(''));}catch{return failed('provider_reported');}
  const usage={};for(const [from,to] of [['promptTokenCount','input_tokens'],['candidatesTokenCount','output_tokens'],['totalTokenCount','total_tokens']]){
    const value=receipt.usageMetadata?.[from];if(Number.isSafeInteger(value)&&value>=0)usage[to]=value;
  }
  return {status:'completed',output,model:config.model,model_verification:'provider_reported',usage};
}
