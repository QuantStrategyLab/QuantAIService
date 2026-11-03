// Local harness only: official client/grant helpers. Never bundled into production.
import {WorkerEntrypoint} from 'cloudflare:workers';
import {configuration,createAuthApplication} from '../src/auth.mjs';
export {TaskService} from '../src/task-service.mjs';
import {PigbibiTaskStore,QuantTaskStore} from '../src/state.mjs';
const observed=Base=>class extends Base {
  constructor(ctx,env){
    const kv=env.OAUTH_KV;
    super(ctx,{...env,OAUTH_KV:{list:kv.list.bind(kv),delete:kv.delete.bind(kv),get:async(key,options)=>{
      if(key.startsWith('grant:')){
        const mode=await kv.get('fixture-grant-mode');
        if(mode==='unavailable')throw new Error('synthetic grant read failure');
        if(mode==='invalid')return {};
        const grant=await kv.get(key,options);
        if(grant&&mode==='expired')return {...grant,expiresAt:Date.now()/1000-1};
        if(grant&&mode==='scope')return {...grant,scope:['tasks:read','tasks:work']};
        if(grant&&['id','userId','clientId','resource'].includes(mode))return {...grant,[mode]:'synthetic-mismatch'};
        return grant;
      }
      return kv.get(key,options);
    }}});
  }

  async alarm(){this.testAlarmCount=(this.testAlarmCount??0)+1;return super.alarm();}
  async setSubscriptionExpiry(id,epochSeconds){this.sql.exec('UPDATE subscriptions SET expires=? WHERE id=?',epochSeconds,id);}
  async setSubscriptionGrant(id,ttl){this.sql.exec('UPDATE subscriptions SET ttl_seconds=? WHERE id=?',ttl,id);}
  async legacyIdentity(id){const row=this.sql.exec('SELECT identity_json FROM subscriptions WHERE id=?',id).one();const identity=JSON.parse(row.identity_json);delete identity.grantId;this.sql.exec('UPDATE subscriptions SET identity_json=? WHERE id=?',JSON.stringify(identity),id);}
  async fixtureTaskStatus(owner,id){return this.get(owner,id).status;}
  async subscriptionLease(id){return this.sql.exec('SELECT expires,ttl_seconds FROM subscriptions WHERE id=?',id).one();}
  async signingBoundary(id,action){
    globalThis.subscriptionSigningBoundary=async()=>{
      delete globalThis.subscriptionSigningBoundary;
      if(action==='expire')this.sql.exec('UPDATE subscriptions SET expires=?,non_expiring=0 WHERE id=?',Date.now()/1000-1,id);
      else if(action==='delete')this.sql.exec('UPDATE subscriptions SET deleted=? WHERE id=?',Date.now()/1000,id);
      else {
        const identity=JSON.parse(this.sql.exec('SELECT identity_json FROM subscriptions WHERE id=?',id).one().identity_json);
        const keys=await this.env.OAUTH_KV.list({prefix:'grant:'});
        for(const key of keys.keys)if(key.name.endsWith(':'+identity.grantId))await this.env.OAUTH_KV.delete(key.name);
      }
    };
  }
  async probeTick(){return this.alarm();}
  async alarmStats(){const scheduled=await this.ctx.storage.getAlarm();return {count:this.testAlarmCount??0,scheduledDelayMs:scheduled===null?null:scheduled-Date.now()};}
  async subscriptionVisibility(owner,principal,otherPrincipal,id){
    const own=(await this.listSubscriptions(owner,principal)).subscriptions.map(row=>row.id);
    const other=(await this.listSubscriptions(owner,otherPrincipal)).subscriptions.map(row=>row.id);
    let denied=false;
    try{await this.subscriptionStatus(owner,otherPrincipal,id);}catch(error){denied=error.name==='PermissionError';}
    return {own,other,denied};
  }
  async subscriptionCount(){return this.sql.exec('SELECT COUNT(*) AS n FROM subscriptions').one().n;}
  async seedDeliveryBatch(id,initialFailures){
    this.sql.exec('UPDATE subscriptions SET consecutive_failures=? WHERE id=?',initialFailures,id);
    this.sql.exec('DELETE FROM event_deliveries WHERE subscription=?',id);
    for(const seq of [-2,-1])this.sql.exec('INSERT INTO event_deliveries(subscription,seq,body) VALUES(?,?,?)',id,seq,JSON.stringify({name:'task.requested',data:{task_id:'11111111-1111-4111-8111-111111111111'}}));
    await this.ctx.storage.setAlarm(Date.now()+10);
  }
  async fillDeliveryQueue(id,count=128){
    // Far-future next_attempt keeps capacity full without running 128 outbound POSTs in one alarm.
    this.sql.exec('DELETE FROM event_deliveries WHERE subscription=?',id);
    const later=Date.now()/1000+86400;
    for(let i=0;i<count;i++)this.sql.exec("INSERT INTO event_deliveries(subscription,seq,body,status,next_attempt) VALUES(?,?,?,'pending',?)",id,-1000-i,JSON.stringify({name:'task.requested',data:{task_id:'11111111-1111-4111-8111-111111111111'}}),later);
  }
  async subscriptionRow(id){
    return this.sql.exec('SELECT id,start_seq,ttl_seconds,non_expiring,expires,deleted FROM subscriptions WHERE id=?',id).toArray()[0]??null;
  }
  async subscriptionColumns(){
    return this.sql.exec('PRAGMA table_info(subscriptions)').toArray().map(column=>column.name);
  }
  async expireSubscription(id){
    this.sql.exec('UPDATE subscriptions SET expires=?,non_expiring=0,ttl_seconds=1 WHERE id=?',Date.now()/1000-1,id);
  }
  async staleUnclaimed(owner,id){this.sql.exec("UPDATE tasks SET status='cancel_requested',error='deadline_exceeded',deadline=? WHERE id=? AND owner=?",Date.now()/1000-1,id,owner);await this.ctx.storage.setAlarm(Date.now()+10);}
  async expireAPI(owner,id){this.sql.exec('UPDATE tasks SET deadline=? WHERE id=? AND owner=?',Math.floor(Date.now()/1000)-1,id,owner);await this.ctx.storage.setAlarm(Date.now()+10);}
};
export class PersonalFixtureStore extends observed(PigbibiTaskStore){}
export class QuantFixtureStore extends observed(QuantTaskStore){}
export default class Fixture extends WorkerEntrypoint {
  async seedDeliveryBatch(id,initialFailures){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).seedDeliveryBatch(id,initialFailures);}
  async fillDeliveryQueue(id,count){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).fillDeliveryQueue(id,count);}
  async subscriptionRow(id){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).subscriptionRow(id);}
  async subscriptionColumns(){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).subscriptionColumns();}
  async expireSubscription(id){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).expireSubscription(id);}
  async subscriptionCount(){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).subscriptionCount();}
  async eventLogs(){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).recentEventLogs();}
  async subscriptionVisibility(owner,principal,otherPrincipal,id){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).subscriptionVisibility(owner,principal,otherPrincipal,id);}
  async projectTask(owner,request){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.submit(owner,'second-project-synthetic',JSON.stringify(request));}
  async nativeClaim(owner,id,provider){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.claim(owner,id,provider,'synthetic-worker','synthetic-attempt');}
  async nativeReport(owner,id,provider,revision){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.commitReport(owner,id,provider,'synthetic-worker','synthetic-attempt',{body:JSON.stringify({revision,status:'cancelled',output:null,artifacts:[]}),terminal_status:'cancelled'});}
  async alarmProbe(action,owner,id){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));if(action==='expire')await store.expireAPI(owner,id);else if(action==='stale-unclaimed')await store.staleUnclaimed(owner,id);else if(action==='tick')await store.probeTick();return store.alarmStats();}
  async abandonAPI(owner,request,cancel=false){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));const task=await store.submit(owner,'fixture-abandoned-api-'+cancel,JSON.stringify(request));await store.beginSubmission(owner,task.id);if(cancel)await store.cancel(owner,task.id);return task.id;}
  async setSubscriptionExpiry(id,epochSeconds){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));await store.setSubscriptionExpiry(id,epochSeconds);}
  async setSubscriptionGrant(id,ttl){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));await store.setSubscriptionGrant(id,ttl);}
  async subscriptionLease(id){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.subscriptionLease(id);}
  async fixtureTaskStatus(owner,id){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.fixtureTaskStatus(owner,id);}
  async legacyIdentity(id){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));await store.legacyIdentity(id);}
  async grantReadMode(mode){await this.env.OAUTH_KV.put('fixture-grant-mode',mode);}
  async revokeOAuthGrants(githubUserId,clientId){
    const oauth=createAuthApplication(configuration(this.env)).authorizationServer.getOAuthApi(this.env);
    const userId='github-'+githubUserId;
    const grants=await oauth.listUserGrants(userId);
    let count=0;
    for(const grant of grants.items)if(grant.clientId===clientId){await oauth.revokeGrant(grant.id,userId);count++;}
    return count;
  }
  async signingBoundary(id,action){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).signingBoundary(id,action);}
  async client(redirect='https://chatgpt.com/connector_platform_oauth_redirect'){
    const app=createAuthApplication(configuration(this.env));return app.authorizationServer.getOAuthApi(this.env).createClient({clientName:'Local protocol test',redirectUris:[redirect],tokenEndpointAuthMethod:'none',grantTypes:['authorization_code','refresh_token'],responseTypes:['code']});
  }
  async policy(clients){await this.env.OAUTH_KV.put('fixture-policy',JSON.stringify(clients));}
  async grant({clientId,scope,resource,challenge,provider='dot',githubUserId='10001',organization=this.env.ORGANIZATION,projectId=this.env.PROJECT_ID,ownerId=this.env.ORGANIZATION_ID}){
    const oauth=createAuthApplication(configuration(this.env)).authorizationServer.getOAuthApi(this.env);
    const q=new URLSearchParams({response_type:'code',client_id:clientId,redirect_uri:'http://localhost:8989/callback',state:'fixture',scope:scope.join(' '),resource,code_challenge:challenge,code_challenge_method:'S256'});
    const parsed=await oauth.parseAuthRequest(new Request(this.env.PUBLIC_ORIGIN+'/authorize?'+q));
    const result=await oauth.completeAuthorization({request:parsed,userId:'github-'+githubUserId,scope,metadata:{synthetic:true},props:{organization,projectId,ownerId,githubUserId,provider}});
    return new URL(result.redirectTo).searchParams.get('code');
  }
  async fetch(request){
    const config=JSON.parse(await this.env.OAUTH_KV.get('fixture-policy')??'{}');
    const env={...this.env,...config};
    return createAuthApplication(configuration(env)).fetch(request,env,this.ctx);
  }
}
