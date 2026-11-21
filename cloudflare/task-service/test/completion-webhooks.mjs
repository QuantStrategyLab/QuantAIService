import assert from 'node:assert/strict';
import {mock} from 'node:test';
import {CompletionWebhooks} from '../src/completion-webhooks.mjs';

const project='Owner/Project',policy={[project]:{}};
const route={enabled:true,profile:'review',url:'https://consumer.example/ai/completed',secret_env:'CALLBACK_SECRET'};
const calls=[],sql={exec:(...args)=>{calls.push(args);}};
const empty=new CompletionWebhooks(sql,{PROJECT_POLICIES:policy});
assert.equal(await empty.flush(),null);assert.equal(calls.length,0);
const disabled=new CompletionWebhooks(sql,{PROJECT_POLICIES:policy,COMPLETION_WEBHOOKS:{[project]:{...route,enabled:false}}});
assert.equal(disabled.routes.size,0);assert.equal(calls.length,0);
assert.throws(()=>new CompletionWebhooks(sql,{PROJECT_POLICIES:{},COMPLETION_WEBHOOKS:{[project]:route}}));
assert.throws(()=>new CompletionWebhooks(sql,{PROJECT_POLICIES:policy,COMPLETION_WEBHOOKS:{[project]:{...route,url:'http://localhost'}}}));
assert.throws(()=>new CompletionWebhooks(sql,{PROJECT_POLICIES:policy,COMPLETION_WEBHOOKS:{[project]:route},CALLBACK_SECRET:'short'}));
const active=new CompletionWebhooks(sql,{PROJECT_POLICIES:policy,COMPLETION_WEBHOOKS:{[project]:route},CALLBACK_SECRET:'s'.repeat(32)});
const withTimeout=timeout_ms=>new CompletionWebhooks(sql,{
  PROJECT_POLICIES:policy,COMPLETION_WEBHOOKS:{[project]:{...route,timeout_ms}},CALLBACK_SECRET:'s'.repeat(32),
});
for(const invalid of [null,1999,25001,'20000'])assert.throws(()=>withTimeout(invalid),/invalid completion webhook timeout/);
const slow=withTimeout(20000);
assert.equal(active.routes.get(project).timeout,2000);
assert.equal(slow.routes.get(project).identity,active.routes.get(project).identity);
const before=calls.length;
active.enqueue({owner:project,id:'task',revision:1,profile:'other',material:'private'},'completed',1);
assert.equal(calls.length,before);
active.enqueue({owner:project,id:'task',revision:1,profile:'review',material:'private'},'completed',1);
const body=JSON.parse(calls.at(-1).at(-1));
assert.deepEqual(Object.keys(body).sort(),['event_id','name','project','revision','status','task_id']);
assert.ok(!JSON.stringify(body).includes('private'));
const delivery={owner:project,route:slow.routes.get(project).identity,seq:1,body:JSON.stringify(body),attempts:0};
const signal=mock.method(AbortSignal,'timeout',()=>new AbortController().signal);
const fetch=mock.method(globalThis,'fetch',async()=>new Response(null,{status:202}));
slow.sql={exec:(statement,...args)=>{
  if(statement.startsWith('SELECT *'))return {toArray:()=>[delivery]};
  if(statement.startsWith('SELECT MIN'))return {one:()=>({n:null})};
  calls.push(args);
}};
try{
  assert.equal(await slow.flush(),null);
  assert.deepEqual(signal.mock.calls[0].arguments,[20000]);
  assert.equal(fetch.mock.callCount(),1);
  assert.equal(calls.at(-1)[0],'sent');
}finally{
  signal.mock.restore();
  fetch.mock.restore();
}
console.log('PASS completion configuration: disabled, project boundary, HTTPS, protected secret, profile boundary and metadata-only hints');
