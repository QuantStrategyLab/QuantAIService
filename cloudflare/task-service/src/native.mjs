// Routine / Cloud Agent triggers contain identifiers only; results arrive through scoped MCP tools.
import {callbackURL} from './webhooks.mjs';
const rejected=()=>{throw Object.assign(new Error('routine rejected'),{name:'SubmissionRejected'});};
const MUSE_LAUNCH='https://api.cursor.com/v1/agents';
const musePrompt=(body)=>({text:'AI Service task notification. Identifiers only. Authenticate to the MCP resource as the muse worker, then claim_task and report_task_result.\n'+JSON.stringify(body)});
function museHandle(taskId,payload,fallback=null){
  const agent=payload?.agent&&typeof payload.agent==='object'?payload.agent:{};
  const run=payload?.run&&typeof payload.run==='object'?payload.run:{};
  const agentId=typeof agent.id==='string'?agent.id
    :(typeof payload?.agentId==='string'?payload.agentId:fallback?.agent_id);
  const runId=typeof run.id==='string'?run.id
    :(typeof payload?.runId==='string'?payload.runId
      :(typeof agent.latestRunId==='string'?agent.latestRunId:fallback?.run_id));
  const handle={task_id:taskId,acceptance:'agent_started',model_verification:'unavailable'};
  if(typeof agentId==='string'&&agentId)handle.agent_id=agentId;
  if(typeof runId==='string'&&runId)handle.run_id=runId;
  return handle;
}
async function museReply(url,headers,payload,remainingSeconds){
  const reply=await fetch(url,{method:'POST',headers,redirect:'manual',
    signal:AbortSignal.timeout(Math.max(1,Math.floor(Math.min(30,remainingSeconds)*1000))),
    body:payload===null?undefined:JSON.stringify(payload)});
  const text=await reply.text();
  let parsed=null;try{parsed=text?JSON.parse(text):null;}catch{parsed=null;}
  return {reply,parsed};
}
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
export async function triggerNative(env,name,event,remainingSeconds=30,existingHandle=null){
  const c=nativeConfiguration(env,name);
  const body={type:'task.requested',...event,resource:env.PUBLIC_ORIGIN+'/mcp'};
  const headers={'Content-Type':'application/json'};
  if(c.type==='muse'){
    headers.Authorization='Basic '+btoa(c.key+':');
    const prior=existingHandle&&typeof existingHandle==='object'?existingHandle:null;
    const agentId=typeof prior?.agent_id==='string'?prior.agent_id:null;
    if(event.change!=='requested'){
      if(!agentId)rejected();
      if(event.change==='cancel_requested'&&typeof prior.run_id==='string'&&prior.run_id){
        const cancel=await museReply(`${MUSE_LAUNCH}/${agentId}/runs/${prior.run_id}/cancel`,headers,null,remainingSeconds);
        if(cancel.reply.status>=500)throw Error('routine acceptance unknown');
        // 2xx or 409 run_not_cancellable still allow a steer so the muse worker can report.
        if(cancel.reply.status>=400&&cancel.reply.status!==409)rejected();
      }
      const follow=await museReply(`${MUSE_LAUNCH}/${agentId}/runs`,headers,
        {prompt:musePrompt(body),delivery:'steer'},remainingSeconds);
      if(follow.reply.status>=500)throw Error('routine acceptance unknown');
      if(follow.reply.status!==200&&follow.reply.status!==201&&follow.reply.status!==202)rejected();
      return museHandle(event.task_id,follow.parsed,prior);
    }
    const payload={prompt:musePrompt(body)};
    if(c.model)payload.model={id:c.model};
    if(c.repository)payload.repos=[{url:c.repository}];
    const created=await museReply(c.url,headers,payload,remainingSeconds);
    if(created.reply.status>=500)throw Error('routine acceptance unknown');
    if(created.reply.status!==200&&created.reply.status!==201)rejected();
    return museHandle(event.task_id,created.parsed,{task_id:event.task_id});
  }
  headers.Authorization='Bearer '+c.key;
  if(c.type==='claude_routine')headers['anthropic-version']='2026-10-21';
  const payload=c.type==='claude_routine'?{text:JSON.stringify(body)}:body;
  const reply=await fetch(c.url,{method:'POST',headers,redirect:'manual',
    signal:AbortSignal.timeout(Math.max(1,Math.floor(Math.min(30,remainingSeconds)*1000))),
    body:JSON.stringify(payload)});
  await reply.body?.cancel();
  if(reply.status===200){
    return {task_id:event.task_id,acceptance:'routine_started',model_verification:'unavailable'};
  }
  // 5xx/timeouts may follow an accepted run. Neither path supports deduplication.
  if(reply.status>=500)throw Error('routine acceptance unknown');
  rejected();
}
