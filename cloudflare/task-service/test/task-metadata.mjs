import assert from 'node:assert/strict';
import {before, after, test} from 'node:test';
import {generateKeyPairSync, sign} from 'node:crypto';
import {resolve} from 'node:path';
import {build} from 'esbuild';
import {Miniflare} from 'miniflare';

const origin='https://tasks.example.test', issuer='https://token.actions.githubusercontent.com';
const owner='ExampleOwner', project=owner+'/SyntheticProject', otherProject=owner+'/OtherProject';
const {privateKey,publicKey}=generateKeyPairSync('rsa',{modulusLength:2048});
const jwk={...publicKey.export({format:'jwk'}),kid:'synthetic-key',alg:'RS256',use:'sig'};
const grants=[['observer',project,['tasks:list']],['legacy',project,['tasks:read','tasks:submit']],['other',otherProject,['tasks:list']]].map(([identity,project,scopes])=>{
  const ref='refs/heads/'+identity;
  return {issuer,identity,project,scopes,subject:`repo:${project}:ref:${ref}`,
    claims:{repository:project,ref,workflow_ref:`${project}/.github/workflows/synthetic.yml@${ref}`}};
});
let mf,worker,store;
const timestamp=Date.now()/1000;
const old=timestamp-3600;
const iso=seconds=>new Date(seconds*1000).toISOString();
const token=(grant=grants[0],overrides={})=>{
  const now=Math.floor(Date.now()/1000);
  const claims={iss:issuer,sub:grant.subject,aud:origin+'/v1/tasks',iat:now-5,nbf:now-5,exp:now+300,
    actor_id:'10001',repository_owner:owner,...grant.claims,...overrides};
  const body=[{alg:'RS256',kid:jwk.kid,typ:'JWT'},claims].map(value=>Buffer.from(JSON.stringify(value)).toString('base64url')).join('.');
  return body+'.'+sign('RSA-SHA256',Buffer.from(body),privateKey).toString('base64url');
};
const request=(path='/v1/tasks',bearer=token(),options={})=>worker.fetch(origin+path,{headers:bearer?{Authorization:'Bearer '+bearer}:{},...options});
const body=async response=>{assert.equal(response.status,200,await response.clone().text());assert.equal(response.headers.get('Cache-Control'),'no-store');return response.json();};

