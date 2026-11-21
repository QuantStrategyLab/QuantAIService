// Server-configured terminal hints. Consumers dedupe and read authoritative tasks.
import {callbackURL} from './webhooks.mjs';
const terminal=new Set(['completed','failed','cancelled','invalid_output']);
const routeKeys=new Set(['enabled','profile','secret_env','url','timeout_ms']);
const defaultTimeout=2000;
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const bytes=v=>new TextEncoder().encode(v);

export class CompletionWebhooks {
  constructor(sql,env){
    this.sql=sql;this.routes=new Map();
    const configured=env.COMPLETION_WEBHOOKS??{};
    if(!object(configured))throw new TypeError('invalid completion webhook configuration');
    for(const [project,route] of Object.entries(configured)){
      if(!Object.hasOwn(env.PROJECT_POLICIES,project)||!object(route)||
          Object.keys(route).some(key=>!routeKeys.has(key))||typeof route.enabled!=='boolean'||
          typeof route.profile!=='string'||!route.profile||route.profile.length>100||route.profile.includes(':')){
        throw new TypeError('completion webhook project or route is not admitted');
      }
      callbackURL(route.url);
      if(new URL(route.url).search||typeof route.secret_env!=='string'||!route.secret_env)throw new TypeError('invalid completion webhook destination');
      const timeout=Object.hasOwn(route,'timeout_ms')?route.timeout_ms:defaultTimeout;
      if(!Number.isInteger(timeout)||timeout<defaultTimeout||timeout>25000)throw new TypeError('invalid completion webhook timeout');
      if(!route.enabled)continue;
      const secret=env[route.secret_env];
      if(typeof secret!=='string'||bytes(secret).length<32)throw new TypeError('protected completion webhook secret required');
      this.routes.set(project,{url:route.url,secret,profile:route.profile,timeout,identity:JSON.stringify([project,route.profile,route.url,route.secret_env])});
    }
    if(this.routes.size)this.sql.exec(`CREATE TABLE IF NOT EXISTS completion_deliveries (
      route TEXT NOT NULL, seq INTEGER NOT NULL, owner TEXT NOT NULL, body TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(route,seq));`);
  }

  enqueue(task,status,eventId){
    const route=this.routes.get(task.owner);
    if(!route||route.profile!==task.profile||!terminal.has(status))return;
    const body=JSON.stringify({name:'task.terminal',event_id:eventId,task_id:task.id,
      project:task.owner,revision:task.revision,status});
    // Called in the same SQLite transaction as the terminal task/event write.
    this.sql.exec('INSERT OR IGNORE INTO completion_deliveries(route,seq,owner,body) VALUES(?,?,?,?)',
      route.identity,eventId,task.owner,body);
  }

  nextAttempt(){
    if(!this.routes.size)return null;
    const identities=[...this.routes.values()].map(r=>r.identity),marks=identities.map(()=>'?').join(',');
    return this.sql.exec(`SELECT MIN(next_attempt) AS n FROM completion_deliveries
      WHERE status='pending' AND route IN (${marks})`,...identities).one().n;
  }

  async flush(){
    if(!this.routes.size)return null;
    const identities=[...this.routes.values()].map(r=>r.identity),marks=identities.map(()=>'?').join(',');
    const rows=this.sql.exec(`SELECT * FROM completion_deliveries WHERE status='pending' AND next_attempt<=?
      AND route IN (${marks}) ORDER BY seq LIMIT 1`,Date.now()/1000,...identities).toArray();
    for(const row of rows){
      const route=this.routes.get(row.owner);if(!route||route.identity!==row.route)continue;
      if(row.attempts>=5){
        this.sql.exec("UPDATE completion_deliveries SET status='stopped' WHERE route=? AND seq=?",row.route,row.seq);
        continue;
      }
      const stamp=String(Math.floor(Date.now()/1000));
      const key=await crypto.subtle.importKey('raw',bytes(route.secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);
      const mac=new Uint8Array(await crypto.subtle.sign('HMAC',key,bytes(stamp+'.'+row.body)));
      const signature='sha256='+Array.from(mac,b=>b.toString(16).padStart(2,'0')).join('');
      const attempts=row.attempts+1;
      // Persist the delivery budget before sending, including crash/lost-reply cases.
      this.sql.exec('UPDATE completion_deliveries SET attempts=?,next_attempt=? WHERE route=? AND seq=?',
        attempts,Date.now()/1000+Math.min(2**attempts,60),row.route,row.seq);
      let code=0;
      try{
        const reply=await fetch(route.url,{method:'POST',headers:{'Content-Type':'application/json',
          'X-Callback-Timestamp':stamp,'X-Callback-Signature':signature},body:row.body,
          redirect:'manual',signal:AbortSignal.timeout(route.timeout)});
        code=reply.status;await reply.body?.cancel();
      }catch{}
      const transient=code===0||code===429||code>=500;
      const state=code>=200&&code<300?'sent':transient&&attempts<5?'pending':'stopped';
      this.sql.exec('UPDATE completion_deliveries SET status=?,attempts=?,next_attempt=? WHERE route=? AND seq=?',
        state,attempts,Date.now()/1000+Math.min(2**attempts,60),row.route,row.seq);
    }
    return this.nextAttempt();
  }
}
