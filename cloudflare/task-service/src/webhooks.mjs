// Port of existing events.py Standard Webhooks HMAC contract; no task material in triggers.
export function callbackURL(value) {
  if(typeof value!=='string')throw new TypeError('invalid callback');
  const u=new URL(value);
  if(u.protocol!=='https:'||u.username||u.password||u.hash||u.hostname==='localhost'||/\.(localhost|local|internal)$/.test(u.hostname)||/^\d+\.\d+\.\d+\.\d+$/.test(u.hostname)||u.hostname.startsWith('['))throw new TypeError('invalid callback');
  return u.href;
}
export function signingKey(secret) {
  if(typeof secret!=='string'||!secret.startsWith('whsec_'))throw new TypeError('invalid webhook secret');
  let key;try{key=Uint8Array.from(atob(secret.slice(6)),c=>c.charCodeAt(0));}catch{throw new TypeError('invalid webhook secret');}
  if(key.length<24||key.length>64)throw new TypeError('invalid webhook secret');return key;
}
export async function signedHeaders(subscription,secret,id,body,previous=null) {
  const timestamp=String(Math.floor(Date.now()/1000)),payload=new TextEncoder().encode(`${id}.${timestamp}.${body}`),signatures=[];
  for(const value of [...new Set([secret,previous].filter(Boolean))]){
    const key=await crypto.subtle.importKey('raw',signingKey(value),{name:'HMAC',hash:'SHA-256'},false,['sign']);
    const sig=new Uint8Array(await crypto.subtle.sign('HMAC',key,payload));
    signatures.push('v1,'+btoa(String.fromCharCode(...sig)));
  }
  return {'Content-Type':'application/json','webhook-id':id,'webhook-timestamp':timestamp,'webhook-signature':signatures.join(' '),'X-MCP-Subscription-Id':subscription};
}
export const eventCatalog=()=>({events:[{name:'task.requested',description:'A task was requested, updated or cancelled in this project.',delivery:['webhook'],inputSchema:{type:'object',properties:{project:{type:'string'}},required:['project'],additionalProperties:false},payloadSchema:{type:'object',properties:{task_id:{type:'string'},project:{type:'string'},revision:{type:'integer'},change:{type:'string'}},required:['task_id','project','revision','change'],additionalProperties:false}}]});
