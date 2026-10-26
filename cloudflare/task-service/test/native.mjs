import assert from 'node:assert/strict';
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {createHash,createHmac,randomBytes} from 'node:crypto';
import {resolve} from 'node:path';
import {mkdir,writeFile} from 'node:fs/promises';
const production=(await build({entryPoints:['src/worker.mjs'],bundle:true,format:'esm',platform:'browser',write:false,external:['cloudflare:*'],plugins:[{name:'browser',setup(b){b.onResolve({filter:/^@hyperjump\/browser$/},()=>({path:resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));}}]})).outputFiles[0].text;
const fixture=(await build({entryPoints:['test/fixture.mjs'],bundle:true,format:'esm',platform:'browser',write:false,external:['cloudflare:*'],plugins:[{name:'browser',setup(b){b.onResolve({filter:/^@hyperjump\/browser$/},()=>({path:resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));}}]})).outputFiles[0].text;
const organization=process.argv.includes('--pigbibi')?'pigbibi':'quant',organizationId=organization==='quant'?'Quant':'Pigbibi';
const origin='https://tasks.example.test',project=organizationId+'/SyntheticProject',projectId=organization+'-synthetic';
const stateClass=organization==='quant'?'QuantFixtureStore':'PersonalFixtureStore';
const policy={ORGANIZATION:organization,ORGANIZATION_ID:organizationId,PUBLIC_ORIGIN:origin,PROJECT_ID:projectId,TASK_STORE_NAME:'personal-tasks-v1',GITHUB_CLIENT_ID:'synthetic-client',GITHUB_CLIENT_SECRET:'synthetic-only-secret',AUTH_CLIENTS:{},ADMISSIONS_JSON:'[]',PROJECT_CONTEXTS:{[projectId]:{organization,ownerId:organizationId,project,providers:['dot']}},PROJECT_POLICIES:{[project]:{providers:{agent:'dot'},models:{dot:['configured-dot']},max_active:20,max_timeout_seconds:3600,max_request_bytes:2000000}}};
policy.PROJECT_POLICIES[project].providers={agent:'dot','api:disabled':'disabled-api','api:no-authorization':'unapproved-api','api:no-key':'missing-key-api'};
for(const name of ['disabled-api','unapproved-api','missing-key-api'])policy.PROJECT_POLICIES[project].models[name]=['synthetic-fixed'];
policy.API_PROVIDERS={'disabled-api':{type:'gemini_api',enabled:false,api_usage_authorized:true,model:'synthetic-fixed',key_env:'SYNTHETIC_API_KEY'},'unapproved-api':{type:'gemini_api',enabled:true,api_usage_authorized:false,model:'synthetic-fixed',key_env:'SYNTHETIC_API_KEY'},'missing-key-api':{type:'gemini_api',enabled:true,api_usage_authorized:true,model:'synthetic-fixed',key_env:'MISSING_KEY'}};
const deliveries=[],key=randomBytes(32),secret='whsec_'+key.toString('base64');let failedOnce=false,githubUser=10001,outbound=0;
const apiCalls=[];let apiReply='success',releaseAPI;
const outboundService=async request=>{outbound++;const url=new URL(request.url);if(url.hostname==='github.com')return Response.json({access_token:'synthetic-github-token'});if(url.hostname==='api.github.com')return Response.json({id:githubUser});
 if(url.hostname==='generativelanguage.googleapis.com'){
  assert.equal(url.pathname,'/v1beta/models/synthetic-fixed:generateContent');assert.equal(request.headers.get('x-goog-api-key'),'synthetic-api-key');
  const body=await request.json();assert.equal(body.generationConfig.candidateCount,1);assert.ok(body.generationConfig.responseJsonSchema);assert.ok(!body.tools);assert.ok(!body.safetySettings);apiCalls.push(body);
  if(apiReply==='held')await new Promise(resolve=>{releaseAPI=resolve;});
  if(apiReply==='network')throw Error('synthetic connection loss');
  if(apiReply==='rejected')return new Response(null,{status:429});
  if(apiReply==='server')return new Response(null,{status:503});
  if(apiReply==='redirect')return new Response(null,{status:302,headers:{Location:'https://never-follow.example/'}});
  if(apiReply==='bad-receipt')return new Response('{"modelVersion":', {status:200});
  if(apiReply==='oversize')return new Response(' '.repeat(2000001),{status:200});
  const text=apiReply==='schema'?'{"wrong":true}':apiReply==='duplicate'?' {"count":1,"count":2} ':'{"count":2,"ratio":1.0}';
  return Response.json({modelVersion:apiReply==='mismatch'?'another-model':'synthetic-fixed',candidates:[{finishReason:apiReply==='incomplete'?'MAX_TOKENS':'STOP',content:{parts:[{text}]}}],usageMetadata:{promptTokenCount:3,candidatesTokenCount:2,totalTokenCount:5,unsafe:'not copied'}});
 }
 assert.equal(url.hostname,'dot-callback.example');const body=await request.text(),stamp=request.headers.get('webhook-timestamp'),id=request.headers.get('webhook-id');
 const expected='v1,'+createHmac('sha256',key).update(id+'.'+stamp+'.'+body).digest('base64');assert.ok(request.headers.get('webhook-signature').split(' ').includes(expected));const event=JSON.parse(body);
 if(event.type==='verification')return Response.json({challenge:event.challenge});deliveries.push({id,body:event});if(!failedOnce){failedOnce=true;return new Response(null,{status:503});}return new Response(null,{status:202});};
// Test clients are deterministic *configuration* only; grants still use official helpers.
const mf=new Miniflare({cf:false,workers:[{name:'personal',modules:true,script:fixture,compatibilityDate:'2026-07-30',compatibilityFlags:['global_fetch_strictly_public'],bindings:policy,kvNamespaces:{OAUTH_KV:'test-oauth'},durableObjects:{TASK_STATE:{className:stateClass,useSQLite:true}},serviceBindings:{TASK_SERVICE:{name:'personal',entrypoint:'TaskService'}},outboundService}]});
let worker=await mf.getWorker();const cases=[];
const test=async(name,fn)=>{await fn();cases.push({name,status:'PASS'});console.log('PASS '+name);};
const request=(path,init={})=>worker.fetch(origin+path,{...init,redirect:'manual'});
let ids,tokens;
const auth=t=>({'Authorization':'Bearer '+t,'Content-Type':'application/json'});
const mint=async(provider,resource,scope,clientId,overrides={})=>{const verifier=randomBytes(48).toString('base64url'),challenge=createHash('sha256').update(verifier).digest('base64url');const code=await worker.grant({clientId,scope,resource,challenge,provider,...overrides});const r=await request('/oauth/token',{method:'POST',body:new URLSearchParams({grant_type:'authorization_code',code,client_id:clientId,redirect_uri:'http://localhost:8989/callback',code_verifier:verifier,resource})});assert.equal(r.status,200);return (await r.json()).access_token;};
const rpc=async(method,params={},token=tokens.dot)=>{const r=await request('/mcp',{method:'POST',headers:auth(token),body:JSON.stringify({jsonrpc:'2.0',id:1,method,params})});const text=await r.text();return {status:r.status,body:text?JSON.parse(text):null};};
const tool=(name,args)=>rpc('tools/call',{name,arguments:args});
const taskRequest={mode:'agent',model:'configured-dot',objective:'Synthetic engineering counts only',materials:[{source_id:'first-org-synthetic-v1',checks:[{id:'a',passed:true},{id:'b',passed:false}]}],output_schema:{type:'object',properties:{source_id:{const:'first-org-synthetic-v1'},passed:{const:1},failed:{const:1},trade_authorized:{const:false}},required:['source_id','passed','failed','trade_authorized'],additionalProperties:false}};
const submit=async(key,body=taskRequest)=>{const r=await request('/v1/tasks',{method:'POST',headers:{...auth(tokens.caller),'Idempotency-Key':key},body:JSON.stringify(body)});assert.equal(r.status,202);return r.json();};
const get=async id=>{const r=await request('/v1/tasks/'+id,{headers:auth(tokens.caller)});assert.equal(r.status,200);return r.json();};
const until=async(fn,ms=10000)=>{const end=Date.now()+ms;while(Date.now()<end){const v=await fn();if(v)return v;await new Promise(r=>setTimeout(r,50));}throw Error('bounded wait expired');};
try{
 await test('production bundle has no test issuance or out imports',async()=>{assert.ok(!production.includes('synthetic-github-token'));assert.ok(!production.includes('/__fixture__/'));assert.ok(!production.includes('abandonAPI'));assert.ok(!production.includes('/out/'));});
 ids={dot:(await worker.client('http://localhost:8989/callback')).clientId,caller:(await worker.client('http://localhost:8989/callback')).clientId};
 const clients={[ids.dot]:{provider:'dot',scopes:['tasks:read','tasks:work','events:subscribe'],resources:[origin+'/mcp']},[ids.caller]:{provider:null,scopes:['tasks:read','tasks:submit'],resources:[origin+'/v1/tasks']}};
 const admission=[{organization,projectId,githubUserId:'10001',ownerId:organizationId,clientIds:Object.values(ids),providers:['dot'],scopes:['tasks:read','tasks:work','tasks:submit','events:subscribe']}];
 policy.AUTH_CLIENTS=clients;policy.ADMISSIONS_JSON=JSON.stringify(admission);
 // Apply identical configuration to auth and DO; existing Worker bindings are immutable.
 await worker.policy({AUTH_CLIENTS:clients,ADMISSIONS_JSON:JSON.stringify(admission)});
 // Namespace policy is evaluated in its own constructor from configured environment.
 await mf.setOptions({cf:false,workers:[{name:'personal',modules:true,script:fixture,compatibilityDate:'2026-07-30',compatibilityFlags:['global_fetch_strictly_public'],bindings:policy,kvNamespaces:{OAUTH_KV:'test-oauth'},durableObjects:{TASK_STATE:{className:stateClass,useSQLite:true}},serviceBindings:{TASK_SERVICE:{name:'personal',entrypoint:'TaskService'}},outboundService}]});
 worker=await mf.getWorker();
 await test('MCP advertises configured worker scopes; REST advertises only its operation',async()=>{
  const advertised=['tasks:read','tasks:work','events:subscribe'];
  const metadata=await request('/.well-known/oauth-protected-resource/mcp');assert.equal(metadata.status,200);assert.deepEqual((await metadata.json()).scopes_supported,advertised);
  const mcp=await request('/mcp',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});assert.equal(mcp.status,401);assert.ok(mcp.headers.get('WWW-Authenticate').includes('scope="'+advertised.join(' ')+'"'));
  const restMetadata=await request('/.well-known/oauth-protected-resource/v1/tasks');assert.equal(restMetadata.status,200);assert.deepEqual((await restMetadata.json()).scopes_supported,['tasks:read']);
  for(const [method,scope] of [['GET','tasks:read'],['POST','tasks:submit']]){const r=await request('/v1/tasks',{method});assert.equal(r.status,401);assert.ok(r.headers.get('WWW-Authenticate').includes('scope="'+scope+'"'));}
 });
 await test('work-only grant is not expanded by MCP scope advertisement',async()=>{
  const limited=await mint('dot',origin+'/mcp',['tasks:work'],ids.dot),before=outbound;
  assert.deepEqual((await rpc('tools/list',{},limited)).body.result.tools.map(t=>t.name),['get_task','claim_task','report_task_result']);
  assert.deepEqual((await rpc('server/discover',{},limited)).body.result.capabilities,{tools:{}});
  const denied=await rpc('events/subscribe',{name:'task.requested',arguments:{project},delivery:{mode:'webhook',url:'https://dot-callback.example/events',secret},ttlMs:3600000},limited);
  assert.equal(denied.body.error.code,-32003);assert.equal(outbound,before);
 });
 await test('configured scope advertisement does not admit an unknown OAuth client',async()=>{
  const unknown=(await worker.client('http://localhost:8989/callback')).clientId;
  const token=await mint('dot',origin+'/mcp',['tasks:work','events:subscribe'],unknown);
  assert.equal((await rpc('tools/list',{},token)).status,403);
 });
 tokens={dot:await mint('dot',origin+'/mcp',['tasks:read','tasks:work','events:subscribe'],ids.dot),caller:await mint(null,origin+'/v1/tasks',['tasks:read','tasks:submit'],ids.caller)};
 await test('client role/audience cannot self grant submit or worker',async()=>{const r=await request('/v1/tasks',{method:'POST',headers:auth(tokens.dot),body:'{}'});assert.equal(r.status,401);assert.equal((await rpc('tools/list',{},tokens.caller)).status,401);});
 await test('foreign organization and owner grants cannot cross configured admission',async()=>{
  for(const overrides of [{organization:organization==='quant'?'pigbibi':'quant'},{ownerId:'UnadmittedOwner'}]){
   const denied=await mint(null,origin+'/v1/tasks',['tasks:read','tasks:submit'],ids.caller,overrides);
   assert.equal((await request('/v1/tasks/nonexistent',{headers:auth(denied)})).status,403);
  }
  tokens.caller=await mint(null,origin+'/v1/tasks',['tasks:read','tasks:submit'],ids.caller);
 });
 await test('official upstream consent state needs cookie; real callback route exchanges mocked GitHub only in harness',async()=>{
  const verifier=randomBytes(48).toString('base64url'),q=new URLSearchParams({response_type:'code',client_id:ids.caller,redirect_uri:'http://localhost:8989/callback',state:'browser-bound-test',scope:'tasks:read tasks:submit',resource:origin+'/v1/tasks',code_challenge:createHash('sha256').update(verifier).digest('base64url'),code_challenge_method:'S256'});
  const page=await request('/authorize?'+q);assert.equal(page.status,200);const cookie=page.headers.get('set-cookie').split(';')[0],html=await page.text(),handle=html.match(/name="handle" value="([^"]+)"/)[1];
  const approved=await request('/authorize',{method:'POST',headers:{cookie},body:new URLSearchParams({handle,decision:'approve',scope:'tasks:read'})});assert.equal(approved.status,302);
  const upstreamURL=new URL(approved.headers.get('location'));assert.equal(upstreamURL.hostname,'github.com');assert.equal(upstreamURL.searchParams.get('code_challenge_method'),'S256');
  const path='/callback?state='+encodeURIComponent(upstreamURL.searchParams.get('state'))+'&code=synthetic-code';assert.equal((await request(path)).status,400);
  const binding=approved.headers.getSetCookie().find(c=>c.startsWith('__Host-oauth-upstream-')&&!c.includes('Max-Age=0')).split(';')[0];const callback=await request(path,{headers:{cookie:binding}});assert.equal(callback.status,302,callback.status===302?'':(await callback.text())+'; outbound='+outbound);assert.equal(new URL(callback.headers.get('location')).searchParams.get('state'),'browser-bound-test');
 });
 // The callback issued a separate official grant for caller and revokes earlier grant.
 tokens.caller=await mint(null,origin+'/v1/tasks',['tasks:read','tasks:submit'],ids.caller);
 let sub;
 await test('native MCP catalog/challenge/owned subscription actual HMAC',async()=>{
  assert.equal((await rpc('tools/list')).body.result.tools.length,5);
  assert.deepEqual((await rpc('server/discover')).body.result.capabilities,{tools:{},events:{}});assert.equal((await rpc('events/list')).body.result.events[0].name,'task.requested');
  const r=await rpc('events/subscribe',{name:'task.requested',arguments:{project},delivery:{mode:'webhook',url:'https://dot-callback.example/events',secret},ttlMs:3600000});assert.ok(r.body.result,r.body.error?.message);sub=r.body.result.id;
  assert.equal((await tool('get_event_subscription',{subscription_id:sub})).body.result.structuredContent.status,'active');
 });
 let task;
 await test('durable alarm actually dispatches signed native event and retries identical id/body',async()=>{
  task=await submit('native-first');await until(()=>deliveries.length>=2);assert.equal((await get(task.id)).status,'running');assert.equal(deliveries[0].id,deliveries[1].id);assert.deepEqual(deliveries[0].body,deliveries[1].body);assert.equal(deliveries[0].body.data.task_id,task.id);
 });
 await test('worker claim/report and caller read preserve bound identity and final output',async()=>{
  const claim=await tool('claim_task',{task_id:task.id,attempt_id:'native-attempt'});assert.equal(claim.body.result.structuredContent.status,'running');
  const out={source_id:'first-org-synthetic-v1',passed:1,failed:1,trade_authorized:false};
  const reported=await tool('report_task_result',{task_id:task.id,attempt_id:'native-attempt',revision:1,status:'completed',output:out});assert.equal(reported.body.result.structuredContent.status,'completed');assert.deepEqual((await get(task.id)).output,out);
 });
 await test('same frozen intent finds same task after accepted response loss, no resubmit',async()=>{
  const digest=createHash('sha256').update((await import('../src/canonical.mjs')).serialize((await import('../src/canonical.mjs')).parseJson(JSON.stringify(taskRequest)))).digest('hex');
  const r=await request('/v1/tasks/lookup',{headers:{...auth(tokens.caller),'Idempotency-Key':'native-first','X-Request-SHA256':digest}});assert.equal(r.status,200);assert.deepEqual(await r.json(),{id:task.id});
 });
 await test('cancel is real state; no false confirmation before native worker stops',async()=>{
  const t=await submit('cancel-running');await until(async()=> (await get(t.id)).status==='running');await tool('claim_task',{task_id:t.id,attempt_id:'cancel-attempt'});
  const r=await request('/v1/tasks/'+t.id+'/cancel',{method:'POST',headers:auth(tokens.caller),body:'{}'});assert.equal(r.status,200);assert.equal((await r.json()).status,'cancel_requested');
  const report=await tool('report_task_result',{task_id:t.id,attempt_id:'cancel-attempt',revision:1,status:'cancelled'});assert.equal(report.body.result.structuredContent.status,'cancelled');
 });
 await test('deadline alarm requests cancellation; late completed is rejected until worker confirms stop',async()=>{
  const t=await submit('deadline-native',{...taskRequest,timeout_seconds:1});await until(async()=> (await get(t.id)).status==='running');
  await tool('claim_task',{task_id:t.id,attempt_id:'deadline-attempt'});
  await until(async()=> (await get(t.id)).status==='cancel_requested',4000);
  const late=await tool('report_task_result',{task_id:t.id,attempt_id:'deadline-attempt',revision:1,status:'completed',output:{source_id:'first-org-synthetic-v1',passed:1,failed:1,trade_authorized:false}});assert.equal(late.body.error.code,-32009);
  const stopped=await tool('report_task_result',{task_id:t.id,attempt_id:'deadline-attempt',revision:1,status:'cancelled'});assert.equal(stopped.body.result.structuredContent.status,'cancelled');
 });
 const apiRequest={mode:'api',profile:'summary',model:'synthetic-fixed',objective:'Synthetic counts only',materials:[{checks:[true,false,true]}],output_schema:{type:'object',properties:{count:{const:2},ratio:{const:1}},required:['count','ratio'],additionalProperties:false}};
 await test('API remains unavailable without explicit provider configuration',async()=>{
  const before=apiCalls.length;
  policy.PROJECT_POLICIES[project].providers['api:summary']='fixed-api';policy.PROJECT_POLICIES[project].models['fixed-api']=['synthetic-fixed'];
  for(const profile of ['summary','disabled','no-authorization','no-key']){const r=await request('/v1/tasks',{method:'POST',headers:{...auth(tokens.caller),'Idempotency-Key':'disabled-api-'+profile},body:JSON.stringify({...apiRequest,profile})});assert.equal(r.status,403);}assert.equal(apiCalls.length,before);
 });
 policy.API_PROVIDERS={'fixed-api':{type:'gemini_api',enabled:true,api_usage_authorized:true,model:'synthetic-fixed',key_env:'SYNTHETIC_API_KEY',max_output_tokens:128,timeout_seconds:3}};policy.SYNTHETIC_API_KEY='synthetic-api-key';
 await mf.setOptions({cf:false,workers:[{name:'personal',modules:true,script:fixture,compatibilityDate:'2026-07-30',compatibilityFlags:['global_fetch_strictly_public'],bindings:policy,kvNamespaces:{OAUTH_KV:'test-oauth'},durableObjects:{TASK_STATE:{className:stateClass,useSQLite:true}},serviceBindings:{TASK_SERVICE:{name:'personal',entrypoint:'TaskService'}},outboundService}]});worker=await mf.getWorker();
 await test('explicit API profile executes once in real SQLite and preserves canonical output/events/lookup',async()=>{
  const before=apiCalls.length,t=await submit('api-success',apiRequest);await until(async()=> (await get(t.id)).status==='completed');
  const result=await get(t.id);assert.deepEqual(result.output,{count:2,ratio:1});assert.equal(result.handle.model_verification,'provider_reported');assert.deepEqual(result.handle.usage,{input_tokens:3,output_tokens:2,total_tokens:5});assert.equal(apiCalls.length,before+1);
  const response=await request('/v1/tasks/'+t.id,{headers:auth(tokens.caller)});assert.match(await response.text(),/"ratio":1\.0/);
  assert.equal((await submit('api-success',apiRequest)).id,t.id);assert.equal(apiCalls.length,before+1);
  const canonical=(await import('../src/canonical.mjs')),digest=createHash('sha256').update(canonical.serialize(canonical.parseJson(JSON.stringify(apiRequest)))).digest('hex');
  const found=await request('/v1/tasks/lookup',{headers:{...auth(tokens.caller),'Idempotency-Key':'api-success','X-Request-SHA256':digest}});assert.deepEqual(await found.json(),{id:t.id});
  const events=await (await request('/v1/tasks/'+t.id+'/events',{headers:auth(tokens.caller)})).json();assert.deepEqual(events.events.map(e=>e.status),['queued','submitting','completed']);
  assert.equal((await tool('claim_task',{task_id:t.id,attempt_id:'not-an-agent'})).body.error.code,-32003);
 });
 await test('in-flight API does not block client reads/cancellation or resubmit during another alarm',async()=>{
  apiReply='held';const before=apiCalls.length,t=await submit('api-held',apiRequest);await until(()=>Boolean(releaseAPI));
  assert.equal((await get(t.id)).status,'submitting');const cancelled=await request('/v1/tasks/'+t.id+'/cancel',{method:'POST',headers:auth(tokens.caller),body:'{}'});assert.equal((await cancelled.json()).status,'cancel_requested');
  apiReply='success';const other=await submit('api-concurrent',apiRequest);assert.equal((await get(t.id)).status,'cancel_requested');releaseAPI();releaseAPI=null;
  await until(async()=> (await get(t.id)).status==='completed');await until(async()=> (await get(other.id)).status==='completed');assert.equal(apiCalls.length,before+2);
 });
 await test('expired in-flight API schedules bounded recovery rather than a 10ms alarm loop',async()=>{
  apiReply='held';const before=apiCalls.length,t=await submit('api-expired-in-flight',apiRequest);await until(()=>Boolean(releaseAPI));
  const started=await worker.alarmProbe('stats');await worker.alarmProbe('expire',project,t.id);await new Promise(resolve=>setTimeout(resolve,250));const naturally=await worker.alarmProbe('stats');
  const ticks=[];
  try{
   for(let i=0;i<5;i++)ticks.push(await worker.alarmProbe('tick'));
   const proof={organization,realWorkerd:true,realSQLite:true,heldSyntheticFetch:true,observationWindowMs:250,naturalAlarmCount:naturally.count-started.count,manualAlarmCount:ticks.at(-1).count-naturally.count,scheduledDelaysMs:ticks.map(tick=>tick.scheduledDelayMs),outboundCalls:apiCalls.length-before};
   await mkdir('test-results',{recursive:true});await writeFile('test-results/alarm-backoff-'+organization+'.json',JSON.stringify(proof,null,2)+'\n');
   assert.ok(ticks.every(tick=>tick.scheduledDelayMs>=900),JSON.stringify(proof));assert.equal(apiCalls.length,before+1);assert.equal((await get(t.id)).status,'submitting');
  }finally{apiReply='success';releaseAPI();releaseAPI=null;}
  await until(async()=> (await get(t.id)).status==='completed');
 });
 await test('abandoned durable API submission becomes unknown without an outbound replacement',async()=>{
  const before=apiCalls.length;for(const cancel of [false,true]){const id=await worker.abandonAPI(project,apiRequest,cancel);await until(async()=> (await get(id)).status==='outcome_unknown');}assert.equal(apiCalls.length,before);
 });
 await test('unconfigured model/profile/image and foreign organization cause no API request',async()=>{
  const before=apiCalls.length;
  for(const changes of [{model:'another-model'},{profile:'unconfigured'},{materials:[{type:'image',mime_type:'image/png',base64:'YQ=='}]}]){
   const r=await request('/v1/tasks',{method:'POST',headers:{...auth(tokens.caller),'Idempotency-Key':'api-denied-'+Math.random()},body:JSON.stringify({...apiRequest,...changes})});assert.equal(r.status,403);
  }
  const foreign=await mint(null,origin+'/v1/tasks',['tasks:read','tasks:submit'],ids.caller,{organization:organization==='quant'?'pigbibi':'quant'});
  assert.equal((await request('/v1/tasks',{method:'POST',headers:{...auth(foreign),'Idempotency-Key':'foreign-api'},body:JSON.stringify(apiRequest)})).status,403);
  tokens.caller=await mint(null,origin+'/v1/tasks',['tasks:read','tasks:submit'],ids.caller);assert.equal(apiCalls.length,before);
 });
 await test('API rejection/model mismatch/schema/duplicate output never become successful results',async()=>{
  for(const [reply,status] of [['rejected','failed'],['mismatch','failed'],['schema','invalid_output'],['duplicate','failed'],['incomplete','failed']]){
   apiReply=reply;const before=apiCalls.length,t=await submit('api-'+reply,apiRequest);await until(async()=> (await get(t.id)).status===status);const result=await get(t.id);assert.equal(result.output,null);assert.equal(apiCalls.length,before+1);if(reply==='mismatch')assert.equal(result.handle.model_verification,'mismatch');
  }
 });
 await test('API lost/invalid/oversize/server/redirect receipts stay unknown without retry or fallback',async()=>{
  for(const reply of ['network','server','redirect','bad-receipt','oversize']){
   apiReply=reply;const before=apiCalls.length,t=await submit('api-'+reply,apiRequest);await until(async()=> (await get(t.id)).status==='outcome_unknown');assert.equal((await submit('api-'+reply,apiRequest)).id,t.id);await get(t.id);assert.equal(apiCalls.length,before+1);
  }
  apiReply='success';const t=await submit('api-after-unknown',apiRequest);await until(async()=> (await get(t.id)).status==='completed');assert.equal(apiCalls.length,15);
 });
 await test('unsubscribe stops further acceptance, unavailable provider fails instead of paid fallback',async()=>{
  assert.equal((await tool('delete_event_subscription',{subscription_id:sub})).body.result.structuredContent.status,'gone');const t=await submit('no-subscription');await until(async()=> (await get(t.id)).status==='failed');
  const r=await request('/v1/tasks',{method:'POST',headers:{...auth(tokens.caller),'Idempotency-Key':'no-paid-fallback'},body:JSON.stringify({...taskRequest,mode:'api'})});assert.equal(r.status,403);
 });
 await mkdir('test-results',{recursive:true});await writeFile('test-results/native-'+organization+'.json',JSON.stringify({organization,cases,count:cases.length,realWorkerd:true,realGithub:false,realDot:false,testWebhookDeliveries:deliveries.length,externalRealRequests:0,outboundIntercepted:outbound},null,2)+'\n');
 console.log(`RESULT ${cases.length}/${cases.length} PASS; real workerd, test GitHub/webhook only`);
}finally{await mf.dispose();}
// npm test runs both organizations; the workload suite already covers both, so run it once.
if (!process.argv.includes('--pigbibi')) await import('./workload.mjs');
