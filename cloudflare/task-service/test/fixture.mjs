// Local harness only: official client/grant helpers. Never bundled into production.
import {WorkerEntrypoint} from 'cloudflare:workers';
import {configuration,createAuthApplication} from '../src/auth.mjs';
export {TaskService} from '../src/task-service.mjs';
import {PigbibiTaskStore,QuantTaskStore} from '../src/state.mjs';
const observed=Base=>class extends Base {
  async alarm(){this.testAlarmCount=(this.testAlarmCount??0)+1;return super.alarm();}
  async probeTick(){return this.alarm();}
  async alarmStats(){const scheduled=await this.ctx.storage.getAlarm();return {count:this.testAlarmCount??0,scheduledDelayMs:scheduled===null?null:scheduled-Date.now()};}
  async subscriptionCount(){return this.sql.exec('SELECT COUNT(*) AS n FROM subscriptions').one().n;}
  async staleUnclaimed(owner,id){this.sql.exec("UPDATE tasks SET status='cancel_requested',error='deadline_exceeded',deadline=? WHERE id=? AND owner=?",Date.now()/1000-1,id,owner);await this.ctx.storage.setAlarm(Date.now()+10);}
  async expireAPI(owner,id){this.sql.exec('UPDATE tasks SET deadline=? WHERE id=? AND owner=?',Math.floor(Date.now()/1000)-1,id,owner);await this.ctx.storage.setAlarm(Date.now()+10);}
};
export class PersonalFixtureStore extends observed(PigbibiTaskStore){}
export class QuantFixtureStore extends observed(QuantTaskStore){}
export default class Fixture extends WorkerEntrypoint {
  async subscriptionCount(){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME)).subscriptionCount();}
  async projectTask(owner,request){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.submit(owner,'second-project-synthetic',JSON.stringify(request));}
  async nativeClaim(owner,id,provider){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.claim(owner,id,provider,'synthetic-worker','synthetic-attempt');}
  async nativeReport(owner,id,provider,revision){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));return store.commitReport(owner,id,provider,'synthetic-worker','synthetic-attempt',{body:JSON.stringify({revision,status:'cancelled',output:null,artifacts:[]}),terminal_status:'cancelled'});}
  async alarmProbe(action,owner,id){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));if(action==='expire')await store.expireAPI(owner,id);else if(action==='stale-unclaimed')await store.staleUnclaimed(owner,id);else if(action==='tick')await store.probeTick();return store.alarmStats();}
  async abandonAPI(owner,request,cancel=false){const store=this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));const task=await store.submit(owner,'fixture-abandoned-api-'+cancel,JSON.stringify(request));await store.beginSubmission(owner,task.id);if(cancel)await store.cancel(owner,task.id);return task.id;}
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
