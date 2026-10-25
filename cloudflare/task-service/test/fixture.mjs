// Local harness only: official client/grant helpers. Never bundled into production.
import {WorkerEntrypoint} from 'cloudflare:workers';
import {configuration,createAuthApplication} from '../src/auth.mjs';
export {TaskService} from '../src/task-service.mjs';
export {PigbibiTaskStore as PersonalFixtureStore,QuantTaskStore as QuantFixtureStore} from '../src/state.mjs';
export default class Fixture extends WorkerEntrypoint {
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
