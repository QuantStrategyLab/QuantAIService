// Routine triggers contain identifiers only; results arrive through scoped MCP tools.
import {callbackURL} from './webhooks.mjs';
const rejected=()=>{throw Object.assign(new Error('routine rejected'),{name:'SubmissionRejected'});};
export function nativeConfiguration(env,name){
  const c=env.AGENT_PROVIDERS?.[name];
  if(!c||c.enabled!==true||!['grok_webhook','claude_routine'].includes(c.type)||
     typeof c.key_env!=='string'||!/^[A-Z][A-Z0-9_]{0,127}$/.test(c.key_env))rejected();
  let url;try{url=new URL(callbackURL(c.url));}catch{rejected();}
  if(c.type==='claude_routine'&&(url.origin!=='https://api.anthropic.com'||
     !/^\/v1\/claude_code\/routines\/trig_[A-Za-z0-9]+\/fire$/.test(url.pathname)||url.search))rejected();
  const key=env[c.key_env];
  if(typeof key!=='string'||!key||/[\r\n]/.test(key))rejected();
  return {type:c.type,url:url.href,key};
}
export async function triggerNative(env,name,event,remainingSeconds=30){
  const c=nativeConfiguration(env,name);
  const body={type:'task.requested',...event,resource:env.PUBLIC_ORIGIN+'/mcp'};
  const headers={'Content-Type':'application/json',Authorization:'Bearer '+c.key};
  if(c.type==='claude_routine')headers['anthropic-version']='2026-10-21';
  const reply=await fetch(c.url,{method:'POST',headers,redirect:'manual',
    signal:AbortSignal.timeout(Math.max(1,Math.floor(Math.min(30,remainingSeconds)*1000))),
    body:JSON.stringify(c.type==='claude_routine'?{text:JSON.stringify(body)}:body)});
  await reply.body?.cancel();
  if(reply.status===200)return {task_id:event.task_id,acceptance:'routine_started',model_verification:'unavailable'};
  // 5xx/timeouts may follow an accepted run. Neither routine supports deduplication.
  if(reply.status>=500)throw Error('routine acceptance unknown');
  rejected();
}
