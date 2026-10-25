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
const deliveries=[],key=randomBytes(32),secret='whsec_'+key.toString('base64');let failedOnce=false,githubUser=10001,outbound=0;
const outboundService=async request=>{outbound++;const url=new URL(request.url);if(url.hostname==='github.com')return Response.json({access_token:'synthetic-github-token'});if(url.hostname==='api.github.com')return Response.json({id:githubUser});
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
 await test('production bundle has no test issuance or out imports',async()=>{assert.ok(!production.includes('synthetic-github-token'));assert.ok(!production.includes('/__fixture__/'));assert.ok(!production.includes('/out/'));});
 ids={dot:(await worker.client('http://localhost:8989/callback')).clientId,caller:(await worker.client('http://localhost:8989/callback')).clientId};
 const clients={[ids.dot]:{provider:'dot',scopes:['tasks:read','tasks:work','events:subscribe'],resources:[origin+'/mcp']},[ids.caller]:{provider:null,scopes:['tasks:read','tasks:submit'],resources:[origin+'/v1/tasks']}};
 const admission=[{organization,projectId,githubUserId:'10001',ownerId:organizationId,clientIds:Object.values(ids),providers:['dot'],scopes:['tasks:read','tasks:work','tasks:submit','events:subscribe']}];
 policy.AUTH_CLIENTS=clients;policy.ADMISSIONS_JSON=JSON.stringify(admission);
 // Apply identical configuration to auth and DO; existing Worker bindings are immutable.
 await worker.policy({AUTH_CLIENTS:clients,ADMISSIONS_JSON:JSON.stringify(admission)});
 // Namespace policy is evaluated in its own constructor from configured environment.
 await mf.setOptions({cf:false,workers:[{name:'personal',modules:true,script:fixture,compatibilityDate:'2026-07-30',compatibilityFlags:['global_fetch_strictly_public'],bindings:policy,kvNamespaces:{OAUTH_KV:'test-oauth'},durableObjects:{TASK_STATE:{className:stateClass,useSQLite:true}},serviceBindings:{TASK_SERVICE:{name:'personal',entrypoint:'TaskService'}},outboundService}]});
 worker=await mf.getWorker();
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
 await test('unsubscribe stops further acceptance, unavailable provider fails instead of paid fallback',async()=>{
  assert.equal((await tool('delete_event_subscription',{subscription_id:sub})).body.result.structuredContent.status,'gone');const t=await submit('no-subscription');await until(async()=> (await get(t.id)).status==='failed');
  const r=await request('/v1/tasks',{method:'POST',headers:{...auth(tokens.caller),'Idempotency-Key':'no-paid-fallback'},body:JSON.stringify({...taskRequest,mode:'api'})});assert.equal(r.status,403);
 });
 await mkdir('test-results',{recursive:true});await writeFile('test-results/native-'+organization+'.json',JSON.stringify({organization,cases,count:cases.length,realWorkerd:true,realGithub:false,realDot:false,testWebhookDeliveries:deliveries.length,externalRealRequests:0,outboundIntercepted:outbound},null,2)+'\n');
 console.log(`RESULT ${cases.length}/${cases.length} PASS; real workerd, test GitHub/webhook only`);
}finally{await mf.dispose();}