before(async()=>{
  const script=(await build({entryPoints:['test/task-metadata-fixture.mjs'],bundle:true,format:'esm',platform:'browser',write:false,
    external:['cloudflare:*'],plugins:[{name:'browser',setup(b){b.onResolve({filter:/^@hyperjump\/browser$/},()=>({path:resolve('node_modules/@hyperjump/browser/lib/index.browser.js')}));}}]})).outputFiles[0].text;
  const organization=process.env.METADATA_ORGANIZATION??'quant';
  const policy={providers:{agent:'dot'},models:{dot:['synthetic-model']},max_request_bytes:2000000,max_active:500,max_timeout_seconds:3600};
  mf=new Miniflare({cf:false,workers:[{name:'metadata',modules:true,script,compatibilityDate:'2026-07-30',compatibilityFlags:['global_fetch_strictly_public'],
    bindings:{ORGANIZATION:organization,ORGANIZATION_ID:organization==='quant'?'Quant':'Pigbibi',PUBLIC_ORIGIN:origin,
      PROJECT_ID:'synthetic-project',TASK_STORE_NAME:'synthetic-store',AUTH_CLIENTS:{},ADMISSIONS_JSON:'[]',
      GITHUB_WORKLOAD_AUDIENCE:origin+'/v1/tasks',GITHUB_WORKLOAD_GRANTS_JSON:JSON.stringify(grants),
      PROJECT_CONTEXTS:{'synthetic-project':{organization,ownerId:owner,project,providers:['dot']},
        'other-project':{organization,ownerId:owner,project:otherProject,providers:['dot']}},
      PROJECT_POLICIES:{[project]:policy,[otherProject]:policy}},
    kvNamespaces:{OAUTH_KV:'synthetic-oauth'},
    durableObjects:{TASK_STATE:{className:organization==='quant'?'QuantMetadataStore':'PersonalMetadataStore',useSQLite:true}},
    serviceBindings:{TASK_SERVICE:{name:'metadata',entrypoint:'TaskService'}},
    outboundService:request=>{assert.equal(request.url,issuer+'/.well-known/jwks');return Response.json({keys:[jwk]});}
  }]});
  worker=await mf.getWorker();
  const ns=await mf.getDurableObjectNamespace('TASK_STATE');store=ns.get(ns.idFromName('synthetic-store'));
  await store.seed([
    {id:'synthetic-queued-old',provider:'dot',status:'queued',created:old},
    {id:'synthetic-queued-new',provider:'dot',status:'queued',created:timestamp-60},
    {id:'synthetic-claimed',provider:'dot',status:'running',created:old,attempt:'synthetic-attempt',claimed_at:old+1},
    {id:'synthetic-legacy-claimed',provider:'dot',status:'cancel_requested',created:old,attempt:'synthetic-attempt'},
    {id:'synthetic-unclaimed',provider:'dot',status:'running',created:old},
    {id:'synthetic-completed',provider:'api',status:'completed',created:old,claimed_at:old+1,completed_at:old+2,updated:old+2},
    {id:'synthetic-failed',provider:'api',status:'failed',created:old,error:'submission_rejected',completed_at:old+3,updated:old+3},
    {id:'synthetic-masked-error',provider:'api',status:'failed',created:old,error:'PRIVATE ERROR WITH SECRET',completed_at:old+4,updated:old+4},
    {id:'synthetic-cancelled',provider:'dot',status:'cancelled',created:old,attempt:'synthetic-attempt',completed_at:old+5,updated:old+5},
  ].map(row=>({...row,owner:project})));
  await store.seed([{id:'synthetic-other',owner:otherProject,provider:'dot',status:'queued',created:old}]);
});
after(async()=>{await mf?.dispose();});

test('deny missing authentication, legacy grants, forged scopes and invalid signatures',async()=>{
  const before=await store.sizes();
  for(const path of ['/v1/tasks','/v1/tasks/stats']){
    assert.equal((await request(path,null)).status,401);
    assert.equal((await request(path,token(grants[1]))).status,403);
    assert.equal((await request(path,token(grants[1],{scope:'tasks:list'}))).status,403);
    assert.equal((await request(path,token(grants[0],{scope:'tasks:read'}))).status,403);
    assert.equal((await request(path,token(grants[0],{aud:'https://wrong.example.test'}))).status,401);
    assert.equal((await request(path,token(grants[0],{sub:'repo:ExampleOwner/Unregistered:ref:refs/heads/main'}))).status,401);
    const valid=token();const parts=valid.split('.');parts[2]=(parts[2][0]==='A'?'B':'A')+parts[2].slice(1);
    assert.equal((await request(path,parts.join('.'))).status,401);
  }
  assert.deepEqual(await store.sizes(),before);
});

test('list-only grant cannot read task content or submit, cancel, or message',async()=>{
  const before=await store.sizes();
  assert.equal((await request('/v1/tasks/synthetic-claimed')).status,403);
  assert.equal((await request('/v1/tasks/synthetic-claimed/events')).status,403);
  for(const path of ['/v1/tasks','/v1/tasks/synthetic-claimed/cancel','/v1/tasks/synthetic-claimed/messages'])
    assert.equal((await request(path,token(),{method:'POST',body:'{}'})).status,403);
  assert.deepEqual(await store.sizes(),before);
});

