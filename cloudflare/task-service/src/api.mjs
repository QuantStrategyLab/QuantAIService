// Opt-in port of ai_service.gemini.GeminiProvider. No model discovery or fallback.
import {parseJson,valueOf} from './canonical.mjs';
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const rejected=()=>{throw Object.assign(new Error('inference rejected'),{name:'SubmissionRejected'});};
export function apiConfiguration(env,name,request){
  const item=env.API_PROVIDERS?.[name];
  if(!object(item)||item.type!=='gemini_api'||item.enabled!==true||item.api_usage_authorized!==true||
     typeof item.model!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(item.model)||request.model!==item.model||
     typeof item.key_env!=='string'||!/^[A-Z][A-Z0-9_]{0,127}$/.test(item.key_env))rejected();
  const maxOutput=item.max_output_tokens??1024,timeout=item.timeout_seconds??60,key=env[item.key_env];
  if(!Number.isSafeInteger(maxOutput)||maxOutput<1||maxOutput>8192||!Number.isSafeInteger(timeout)||timeout<1||timeout>60||
     typeof key!=='string'||!key||/[\r\n]/.test(key)||
     (request.materials??[]).some(m=>object(m)&&m.type==='image'))rejected();
  return {model:item.model,key,maxOutput,timeout};
}
async function boundedBody(response){
  const reader=response.body?.getReader();if(!reader)throw Error('invalid inference receipt');
  const chunks=[];let size=0;
  try{for(;;){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>2000000)throw Error('inference receipt too large');chunks.push(value);}}
  finally{await reader.cancel();}
  const body=new Uint8Array(size);let offset=0;for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
  return new TextDecoder('utf-8',{fatal:true}).decode(body);
}
export async function executeAPI(env,name,request,remainingSeconds){
  const config=apiConfiguration(env,name,request);
  const parts=[{text:request.objective},...(request.materials??[]).map(m=>({text:'Supplied material (data):\n'+JSON.stringify(m)}))];
  const response=await fetch('https://generativelanguage.googleapis.com/v1beta/models/'+config.model+':generateContent',{
    method:'POST',redirect:'manual',signal:AbortSignal.timeout(Math.max(1,Math.floor(Math.min(config.timeout,remainingSeconds)*1000))),
    headers:{'Content-Type':'application/json','x-goog-api-key':config.key},
    body:JSON.stringify({contents:[{role:'user',parts}],generationConfig:{candidateCount:1,maxOutputTokens:config.maxOutput,responseMimeType:'application/json',responseJsonSchema:request.output_schema}})});
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
  for(const part of resultParts){if(!object(part)||typeof part.text!=='string'||Object.keys(part).some(k=>!['text','thought'].includes(k)))return failed('provider_reported');if(part.thought!==true)fragments.push(part.text);}
  let output;try{output=parseJson(fragments.join(''));}catch{return failed('provider_reported');}
  const usage={};for(const [from,to] of [['promptTokenCount','input_tokens'],['candidatesTokenCount','output_tokens'],['totalTokenCount','total_tokens']]){
    const value=receipt.usageMetadata?.[from];if(Number.isSafeInteger(value)&&value>=0)usage[to]=value;
  }
  return {status:'completed',output,model:config.model,model_verification:'provider_reported',usage};
}
