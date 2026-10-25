// Approved remote operation: only two public client records, no grants or tokens.
import {Miniflare} from 'miniflare';
import {build} from 'esbuild';
import {mkdir,readFile,writeFile,access} from 'node:fs/promises';
import {spawn} from 'node:child_process';
const args=process.argv.slice(2),remote=args.includes('--remote');
const configIndex=args.indexOf('--config');if(configIndex<0||!args[configIndex+1])throw Error('Explicit private JSON config required');
const config=JSON.parse(await readFile(args[configIndex+1],'utf8'));
if(!['pigbibi','quant'].includes(config.organization)||!config.PROJECT_ID||config.PROJECT_ID.startsWith('CONFIGURE_')||new URL(config.PUBLIC_ORIGIN).origin!==config.PUBLIC_ORIGIN)throw Error('Invalid explicit organization/project/origin');
const index=args.indexOf('--namespace-id'),namespace=index<0?'':args[index+1];
if(remote&&!/^[a-f0-9]{32}$/.test(namespace))throw Error('exact approved OAuth namespace id required');
await mkdir('.local',{recursive:true});
try{await access('.local/client-registration.json');throw Error('Existing registration plan: read back instead of generating or blindly retrying.');}catch(e){if(e.code!=='ENOENT')throw e;}
const code=(await build({entryPoints:['test/bootstrap-clients.mjs'],bundle:true,format:'esm',platform:'browser',write:false,external:['cloudflare:*']})).outputFiles[0].text;
const mf=new Miniflare({cf:false,modules:true,script:code,compatibilityDate:'2026-07-30',kvNamespaces:['OAUTH_KV'],bindings:{ORGANIZATION:config.organization,PROJECT_ID:config.PROJECT_ID,PUBLIC_ORIGIN:config.PUBLIC_ORIGIN},outboundService:()=>{throw Error('no network during registration generation');}});
try{
 const entries=await (await mf.getWorker()).register();
 for(const item of entries){const record=JSON.parse(item.record);if(record.tokenEndpointAuthMethod!=='none'||record.clientSecret||item.client.clientSecret)throw Error('Only public clients admitted');}
 await writeFile('.local/client-registration.json',JSON.stringify({namespace:remote?namespace:null,entries,remoteState:'not_written'},null,2)+'\n',{mode:0o600,flag:'wx'});
 for(const item of entries){await writeFile('.local/client-'+item.role+'.json',item.record,{mode:0o600,flag:'wx'});}
 if(remote){
  for(const item of entries){
   const exit=await new Promise((resolve,reject)=>{const p=spawn('wrangler',['kv','key','put','client:'+item.client.clientId,'--namespace-id',namespace,'--path','.local/client-'+item.role+'.json','--remote'],{stdio:['ignore','pipe','pipe']});p.stdout.resume();p.stderr.resume();p.on('error',reject);p.on('close',resolve);});
   if(exit!==0)throw Error('KV client write failed or outcome unknown. Stop; read back exact client key before any retry.');
  }
 }
 console.log(JSON.stringify({remote,clients:entries.map(({role,client})=>({role,clientId:client.clientId,redirectUris:client.redirectUris,tokenEndpointAuthMethod:client.tokenEndpointAuthMethod})),grantsIssued:0}));
}finally{await mf.dispose();}