test('project isolation and minimal metadata with safe error codes',async()=>{
  const before=await store.sizes();
  assert.equal(await worker.providerAccess('/v1/tasks'),403);
  assert.equal(await worker.providerAccess('/v1/tasks/stats'),403);
  const result=await body(await request());
  assert.equal(result.tasks.length,9);assert.equal(result.next_cursor,null);
  const fields=['id','provider','status','created_at','claimed_at','completed_at','error_code'].sort();
  for(const row of result.tasks)assert.deepEqual(Object.keys(row).sort(),fields);
  assert.equal(result.tasks.find(row=>row.id==='synthetic-claimed').claimed_at,iso(old+1));
  assert.equal(result.tasks.find(row=>row.id==='synthetic-completed').completed_at,iso(old+2));
  assert.equal(result.tasks.find(row=>row.id==='synthetic-failed').error_code,'submission_rejected');
  assert.equal(result.tasks.find(row=>row.id==='synthetic-masked-error').error_code,'unknown_error');
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  assert.equal((await request('/v1/tasks?project='+encodeURIComponent(otherProject))).status,403);
  const other=await body(await request('/v1/tasks',token(grants[2])));assert.deepEqual(other.tasks.map(row=>row.id),['synthetic-other']);
  assert.deepEqual(await store.sizes(),before);
});

test('since/provider/status filters combine and use an inclusive ISO boundary',async()=>{
  const result=await body(await request('/v1/tasks?provider=dot&status=queued&since='+encodeURIComponent(iso(old))));
  assert.deepEqual(result.tasks.map(row=>row.id),['synthetic-queued-new','synthetic-queued-old']);
  const recent=await body(await request('/v1/tasks?since='+encodeURIComponent(iso(old+1))));
  assert.deepEqual(recent.tasks.map(row=>row.id),['synthetic-queued-new']);
  assert.deepEqual((await body(await request('/v1/tasks?provider=missing'))).tasks,[]);
  assert.deepEqual((await body(await request('/v1/tasks?since=2999-01-01T00:00:00Z'))).tasks,[]);
});

test('keyset pagination handles equal timestamps without duplicates or gaps',async()=>{
  const expected=(await body(await request())).tasks.map(row=>row.id),seen=[];
  let cursor=null;
  do{
    const result=await body(await request('/v1/tasks?limit=2'+(cursor?'&cursor='+encodeURIComponent(cursor):'')));
    seen.push(...result.tasks.map(row=>row.id));cursor=result.next_cursor;
    if(cursor){
      assert.equal((await request('/v1/tasks?status=queued&cursor='+cursor)).status,400);
      assert.equal((await request('/v1/tasks?cursor='+cursor,token(grants[2]))).status,400);
    }
  }while(cursor);
  assert.deepEqual(seen,expected);assert.equal(new Set(seen).size,9);
});

test('reject malformed query values and duplicate parameters',async()=>{
  for(const query of ['limit=0','limit=-1','limit=1.2','limit=abc','limit=1&limit=2','since=bad',
    'since=2026-02-30T00:00:00Z','since=2026-01-01T24:00:00Z','since=2026-01-01',
    'status=unknown','provider=','provider=x%27%20OR%201','cursor=invalid','cursor=','owner=other'])
    assert.equal((await request('/v1/tasks?'+query)).status,400,query);
  assert.equal((await request('/v1/tasks/stats?provider=dot')).status,400);
});

test('stats count provider/status pairs and stuck tasks, honoring since and project',async()=>{
  const before=await store.sizes();
  const stats=await body(await request('/v1/tasks/stats'));
  assert.deepEqual(stats.counts,[{provider:'api',status:'completed',count:1},{provider:'api',status:'failed',count:2},
    {provider:'dot',status:'cancel_requested',count:1},{provider:'dot',status:'cancelled',count:1},
    {provider:'dot',status:'queued',count:2},{provider:'dot',status:'running',count:2}]);
  assert.deepEqual(stats.stuck,{claimed_not_completed:2,queued_over_threshold:1,total:3});
  assert.equal(stats.queued_threshold_minutes,15);
  const recent=await body(await request('/v1/tasks/stats?since='+encodeURIComponent(iso(old+1))));
  assert.deepEqual(recent.counts,[{provider:'dot',status:'queued',count:1}]);assert.equal(recent.stuck.total,0);
  const empty=await body(await request('/v1/tasks/stats?since=2999-01-01T00:00:00Z'));
  assert.deepEqual(empty.counts,[]);assert.equal(empty.stuck.total,0);
  const other=await body(await request('/v1/tasks/stats',token(grants[2])));assert.equal(other.stuck.total,1);
  assert.deepEqual(await store.sizes(),before);
});

