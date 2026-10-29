import {imageMaterials} from './image-materials.mjs';
import {workerContext,workerProjects} from './projects.mjs';
import {eventCatalog} from './webhooks.mjs';
import {WorkerEntrypoint} from 'cloudflare:workers';
import {insufficientScope} from '@cloudflare/workers-oauth-provider';
import {parseJson, serialize, valueOf, field, nodeFrom, objectNode, replaceFields} from './canonical.mjs';
import {check, checkOutputSchema, outputValid} from './validation.mjs';
const fail=(name)=>{throw Object.assign(new Error(name),{name});};
const bad=()=>fail('ValueError'), denied=()=>fail('PermissionError');
const obj=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const json=(node,status=200)=>new Response(serialize(node),{status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
const ordinary=(v,status=200)=>Response.json(v,{status,headers:{'Cache-Control':'no-store'}});
const schema=(properties,required=Object.keys(properties))=>({type:'object',properties,required,additionalProperties:false});
const string={type:'string'}, fields={task_id:string,attempt_id:string,revision:{type:'integer',minimum:1},status:{enum:['completed','failed','cancelled']},output:{},artifacts:{type:'array',items:{type:'object'}}};
const EVENT_SCOPES=['tasks:work','events:subscribe'];
const securitySchemes=scopes=>[{type:'oauth2',scopes}];
const eventScopeChallenge=c=>insufficientScope({audience:c.identity.resource},EVENT_SCOPES,'Authorize event subscription access to continue.');
export const TOOLS=[
  ['get_task','Read assigned task, current materials, revision and cancellation state.',['task_id'],['task_id']],
  ['claim_task','Claim one execution attempt. Reuse the same attempt_id for retries.',['task_id','attempt_id'],['task_id','attempt_id']],
  ['report_task_result','Report a final advisory result for the claimed attempt and current revision.',Object.keys(fields),['task_id','attempt_id','revision','status']]
].map(([name,description,allowed,required])=>({name,description,inputSchema:schema(Object.fromEntries(allowed.map(k=>[k,fields[k]])),required),securitySchemes:securitySchemes(['tasks:work'])}));
const REQUEST=schema({mode:{enum:['agent','api']},profile:{type:'string',minLength:1,maxLength:100,pattern:'^[^:]+$'},objective:{type:'string',minLength:1},model:string,materials:{type:'array'},output_schema:{type:'object'},timeout_seconds:{type:'integer',minimum:1},issue:{type:'object'}},['mode','objective','model','output_schema']);
const MESSAGE=schema({text:string,materials:{type:'array'}},['text']);

export class TaskService extends WorkerEntrypoint {
  context(identity,request,selectedProject){
    if(!obj(identity)||identity.organization!==this.env.ORGANIZATION||!Array.isArray(identity.scopes)||
       !/^[1-9][0-9]*$/.test(identity.githubUserId??'')||typeof identity.clientId!=='string'||!identity.clientId||
       identity.provider!==null&&!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(identity.provider??''))denied();
    const map=this.env.PROJECT_CONTEXTS??{};
    const home=Object.hasOwn(map,identity.projectId)?map[identity.projectId]:null;
    if(!home)denied();
    const query=new URL(request.url).searchParams.getAll('project');
    if(query.length>1||selectedProject!==undefined&&query.length&&selectedProject!==query[0])denied();
    const c=workerContext(this.env,identity,selectedProject??query[0]);
    if(!c||c.organization!==identity.organization||c.ownerId!==identity.ownerId||identity.provider!==null&&!c.providers.includes(identity.provider))denied();
    const url=new URL(request.url),resource=url.pathname==='/mcp'?'/mcp':'/v1/tasks';
    if(url.origin!==this.env.PUBLIC_ORIGIN||identity.resource!==url.origin+resource)denied();
    const projects=url.searchParams.getAll('project');
    if(projects.length>1||projects.length===1&&projects[0]!==c.project)denied();
    if(identity.provider===null&&resource==='/mcp')denied();
    return {...c, provider:identity.provider,scopes:identity.scopes,identity,
      // Stable authority comes solely from the validated grant, never tool arguments.
      worker:`github-${identity.githubUserId}:${identity.clientId}`};
  }
  store(){return this.env.TASK_STATE.get(this.env.TASK_STATE.idFromName(this.env.TASK_STORE_NAME));}
  scope(c,s){if(!c.scopes.includes(s))denied();}
  async snapshot(c,id,worker=false,withMessages=false){
    const snap=await this.store().getCanonical(c.project,id);
    if(worker&&snap.record.provider!==c.provider)denied();
    const replacements={request:parseJson(snap.request_json)};
    if(snap.record.output!==null&&snap.report_json)replacements.output=field(parseJson(snap.report_json),'output')??nodeFrom(null);
    if(snap.report_json)replacements.artifacts=field(parseJson(snap.report_json),'artifacts')??nodeFrom([]);
    if(withMessages)replacements.messages={kind:'array',entries:snap.messages.map(m=>objectNode([['revision',nodeFrom(m.revision)],['created',nodeFrom(m.created)],...parseJson(m.body).entries]))};
    const record={...snap.record};
    if(obj(record.handle)){record.handle={...record.handle};delete record.handle.output;}
    return replaceFields(nodeFrom(record),replacements);
  }
  async handleAuthenticated(request,identity){
    let mcp=false;
    try{
      const url=new URL(request.url);mcp=url.pathname==='/mcp';const c=this.context(identity,request);
      if(mcp){if(request.method!=='POST')return ordinary({error:'method_not_allowed'},405);this.scope(c,'tasks:work');return await this.mcp(request,c);}
      if(!url.pathname.startsWith('/v1/tasks'))return ordinary({error:'not_found'},404);
      const store=this.store(), path=url.pathname, key=request.headers.get('Idempotency-Key');
      if(request.method==='GET'){
        this.scope(c,'tasks:read');if(c.provider!==null)this.scope(c,'tasks:work');if((await request.text()).length)bad();
        if(path==='/v1/tasks/lookup'){
          const digest=request.headers.get('X-Request-SHA256');if(!/^[a-f0-9]{64}$/.test(digest??''))bad();
          try{const r=await store.lookup(c.project,key,digest);await this.snapshot(c,r.id,c.provider!==null);return ordinary({id:r.id});}
          catch(e){if(['NotFound','PermissionError'].includes(e.name))fail('NotFound');throw e;}
        }
        const match=path.match(/^\/v1\/tasks\/([^/]+)(\/events)?$/);if(!match)return ordinary({error:'not_found'},404);
        const id=decodeURIComponent(match[1]);await this.snapshot(c,id,c.provider!==null);
        if(match[2]){const after=url.searchParams.get('after')??'0';if(!/^\d+$/.test(after)||!Number.isSafeInteger(Number(after)))bad();return ordinary({events:await store.events(c.project,id,Number(after))});}
        return json(await this.snapshot(c,id,c.provider!==null,true));
      }
      if(request.method==='POST'){
        this.scope(c,'tasks:submit');const body=parseJson(await request.text()),value=valueOf(body);
        if(path==='/v1/tasks'){
          await check(REQUEST,value);
          const timeoutNode=field(body,'timeout_seconds');
          if(timeoutNode&&/[.eE]/.test(timeoutNode.raw))bad(); // Python explicitly requires int, not 1.0.
          await checkOutputSchema(value.output_schema);
          const r=await store.submit(c.project,key,serialize(body));return json(await this.snapshot(c,r.id),202);
        }
        const match=path.match(/^\/v1\/tasks\/([^/]+)\/(messages|cancel)$/);if(!match)return ordinary({error:'not_found'},404);
        const id=decodeURIComponent(match[1]);
        if(match[2]==='cancel'){await check(schema({}),value);await store.cancel(c.project,id);return json(await this.snapshot(c,id));}
        await check(MESSAGE,value);await store.message(c.project,id,key,serialize(body));return json(await this.snapshot(c,id));
      }
      return ordinary({error:'method_not_allowed'},405);
    }catch(e){const status={ValueError:400,ValidationError:400,Conflict:409,PermissionError:403,NotFound:404}[e.name]??500;
      return ordinary({error:status===500?'internal_error':status===409?'task conflict':status===403?'access denied':status===404?'task not found':'invalid arguments'},status);
    }
  }
  tools(c){
    const projects=workerProjects(this.env,c.identity);
    const scoped=tool=>projects.length<2?tool:{...tool,inputSchema:{...tool.inputSchema,properties:{...tool.inputSchema.properties,project:{type:'string',enum:projects}}}};
    if(c.provider!=='dot')return TOOLS.map(scoped);
    return [...TOOLS,...['get_event_subscription','delete_event_subscription'].map(name=>({name,description:name==='get_event_subscription'?'Read owned subscription lifecycle.':'Delete owned subscription and stop delivery.',inputSchema:schema({subscription_id:{type:'string',minLength:1,maxLength:200}}),securitySchemes:securitySchemes(EVENT_SCOPES)}))].map(scoped);
  }
  async mcp(request,c){
    const root=parseJson(await request.text()),message=valueOf(root);
    if(!obj(message)||message.jsonrpc!=='2.0')bad();
    if(!Object.hasOwn(message,'id')){
      if(message.method!=='notifications/initialized'||!obj(message.params??{})||Object.hasOwn(message.params??{},'_meta')&&!obj(message.params._meta))bad();
      return new Response(null,{status:202});
    }
    const idNode=field(root,'id');
    try{
      let params=message.params??{},paramsNode=field(root,'params')??nodeFrom({});
      if(!obj(params))bad();if(Object.hasOwn(params,'_meta')){if(!obj(params._meta))bad();params={...params};delete params._meta;paramsNode=objectNode(paramsNode.entries.filter(([k])=>k!=='_meta'));}
      let result;
      switch(message.method){
        case 'initialize':if(typeof params.protocolVersion!=='string'||!obj(params.capabilities)||!obj(params.clientInfo))bad();result=nodeFrom({protocolVersion:['2025-03-26','2025-06-18','2025-11-25'].includes(params.protocolVersion)?params.protocolVersion:'2025-11-25',capabilities:{tools:{}},serverInfo:{name:c.organization==='pigbibi'?'personal-ai-service':'quant-ai-service',version:'2.0.14-cloudflare.1'}});break;
        case 'ping':result=nodeFrom({});break;
        case 'server/discover':result=nodeFrom({resultType:'complete',supportedVersions:['2026-07-28'],capabilities:{tools:{},...(c.provider==='dot'?{events:{}}:{})}});break;
        // Discovery describes the Dot capability; the token must still authorize its use.
        case 'events/list':case 'events/subscribe':case 'events/unsubscribe':{
          if(c.provider!=='dot')denied();
          if(!c.scopes.includes('events:subscribe'))return eventScopeChallenge(c);
          if(message.method!=='events/list')c=this.context(c.identity,request,params.arguments?.project);
          result=nodeFrom(message.method==='events/list'?eventCatalog():message.method==='events/subscribe'?await this.store().subscribe(c.project,c.worker,c.identity,params):await this.store().unsubscribeRequest(c.project,c.worker,params));break;
        }
        case 'tools/list':result=nodeFrom({tools:this.tools(c)});break;
        case 'tools/call':{
          await check(schema({name:string,arguments:{type:'object'}}),params);
          const tool=this.tools(c).find(t=>t.name===params.name);if(!tool)bad();await check(tool.inputSchema,params.arguments);
          const a=params.arguments;
          c=this.context(c.identity,request,a.project??c.project);
          if(['get_event_subscription','delete_event_subscription'].includes(params.name)){
            if(!c.scopes.includes('events:subscribe')){
              result=nodeFrom({content:[{type:'text',text:'Authorize event subscription access to continue.'}],isError:true,_meta:{'mcp/www_authenticate':[eventScopeChallenge(c).headers.get('WWW-Authenticate')]}});break;
            }
            const store=this.store();
            const value=params.name==='get_event_subscription'?await store.subscriptionStatus(c.project,c.worker,a.subscription_id):await store.unsubscribe(c.project,c.worker,a.subscription_id);
            result=nodeFrom({content:[{type:'text',text:JSON.stringify(value)}],structuredContent:value,isError:false});break;
          }
          const store=this.store();
          // Existing native-host tools may cache the single-project input schema.
          // Resolve an event task only inside the worker's explicit, server-controlled list.
          if(a.project===undefined){const project=await store.projectForTask(workerProjects(this.env,c.identity),a.task_id);c=this.context(c.identity,request,project);}
          await this.snapshot(c,a.task_id,true);
          if(params.name==='claim_task')await store.claim(c.project,a.task_id,c.provider,c.worker,a.attempt_id);
          if(params.name==='report_task_result'){
            const snap=await store.getCanonical(c.project,a.task_id);
            const argsNode=field(paramsNode,'arguments');
            if(/[.eE]/.test(field(argsNode,'revision').raw))fail('Conflict');
            const output=field(argsNode,'output')??nodeFrom(null),artifacts=field(argsNode,'artifacts')??nodeFrom([]);
            // The client cannot supply this prepared verdict. Validate the frozen request schema here.
            const valid=a.status!=='completed'||await outputValid(JSON.parse(snap.request_json).output_schema,valueOf(output));
            const report=objectNode([['revision',field(argsNode,'revision')],['status',field(argsNode,'status')],['output',output],['artifacts',artifacts]]);
            await store.commitReport(c.project,a.task_id,c.provider,c.worker,a.attempt_id,{body:serialize(report),terminal_status:valid?a.status:'invalid_output'});
          }
          let record=await this.snapshot(c,a.task_id,true,params.name!=='report_task_result');
          if(params.name==='claim_task')record=replaceFields(record,{attempt_id:nodeFrom(a.attempt_id)});
          result=objectNode([['content',nodeFrom([{type:'text',text:serialize(record)},...(params.name==='get_task'?imageMaterials(valueOf(record).request?.materials??[]):[])])],['structuredContent',record],['isError',nodeFrom(false)]]);break;
        }
        default:return ordinary({jsonrpc:'2.0',id:message.id,error:{code:-32601,message:'method not found'}});
      }
      return json(objectNode([['jsonrpc',nodeFrom('2.0')],['id',idNode],['result',result]]));
    }catch(e){if(e.name==='CallbackEndpointError')return ordinary({jsonrpc:'2.0',id:message.id,error:{code:-32015,message:'CallbackEndpointError',data:{reason:e.message==='transport_failed'?'transport_failed':'challenge_failed'}}});
      const conflict=e.name==='Conflict',permission=e.name==='PermissionError';if(!['ValueError','ValidationError','Conflict','PermissionError','NotFound'].includes(e.name))throw e;
      return ordinary({jsonrpc:'2.0',id:message.id,error:{code:conflict?-32009:permission?-32003:-32602,message:conflict?'task conflict':permission?'access denied':'invalid arguments'}});
    }
  }
}
