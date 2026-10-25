// Executes only under local workerd; never imported by the production Worker.
import {WorkerEntrypoint} from 'cloudflare:workers';
import {configuration,createAuthApplication} from '../src/auth.mjs';
export default class ClientRegistrar extends WorkerEntrypoint {
  async register(){
    const oauth=createAuthApplication(configuration(this.env)).authorizationServer.getOAuthApi(this.env);
    const definitions=[{role:'dot',clientName:'AI task service - ChatGPT Dot',redirectUris:['https://chatgpt.com/connector_platform_oauth_redirect']},{role:'caller',clientName:'AI task service - local TaskClient',redirectUris:['http://localhost:8989/callback']}];
    const result=[];
    for(const definition of definitions){const {role,...metadata}=definition;const client=await oauth.createClient({...metadata,tokenEndpointAuthMethod:'none',grantTypes:['authorization_code','refresh_token'],responseTypes:['code']});result.push({role,client,record:await this.env.OAUTH_KV.get('client:'+client.clientId)});}
    return result;
  }
  fetch(){return new Response(null,{status:404});}
}