test('legacy migration preserves terminal times and leaves unknown claim times null',async()=>{
  await store.migrateLegacy();
  const result=await body(await request());
  assert.equal(result.tasks.find(row=>row.id==='synthetic-completed').completed_at,iso(old+2));
  assert.equal(result.tasks.find(row=>row.id==='synthetic-claimed').claimed_at,null);
  assert.equal((await body(await request('/v1/tasks/stats'))).stuck.claimed_not_completed,2);
});

test('real claim and terminal report record stable lifecycle timestamps',async()=>{
  await store.seed([{id:'synthetic-lifecycle',owner:project,provider:'dot',status:'running',created:timestamp}]);
  await store.claim(project,'synthetic-lifecycle','dot','synthetic-worker','synthetic-attempt');
  let row=(await body(await request())).tasks.find(row=>row.id==='synthetic-lifecycle');
  const claimed=row.claimed_at;assert.ok(Date.parse(claimed)>=timestamp*1000);assert.equal(row.completed_at,null);
  await store.claim(project,row.id,'dot','synthetic-worker','synthetic-attempt');
  const report={body:JSON.stringify({revision:1,status:'completed',output:{},artifacts:[]}),terminal_status:'completed'};
  await store.commitReport(project,row.id,'dot','synthetic-worker','synthetic-attempt',report);
  row=(await body(await request())).tasks.find(row=>row.id==='synthetic-lifecycle');
  assert.equal(row.claimed_at,claimed);assert.ok(Date.parse(row.completed_at)>=Date.parse(claimed));
  await store.commitReport(project,row.id,'dot','synthetic-worker','synthetic-attempt',report);
  assert.deepEqual((await body(await request())).tasks.find(task=>task.id===row.id),row);
});

test('default limit is 50 and explicit limits are capped at 200',async()=>{
  await store.seed(Array.from({length:205},(_,index)=>({id:'synthetic-page-'+String(index).padStart(3,'0'),owner:otherProject,
    provider:'dot',status:'queued',created:old})));
  const bearer=token(grants[2]);
  assert.equal((await body(await request('/v1/tasks',bearer))).tasks.length,50);
  const first=await body(await request('/v1/tasks?limit=999',bearer));assert.equal(first.tasks.length,200);assert.ok(first.next_cursor);
  const next=await body(await request('/v1/tasks?limit=200&cursor='+first.next_cursor,bearer));assert.equal(next.tasks.length,6);assert.equal(next.next_cursor,null);
});

// Regressions adapted from the exact-head list/stats review probes.
test('cursor conceals identity and rejects tamper, foreign projects and changed filters',async()=>{
  const before=await store.dump();
  const page=await body(await request('/v1/tasks?limit=1'));
  const cursor=page.next_cursor;
  for(const encoding of ['utf8','latin1','hex','base64','base64url']) {
    const decoded=Buffer.from(cursor,'base64url').toString(encoding);
    for(const marker of [owner,project,Buffer.from(owner).toString('hex'),Buffer.from(project).toString('base64')])
      assert.ok(!decoded.includes(marker),'cursor must not disclose internal identity');
  }
  const data=Buffer.from(cursor,'base64url');
  for(const index of [0,Math.floor(data.length/2),data.length-1]) {
    const changed=Buffer.from(data);changed[index]^=1;
    const response=await request('/v1/tasks?cursor='+changed.toString('base64url'));
    assert.equal(response.status,400);assert.deepEqual(await response.json(),{error:'invalid arguments'});
  }
  for(const path of ['?status=queued','?provider=dot','?since='+encodeURIComponent(iso(old))])
    assert.equal((await request('/v1/tasks'+path+'&cursor='+cursor)).status,400);
  assert.equal((await request('/v1/tasks?cursor='+cursor,token(grants[2]))).status,400);
  const expected=await body(await request('/v1/tasks?limit=1&cursor='+cursor));
  await store.reinitialize();
  assert.deepEqual((await body(await request('/v1/tasks?limit=1&cursor='+cursor))).tasks,expected.tasks);
  assert.equal((await request('/v1/tasks?cursor='+Buffer.from(JSON.stringify([project,0,null,null,1e99,'zzzz'])).toString('base64url'))).status,400);
  assert.deepEqual(await store.dump(),before);
});

