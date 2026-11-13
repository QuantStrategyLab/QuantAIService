// Routine / Cloud Agent triggers contain identifiers only; results arrive through scoped MCP tools.
import {callbackURL} from './webhooks.mjs';
const rejected=()=>{throw Object.assign(new Error('routine rejected'),{name:'SubmissionRejected'});};
const MUSE_LAUNCH='https://api.cursor.com/v1/agents';
export function nativeConfiguration(env,name){
  const c=env.AGENT_PROVIDERS?.[name];
  // claudeduet stays a disabled scaffold. muse uses Cloud Agents API + MCP claim.
  if(!c||c.enabled!==true||!['grok_webhook','claude_routine','muse'].includes(c.type)||
     typeof c.key_env!=='string'||!/^[A-Z][A-Z0-9_]{0,127}$/.test(c.key_env))rejected();
  let url;try{url=new URL(callbackURL(c.url));}catch{rejected();}
  if(c.type==='claude_routine'&&(url.origin!=='https://api.anthropic.com'||
     !/^\/v1\/claude_code\/routines\/trig_[A-Za-z0-9]+\/fire$/.test(url.pathname)||url.search))rejected();
  if(c.type==='muse'&&(url.href!==MUSE_LAUNCH))rejected();
  if(c.model!=null&&(typeof c.model!=='string'||!c.model||/[\r\n]/.test(c.model)))rejected();
  if(c.repository!=null){
    let repo;try{repo=new URL(callbackURL(c.repository));}catch{rejected();}
    if(repo.search||repo.hash)rejected();
  }
  const key=env[c.key_env];
  if(typeof key!=='string'||!key||/[\r\n]/.test(key))rejected();
  return {type:c.type,url:url.href,key,model:c.model??null,repository:c.repository??null};
}
export async function triggerNative(env,name,event,remainingSeconds=30){
  const c=nativeConfiguration(env,name);
  const body={type:'task.requested',...event,resource:env.PUBLIC_ORIGIN+'/mcp'};
  const headers={'Content-Type':'application/json'};
  let payload;
  if(c.type==='muse'){
    headers.Authorization='Basic '+btoa(c.key+':');
    payload={prompt:{text:'AI Service task notification. Identifiers only. Authenticate to the MCP resource as the muse worker, then claim_task and report_task_result.\n'+JSON.stringify(body)}};
    if(c.model)payload.model={id:c.model};
    if(c.repository)payload.repos=[{url:c.repository}];
  }else{
    headers.Authorization='Bearer '+c.key;
    if(c.type==='claude_routine')headers['anthropic-version']='2026-10-21';
    payload=c.type==='claude_routine'?{text:JSON.stringify(body)}:body;
  }
  const reply=await fetch(c.url,{method:'POST',headers,redirect:'manual',
    signal:AbortSignal.timeout(Math.max(1,Math.floor(Math.min(30,remainingSeconds)*1000))),
    body:JSON.stringify(payload)});
  await reply.body?.cancel();
  if(c.type==='muse'){
    if(reply.status===200||reply.status===201)return {task_id:event.task_id,acceptance:'agent_started',model_verification:'unavailable'};
  }else if(reply.status===200){
    return {task_id:event.task_id,acceptance:'routine_started',model_verification:'unavailable'};
  }
  // 5xx/timeouts may follow an accepted run. Neither path supports deduplication.
  if(reply.status>=500)throw Error('routine acceptance unknown');
  rejected();
}