test('5000-row history has bounded list reads for every filter and deep pages',async()=>{
  await store.seed(Array.from({length:5000},(_,i)=>({id:'synthetic-load-'+String(i).padStart(5,'0'),owner:project,
    provider:'dot',status:'queued',created:old-100-i})));
  const measurements=[];
  for(const filters of [{},{status:'invalid_output'},{provider:'missing'},{provider:'missing',status:'failed'},
    {status:'queued'},{provider:'dot'},{provider:'dot',status:'queued'}]) {
    let cursor=null;
    for(let page=0;page<3;page++) {
      const measured=await store.measureMetadata(project,{since:0,provider:null,status:null,limit:1,...filters,cursor});
      assert.equal(measured.error,undefined);
      measurements.push({filters,page,cost:measured.cost});
      assert.ok(measured.cost.every(c=>c.read<=16),'tiny pages must seek instead of scan history');
      assert.ok(measured.cost.every(c=>c.written===0));
      cursor=measured.result.next_cursor;if(!cursor)break;
    }
  }
  let cursor=null, total=0, pages=0;
  do {
    const measured=await store.measureMetadata(project,{since:0,provider:null,status:null,limit:200,cursor});
    assert.equal(measured.error,undefined);
    assert.ok(measured.cost.every(c=>c.read<=410),'later pages must seek directly to their boundary');
    assert.ok(measured.cost.every(c=>c.written===0));
    total+=measured.result.tasks.length;pages++;cursor=measured.result.next_cursor;
  } while(cursor);
  assert.equal(total,5010);assert.equal(pages,26);
  console.log(JSON.stringify({probe:'bounded-list',measurements,deep_pages:pages,total}));
});

test('statistics stop at the scan budget without returning partial counts',async()=>{
  const before=await store.dump();
  const stats=await store.measureMetadata(project,{since:0},true);
  console.log(JSON.stringify({probe:'bounded-statistics',cost:stats.cost,error:stats.error}));
  assert.equal(stats.error,'ValueError','over-budget statistics must fail closed');
  assert.ok(stats.cost.every(c=>c.read<=2010),'statistics must stop at the documented row budget');
  assert.ok(stats.cost.every(c=>c.written===0));
  const response=await request('/v1/tasks/stats');assert.equal(response.status,400);
  assert.deepEqual(await response.json(),{error:'invalid arguments'});
  const recent=await body(await request('/v1/tasks/stats?since='+encodeURIComponent(iso(old))));
  assert.equal(recent.counts.reduce((n,c)=>n+c.count,0),10);
  assert.deepEqual(await store.dump(),before);
});

test('statistics budget boundary preserves exact counts and project isolation',async()=>{
  // Other project already has 206 tasks. Fill precisely to the budget.
  await store.seed(Array.from({length:794},(_,i)=>({id:'synthetic-budget-'+i,owner:otherProject,
    provider:'dot',status:'queued',created:old})));
  const bearer=token(grants[2]);
  const exact=await body(await request('/v1/tasks/stats',bearer));
  assert.equal(exact.counts.reduce((n,c)=>n+c.count,0),1000);
  assert.equal(exact.stuck.queued_over_threshold,1000);
  await store.seed([{id:'synthetic-budget-over',owner:otherProject,provider:'dot',status:'queued',created:old}]);
  assert.equal((await request('/v1/tasks/stats',bearer)).status,400);
  assert.deepEqual((await body(await request('/v1/tasks/stats?since='+encodeURIComponent(iso(old+1)),bearer))).counts,[]);
});

// Liveness does not read task state or make provider requests.
test('health is minimal, origin-bound and read-only', async()=>{
  const reply=await request('/healthz',null);
  assert.deepEqual(await body(reply),{status:'ok'});
  assert.equal((await request('/healthz',null,{method:'POST'})).status,404);
  assert.equal((await worker.fetch('https://other.example.test/healthz')).status,421);
});
