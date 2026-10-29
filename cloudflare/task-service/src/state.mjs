import {workerContext} from './projects.mjs';
import {callbackURL,signingKey,signedHeaders} from './webhooks.mjs';
import {apiConfiguration,executeAPI} from './api.mjs';
import {nativeConfiguration,triggerNative} from './native.mjs';
import {outputValid} from './validation.mjs';
import {serialize,valueOf,objectNode,nodeFrom} from './canonical.mjs';
import { DurableObject } from "cloudflare:workers";

// M1 is a storage capability, not an HTTP API or an authentication layer.
export default {
  fetch() {
    return Response.json({ error: "not_found" }, { status: 404 });
  },
};

const TERMINAL = new Set(["completed", "failed", "invalid_output", "cancelled"]);
const fail = (name, message) => { throw Object.assign(new Error(message), { name }); };
const conflict = (message) => fail("Conflict", message);
const invalid = (message) => fail("ValueError", message);
const denied = (message) => fail("PermissionError", message);
const missing = () => fail("NotFound", "task not found");
const now = () => Date.now() / 1000;
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const bytes = (value) => new TextEncoder().encode(value);

async function digest(body) {
  const hash = await crypto.subtle.digest("SHA-256", bytes(body));
  return Array.from(new Uint8Array(hash), (part) => part.toString(16).padStart(2, "0")).join("");
}

function parseBody(body) {
  if (typeof body !== "string") invalid("a prepared canonical JSON body is required");
  try { return JSON.parse(body); } catch { invalid("invalid JSON body"); }
}

function key(value, label = "Idempotency-Key is required (1..200 characters)") {
  if (typeof value !== "string" || Array.from(value).length < 1 || Array.from(value).length > 200) invalid(label);
}

function noExternalReferences(value) {
  if (!object(value) && !Array.isArray(value)) return;
  for (const [name, item] of Object.entries(value)) {
    if (["$ref", "$dynamicRef"].includes(name) && (typeof item !== "string" || !item.startsWith("#"))) {
      invalid("external schema references are forbidden");
    }
    noExternalReferences(item);
  }
}

// Trusted callers must perform the full existing Python contract validation
// (including Draft 2020-12) BEFORE invoking this capability. Canonical JSON bytes
// come from ai_service.runtime._json; JavaScript JSON.stringify is not equivalent
// for Python floats such as 1.0. No external caller may prepare its own verdict.
export class TaskStore extends DurableObject {
  constructor(ctx, env, organization) {
    super(ctx, env);
    if (env.ORGANIZATION_ID !== organization) denied("namespace organization mismatch");
    this.policies = env.PROJECT_POLICIES;
    if (!object(this.policies)) denied("project policies are not configured");
    this.sql = ctx.storage.sql;
    this.env = env;
    this.apiInFlight = new Set();
    this.nativeInFlight = new Set();
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL,
        digest TEXT NOT NULL, provider TEXT NOT NULL, request TEXT NOT NULL,
        status TEXT NOT NULL, handle TEXT, output TEXT, artifacts TEXT NOT NULL DEFAULT '[]',
        error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
        deadline REAL NOT NULL, cancel_sent INTEGER NOT NULL DEFAULT 0,
        worker TEXT, attempt TEXT, revision INTEGER NOT NULL DEFAULT 1, report_digest TEXT, report_json TEXT,
        UNIQUE(owner, idem));
      CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        status TEXT NOT NULL, created REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS native_notifications (seq INTEGER PRIMARY KEY, status TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS messages (
        task_id TEXT NOT NULL, idem TEXT NOT NULL, body TEXT NOT NULL,
        revision INTEGER NOT NULL, created REAL NOT NULL,
        PRIMARY KEY(task_id, idem));
    `);
    // Local M1 stores may predate canonical report preservation. Never invent a
    // report body for old rows: the lost numeric lexemes cannot be recovered.
    if (!this.sql.exec("PRAGMA table_info(tasks)").toArray().some((column) => column.name === "report_json")) {
      this.sql.exec("ALTER TABLE tasks ADD COLUMN report_json TEXT");
    }
    this.sql.exec(`CREATE TABLE IF NOT EXISTS subscriptions (
      id TEXT PRIMARY KEY, principal TEXT NOT NULL, owner TEXT NOT NULL, identity_json TEXT NOT NULL,
      url TEXT NOT NULL, secret TEXT NOT NULL, previous_secret TEXT, previous_expires REAL,
      expires REAL NOT NULL, start_seq INTEGER NOT NULL, created REAL NOT NULL, deleted REAL);
      CREATE TABLE IF NOT EXISTS event_deliveries (
      subscription TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(subscription,seq));`);
  }

  fetch() {
    return Response.json({ error: "not_found" }, { status: 404 });
  }

  #policy(owner) {
    const policy = Object.hasOwn(this.policies, owner) ? this.policies[owner] : null;
    if (!policy) denied("project is not admitted");
    return policy;
  }

  #row(owner, taskId) {
    if (!Object.hasOwn(this.policies, owner)) missing();
    const rows = this.sql.exec("SELECT * FROM tasks WHERE id=? AND owner=?", taskId, owner).toArray();
    if (rows.length !== 1) missing();
    return rows[0];
  }

  #record(row) {
    const result = { ...row };
    for (const name of ["request", "handle", "output", "artifacts"]) {
      result[name] = result[name] === null ? null : JSON.parse(result[name]);
    }
    for (const name of ["idem", "digest", "cancel_sent", "report_digest", "report_json"]) delete result[name];
    return { ...result, result_kind: "advisory" };
  }

  #state(taskId, status, fields = {}, recordedAt = now()) {
    const names = Object.keys(fields);
    this.sql.exec(`UPDATE tasks SET status=?,updated=?${names.map((name) => `,${name}=?`).join("")} WHERE id=?`,
      status, recordedAt, ...Object.values(fields), taskId);
    this.sql.exec("INSERT INTO events(task_id,status,created) VALUES(?,?,?)", taskId, status, recordedAt);
  }

  #agentRow(owner, taskId, providerName) {
    const row = this.#row(owner, taskId);
    if (JSON.parse(row.request).mode !== "agent" || row.provider !== providerName || !Object.values(this.#policy(owner).providers).includes(providerName)) {
      denied("worker is not assigned to this task");
    }
    return row;
  }

  async submit(owner, idempotencyKey, canonicalRequest) {
    const policy = this.#policy(owner);
    key(idempotencyKey);
    const request = parseBody(canonicalRequest);
    const allowed = new Set(["mode", "profile", "objective", "model", "materials", "output_schema", "timeout_seconds", "issue"]);
    if (!object(request) || Object.keys(request).some((name) => !allowed.has(name))) invalid("unknown task fields");
    if (!["mode", "objective", "model", "output_schema"].every((name) => Object.hasOwn(request, name))) {
      invalid("mode, objective, model and output_schema are required");
    }
    if (typeof request.objective !== "string" || !request.objective.trim()) invalid("objective is required");
    if (typeof request.mode !== "string" || typeof request.model !== "string") invalid("mode and model must be strings");
    const profile = Object.hasOwn(request, "profile") ? request.profile : "default";
    if (typeof profile !== "string" || !profile || Array.from(profile).length > 100 || profile.includes(":")) {
      invalid("profile must be a nonempty configured profile name");
    }
    const route = profile === "default" ? request.mode : `${request.mode}:${profile}`;
    const provider = policy.providers[route];
    if (!["agent","api"].includes(request.mode) || !provider) denied("execution mode is unavailable");
    if (request.mode === "api") {
      try { apiConfiguration(this.env,provider,request); } catch { denied("execution mode is unavailable"); }
    }
    if (request.mode === 'agent' && provider !== 'dot') {
      try { nativeConfiguration(this.env,provider); } catch { denied('execution mode is unavailable'); }
    }
    if (!policy.models[provider]?.includes(request.model)) denied("model is not admitted");
    const timeout = Object.hasOwn(request, "timeout_seconds") ? request.timeout_seconds : policy.max_timeout_seconds;
    if (!Number.isSafeInteger(timeout) || timeout < 1 || timeout > policy.max_timeout_seconds) invalid("timeout_seconds exceeds project policy");
    if (!Array.isArray(Object.hasOwn(request, "materials") ? request.materials : [])) invalid("materials must be an array");
    if (Object.hasOwn(request, "issue")) denied("App comments are not configured");
    if (!object(request.output_schema)) invalid("output_schema must be an object");
    noExternalReferences(request.output_schema);
    if (bytes(canonicalRequest).length > policy.max_request_bytes) invalid("request too large");
    const requestDigest = await digest(canonicalRequest);
    const record = this.ctx.storage.transactionSync(() => {
      const old = this.sql.exec("SELECT * FROM tasks WHERE owner=? AND idem=?", owner, idempotencyKey).toArray()[0];
      if (old) {
        if (old.digest !== requestDigest) conflict("idempotency key already binds a different request");
        return this.#record(old);
      }
      const count = this.sql.exec("SELECT COUNT(*) AS n FROM tasks WHERE owner=? AND status NOT IN ('completed','failed','invalid_output','cancelled')", owner).one().n;
      if (count >= policy.max_active) conflict("project active task quota exhausted");
      const timestamp = now();
      const taskId = crypto.randomUUID();
      this.sql.exec("INSERT INTO tasks(id,owner,idem,digest,provider,request,status,created,updated,deadline) VALUES(?,?,?,?,?,?,?,?,?,?)",
        taskId, owner, idempotencyKey, requestDigest, provider, canonicalRequest, "queued", timestamp, timestamp, timestamp + timeout);
      this.sql.exec("INSERT INTO events(task_id,status,created) VALUES(?,?,?)", taskId, "queued", timestamp);
      return this.#record(this.#row(owner, taskId));
    });
    await this.#wake();
    return record;
  }

  async lookup(owner, idempotencyKey, requestSHA256) {
    this.#policy(owner);
    key(idempotencyKey, "a frozen Idempotency-Key is required (1..200 characters)");
    if (/[\r\n]/.test(idempotencyKey)) invalid("a frozen Idempotency-Key is required (1..200 characters)");
    if (typeof requestSHA256 !== "string" || !/^[0-9a-f]{64}$/.test(requestSHA256)) invalid("a full lowercase request SHA256 is required");
    const rows = this.sql.exec("SELECT * FROM tasks WHERE owner=? AND idem=?", owner, idempotencyKey).toArray();
    if (rows.length !== 1 || rows[0].digest !== requestSHA256) missing();
    const row = rows[0];
    if (await digest(row.request) !== requestSHA256) missing();
    return this.#record(row);
  }

  projectForTask(projects,taskId) {
    key(taskId,'task ID required');
    const row=this.sql.exec('SELECT owner FROM tasks WHERE id=?',taskId).toArray()[0];
    if(!row||!Array.isArray(projects)||!projects.includes(row.owner))missing();
    this.#policy(row.owner);return row.owner;
  }

  get(owner, taskId) {
    return this.#record(this.#row(owner, taskId));
  }

  // Trusted adapter snapshot: raw JSON preserves Python float/exponent lexemes.
  // It is evidence for serialization, not a schema or authorization verdict.
  // Returning the record in the same synchronous read avoids mixing revisions
  // from separate get()/getCanonical() calls at the API boundary.
  getCanonical(owner, taskId) {
    const row = this.#row(owner, taskId);
    return {
      record: this.#record(row), request_json: row.request, report_json: row.report_json,
      messages: this.sql.exec("SELECT revision,created,body FROM messages WHERE task_id=? ORDER BY revision", taskId).toArray(),
    };
  }

  // Narrow replacement for the existing _advance queued -> submitting write.
  // It does not invoke any provider. A false ticket NEVER permits submission.
  beginSubmission(owner, taskId) {
    return this.ctx.storage.transactionSync(() => {
      const row = this.#row(owner, taskId);
      if (row.status !== "queued") return { task: this.#record(row), should_submit: false };
      if (now() >= row.deadline) {
        this.#state(taskId, "cancelled", { error: "deadline_before_submission" });
        return { task: this.get(owner, taskId), should_submit: false };
      }
      this.#state(taskId, "submitting");
      return { task: this.get(owner, taskId), should_submit: true };
    });
  }

  claim(owner, taskId, providerName, worker, attemptId) {
    key(attemptId, "a stable attempt_id is required");
    return this.ctx.storage.transactionSync(() => {
      const row = this.#agentRow(owner, taskId, providerName);
      if (row.attempt && (row.worker !== worker || row.attempt !== attemptId)) conflict("task already belongs to another execution attempt");
      if (!row.attempt) {
        if (!["submitting", "running", "outcome_unknown"].includes(row.status)) conflict("task cannot be claimed in its current state");
        if (now() >= row.deadline) conflict("task deadline passed");
        this.#state(taskId, "running", { worker, attempt: attemptId, error: null });
      }
      return { ...this.get(owner, taskId), attempt_id: attemptId, messages: this.messages(owner, taskId) };
    });
  }

  // Storage commit for a report already checked by a TRUSTED schema adapter.
  // preparedReport.body is Python _json({revision,status,output,artifacts}).
  // terminal_status preserves invalid_output without changing the replay digest
  // of the original completed report. This method MUST NOT be a raw MCP handler.
  async commitReport(owner, taskId, providerName, worker, attemptId, preparedReport) {
    const policy = this.#policy(owner);
    const report = parseBody(preparedReport?.body);
    if (!object(report) || Object.keys(report).sort().join(",") !== "artifacts,output,revision,status") invalid("invalid prepared report fields");
    if (!["completed", "failed", "cancelled"].includes(report.status)) invalid("report status must be completed, failed or cancelled");
    if (!Array.isArray(report.artifacts)) invalid("artifacts must be an array");
    const terminalStatus = preparedReport.terminal_status;
    if (report.status === "completed" ? !["completed", "invalid_output"].includes(terminalStatus) : terminalStatus !== report.status) {
      invalid("trusted report normalization is required");
    }
    if (bytes(preparedReport.body).length > policy.max_request_bytes) invalid("report too large");
    const reportDigest = await digest(preparedReport.body);
    return this.ctx.storage.transactionSync(() => {
      const row = this.#agentRow(owner, taskId, providerName);
      if (!row.attempt || row.worker !== worker || row.attempt !== attemptId) denied("report does not match the task owner and attempt");
      if (!Number.isSafeInteger(report.revision) || report.revision !== row.revision) conflict("materials changed; read the current revision before reporting");
      if (row.report_digest) {
        if (row.report_digest !== reportDigest) conflict("a different final report already exists");
        return this.#record(row);
      }
      if (!["running", "cancel_requested"].includes(row.status)) conflict("task is not accepting results");
      const acceptedAt = now();
      if (terminalStatus === "completed" && acceptedAt >= row.deadline) conflict("task deadline passed");
      this.#state(taskId, terminalStatus, {
        output: terminalStatus === "invalid_output" ? "null" : JSON.stringify(report.output),
        artifacts: JSON.stringify(report.artifacts), report_digest: reportDigest, report_json: preparedReport.body,
        error: terminalStatus === "invalid_output" ? "output_schema_mismatch" : null,
      }, acceptedAt);
      return this.get(owner, taskId);
    });
  }

  async message(owner, taskId, idempotencyKey, canonicalMessage) {
    key(idempotencyKey, "Idempotency-Key is required");
    const message = parseBody(canonicalMessage);
    if (!object(message) || Object.keys(message).some((name) => !["text", "materials"].includes(name)) || typeof message.text !== "string") {
      invalid("message requires text and optional materials");
    }
    if (!Array.isArray(Object.hasOwn(message, "materials") ? message.materials : [])) invalid("materials must be an array");
    if (bytes(canonicalMessage).length > this.#policy(owner).max_request_bytes) invalid("message too large");
    const record = this.ctx.storage.transactionSync(() => {
      const row = this.#row(owner, taskId);
      const old = this.sql.exec("SELECT body FROM messages WHERE task_id=? AND idem=?", taskId, idempotencyKey).toArray()[0];
      if (old) {
        if (old.body !== canonicalMessage) conflict("message key binds different content");
      } else {
        if (TERMINAL.has(row.status) || row.status === "cancel_requested" || JSON.parse(row.request).mode !== "agent") conflict("task does not accept follow-ups");
        const timestamp = now();
        this.sql.exec("INSERT INTO messages(task_id,idem,body,revision,created) VALUES(?,?,?,?,?)", taskId, idempotencyKey, canonicalMessage, row.revision + 1, timestamp);
        this.sql.exec("UPDATE tasks SET revision=?,updated=? WHERE id=?", row.revision + 1, timestamp, taskId);
        this.sql.exec("INSERT INTO events(task_id,status,created) VALUES(?,?,?)", taskId, "message", timestamp);
      }
      return this.get(owner, taskId);
    });
    await this.#wake();
    return record;
  }

  messages(owner, taskId) {
    this.#row(owner, taskId);
    return this.sql.exec("SELECT * FROM messages WHERE task_id=? ORDER BY revision", taskId).toArray()
      .map((row) => ({ revision: row.revision, created: row.created, ...JSON.parse(row.body) }));
  }

  events(owner, taskId, after = 0) {
    this.#row(owner, taskId);
    return this.sql.exec("SELECT seq,status,created FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 100", taskId, after).toArray();
  }
  async #wake() {
    const existing=await this.ctx.storage.getAlarm();
    if(existing===null||existing>Date.now()+10)await this.ctx.storage.setAlarm(Date.now()+10);
  }

  #eventAccess(owner,identity) {
    if(identity.provider!=='dot')return false;
    let project;try{project=workerContext(this.env,identity,owner);}catch{return false;}
    const client=this.env.AUTH_CLIENTS?.[identity.clientId];
    if(!project||project.project!==owner||client?.provider!=='dot'||!client.scopes?.includes('events:subscribe'))return false;
    let admissions;try{admissions=JSON.parse(this.env.ADMISSIONS_JSON??'[]');}catch{return false;}
    return admissions.some(a=>a.githubUserId===identity.githubUserId&&a.ownerId===project.ownerId&&a.clientIds.includes(identity.clientId)&&a.providers.includes('dot')&&a.scopes.includes('events:subscribe'));
  }

  async subscribe(owner,principal,identity,params) {
    return this.ctx.blockConcurrencyWhile(async()=>{
      this.#policy(owner);
      if(!this.#eventAccess(owner,identity))denied('event access denied');
      if(!object(params)||Object.keys(params).some(k=>!['name','arguments','delivery','cursor','ttlMs'].includes(k))||params.name!=='task.requested'||!object(params.arguments)||Object.keys(params.arguments).join(',')!=='project'||params.arguments.project!==owner||params.cursor!=null)invalid('invalid event request');
      const delivery=params.delivery;
      if(!object(delivery)||Object.keys(delivery).sort().join(',')!=='mode,secret,url'||delivery.mode!=='webhook')invalid('webhook required');
      try{callbackURL(delivery.url);signingKey(delivery.secret);}catch{invalid('invalid webhook destination or key');}
      const ttl=params.ttlMs??3600000;
      if(!Number.isSafeInteger(ttl)||ttl<1000)invalid('invalid ttlMs');
      const expires=now()+Math.min(ttl/1000,3600);
      const identityText=JSON.stringify(identity);
      const subscription='sub_'+await digest(JSON.stringify([principal,delivery.url,'task.requested',{project:owner}]));
      const old=this.sql.exec('SELECT * FROM subscriptions WHERE id=?',subscription).toArray()[0];
      const count=this.sql.exec('SELECT COUNT(*) AS n FROM subscriptions WHERE principal=? AND owner=? AND deleted IS NULL AND expires>?',principal,owner,now()).one().n;
      if(!old&&count>=4)invalid('subscription limit reached');
      const challenge=crypto.randomUUID(),id='msg_verification_'+crypto.randomUUID(),body=JSON.stringify({type:'verification',challenge});
      let reply;
      try{reply=await fetch(delivery.url,{method:'POST',headers:await signedHeaders(subscription,delivery.secret,id,body),body,redirect:'manual',signal:AbortSignal.timeout(5000)});}
      catch{fail('CallbackEndpointError','transport_failed');}
      let response;try{response=await reply.json();}catch{fail('CallbackEndpointError','challenge_failed');}
      if(!reply.ok||response.challenge!==challenge)fail('CallbackEndpointError','challenge_failed');
      if(!this.#eventAccess(owner,identity))denied('event access revoked');
      const continuing=old&&old.deleted===null&&old.expires>now();
      this.ctx.storage.transactionSync(()=>{
        const start=continuing?old.start_seq:this.sql.exec('SELECT COALESCE(MAX(seq),0) AS n FROM events').one().n;
        const rotate=continuing&&old.secret!==delivery.secret;
        const previous=rotate?old.secret:continuing?old.previous_secret:null;
        const until=rotate?Math.min(now()+60,old.expires,expires):continuing?Math.min(old.previous_expires??0,expires):null;
        if(!continuing)this.sql.exec('DELETE FROM event_deliveries WHERE subscription=?',subscription);
        this.sql.exec(`INSERT INTO subscriptions(id,principal,owner,identity_json,url,secret,previous_secret,previous_expires,expires,start_seq,created,deleted)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET identity_json=excluded.identity_json,secret=excluded.secret,
          previous_secret=excluded.previous_secret,previous_expires=excluded.previous_expires,expires=excluded.expires,start_seq=excluded.start_seq,deleted=NULL`,
          subscription,principal,owner,identityText,delivery.url,delivery.secret,previous,until,expires,start,continuing?old.created:now());
      });
      await this.#wake();
      return {id:subscription,refreshBefore:new Date(expires*1000).toISOString(),cursor:null,truncated:false};
    });
  }

  subscriptionStatus(owner,principal,id) {
    this.#policy(owner);key(id,'subscription ID required');
    const row=this.sql.exec('SELECT * FROM subscriptions WHERE id=?',id).toArray()[0];
    if(row&&(row.owner!==owner||row.principal!==principal))denied('subscription access denied');
    return {id,status:!row||row.deleted!==null?'gone':row.expires>now()&&this.#eventAccess(owner,JSON.parse(row.identity_json))?'active':'inactive',
      created_at:row?new Date(row.created*1000).toISOString():null,expires_at:row?new Date(row.expires*1000).toISOString():null,deleted_at:row?.deleted?new Date(row.deleted*1000).toISOString():null};
  }

  async unsubscribe(owner,principal,id) {
    return this.ctx.blockConcurrencyWhile(async()=>{
      const old=this.subscriptionStatus(owner,principal,id),deleted=old.status!=='gone';
      if(deleted)this.ctx.storage.transactionSync(()=>{this.sql.exec('UPDATE subscriptions SET deleted=?,secret=?,previous_secret=NULL WHERE id=?',now(),'',id);this.sql.exec('DELETE FROM event_deliveries WHERE subscription=?',id);});
      return {...this.subscriptionStatus(owner,principal,id),deleted};
    });
  }

  async unsubscribeRequest(owner,principal,params) {
    if(!object(params)||Object.keys(params).sort().join(',')!=='arguments,delivery,name'||params.name!=='task.requested'||!object(params.arguments)||Object.keys(params.arguments).join(',')!=='project'||params.arguments.project!==owner||!object(params.delivery)||Object.keys(params.delivery).sort().join(',')!=='mode,url'||params.delivery.mode!=='webhook')invalid('invalid unsubscribe');
    const id='sub_'+await digest(JSON.stringify([principal,params.delivery.url,'task.requested',{project:owner}]));
    await this.unsubscribe(owner,principal,id);return {};
  }

  async cancel(owner,taskId) {
    const record=this.ctx.storage.transactionSync(()=>{
      const row=this.#row(owner,taskId);
      if(row.status==='queued')this.#state(taskId,'cancelled');
      else if(!TERMINAL.has(row.status)&&!['outcome_unknown','cancel_requested'].includes(row.status))this.#state(taskId,'cancel_requested');
      return this.get(owner,taskId);
    });
    await this.#wake();return record;
  }

  async alarm() {
    const apiJobs=[],nativeJobs=[];
    await this.ctx.blockConcurrencyWhile(async()=>{
      const timestamp=now();
      this.ctx.storage.transactionSync(()=>{
        for(const task of this.sql.exec("SELECT * FROM tasks WHERE status IN ('queued','submitting','running') OR (status='cancel_requested' AND json_extract(request,'$.mode')='api' AND handle IS NULL)").toArray()){
          if(task.status==='submitting'||task.status==='cancel_requested'){
            if(this.apiInFlight.has(task.id)||this.nativeInFlight.has(task.id))continue;
            // Crash before acceptance is unknown, never an instruction to submit a new task.
            this.#state(task.id,'outcome_unknown',{error:'acceptance_unknown'});continue;
          }
          if(task.deadline<=timestamp){this.#state(task.id,task.status==='queued'?'cancelled':'cancel_requested',{error:task.status==='queued'?'deadline_before_submission':'deadline_exceeded'});continue;}
          if(task.status!=='queued')continue;
          this.#state(task.id,'submitting');
          if(JSON.parse(task.request).mode==='api'){this.apiInFlight.add(task.id);apiJobs.push(task);continue;}
          if(task.provider!=='dot'){this.nativeInFlight.add(task.id);nativeJobs.push({...task,change:'requested'});continue;}
          const active=this.sql.exec('SELECT * FROM subscriptions WHERE owner=? AND deleted IS NULL AND expires>?',task.owner,timestamp).toArray().some(s=>this.#eventAccess(task.owner,JSON.parse(s.identity_json)));
          if(task.provider!=='dot'||!active){this.#state(task.id,'failed',{error:'submission_rejected'});continue;}
          // Pure durable event acceptance has no remote side effect between these writes.
          this.#state(task.id,'running',{handle:JSON.stringify({task_id:task.id,acceptance:'event_pending',model_verification:'unavailable'})});
        }
        for(const sub of this.sql.exec('SELECT * FROM subscriptions WHERE deleted IS NULL').toArray()){
          if(sub.expires<=timestamp||!this.#eventAccess(sub.owner,JSON.parse(sub.identity_json))){this.sql.exec('DELETE FROM event_deliveries WHERE subscription=?',sub.id);continue;}
          const pending=this.sql.exec("SELECT COUNT(*) AS n FROM event_deliveries WHERE subscription=? AND status='pending'",sub.id).one().n;
          const rows=this.sql.exec(`SELECT e.seq,e.status,e.created,t.id,t.owner,t.revision FROM events e JOIN tasks t ON t.id=e.task_id
            WHERE t.owner=? AND t.provider='dot' AND e.seq>? AND e.status IN ('queued','message','cancel_requested')
            AND t.status IN ('running','cancel_requested','outcome_unknown') ORDER BY e.seq LIMIT ?`,sub.owner,sub.start_seq,Math.max(0,128-pending)).toArray();
          for(const e of rows){const body=JSON.stringify({eventId:'evt_'+e.seq,name:'task.requested',timestamp:new Date(e.created*1000).toISOString(),cursor:null,data:{task_id:e.id,project:e.owner,revision:e.revision,change:e.status}});this.sql.exec('INSERT OR IGNORE INTO event_deliveries(subscription,seq,body) VALUES(?,?,?)',sub.id,e.seq,body);}
          if(rows.length)this.sql.exec('UPDATE subscriptions SET start_seq=? WHERE id=?',rows.at(-1).seq,sub.id);
        }
      });
      this.sql.exec("UPDATE native_notifications SET status='outcome_unknown' WHERE status='sending'");
      const notifications=this.sql.exec(`SELECT e.seq,e.status AS change,t.* FROM events e JOIN tasks t ON t.id=e.task_id
        LEFT JOIN native_notifications n ON n.seq=e.seq WHERE e.status IN ('message','cancel_requested')
        AND n.seq IS NULL AND t.provider!='dot' AND json_extract(t.request,'$.mode')='agent'
        AND t.status IN ('running','cancel_requested') ORDER BY e.seq LIMIT 100`).toArray();
      for(const event of notifications){
        this.sql.exec("INSERT INTO native_notifications(seq,status) VALUES(?,'sending')",event.seq);
        nativeJobs.push(event);
      }
      const deliveries=this.sql.exec(`SELECT d.*,s.url,s.secret,s.previous_secret,s.previous_expires,s.owner,s.identity_json,s.expires,s.deleted FROM event_deliveries d JOIN subscriptions s ON s.id=d.subscription
        WHERE d.status='pending' AND d.next_attempt<=? ORDER BY d.seq LIMIT 4`,timestamp).toArray();
      for(const row of deliveries){
        if(row.deleted!==null||row.expires<=now()||!this.#eventAccess(row.owner,JSON.parse(row.identity_json)))continue;
        let status=0;
        try{const reply=await fetch(row.url,{method:'POST',headers:await signedHeaders(row.subscription,row.secret,'evt_'+row.seq,row.body,row.previous_expires>now()?row.previous_secret:null),body:row.body,redirect:'manual',signal:AbortSignal.timeout(2000)});status=reply.status;await reply.body?.cancel();}catch{}
        const attempts=row.attempts+1,transient=status===0||status===429||status>=500;
        const state=status>=200&&status<300?'sent':transient&&attempts<5?'pending':'stopped';
        this.sql.exec('UPDATE event_deliveries SET status=?,attempts=?,next_attempt=? WHERE subscription=? AND seq=?',state,attempts,now()+Math.min(2**attempts,60),row.subscription,row.seq);
      }
      const delivery=this.sql.exec("SELECT MIN(next_attempt) AS n FROM event_deliveries WHERE status='pending'").one().n;
      // An expired live API call can only finish or become unknown; inspect its
      // recovery state at most once per second instead of rearming every 10ms.
      const scheduledAt=now();
      const taskDeadlines=this.sql.exec("SELECT id,deadline FROM tasks WHERE status IN ('queued','submitting','running') OR (status='cancel_requested' AND json_extract(request,'$.mode')='api' AND handle IS NULL)").toArray()
        .map(task=>(this.apiInFlight.has(task.id)||this.nativeInFlight.has(task.id))&&task.deadline<=scheduledAt?scheduledAt+1:task.deadline);
      const next=[delivery,...taskDeadlines].filter(v=>v!==null);
      if(next.length)await this.ctx.storage.setAlarm(Math.max(Date.now()+10,Math.min(...next)*1000));
    });
    // Marked durably before sending. Lost receipts never cause a second routine run.
    for(const task of nativeJobs){
      let status='accepted',handle;
      try{
        const current=this.#row(task.owner,task.id);
        if(task.change==='requested'&&(current.status==='cancel_requested'||task.deadline<=now())){
          this.#state(task.id,'cancelled');continue;
        }
        handle=await triggerNative(this.env,task.provider,{task_id:task.id,project:task.owner,change:task.change,revision:task.revision},task.change==='requested'?task.deadline-now():30);
      }catch(e){status=e.name==='SubmissionRejected'?'rejected':'outcome_unknown';}
      finally{this.nativeInFlight.delete(task.id);}
      this.ctx.storage.transactionSync(()=>{
        if(task.seq!==undefined){this.sql.exec('UPDATE native_notifications SET status=? WHERE seq=?',status,task.seq);return;}
        // A fast worker may have already claimed, cancelled or completed the task.
        if(this.#row(task.owner,task.id).status!=='submitting')return;
        this.#state(task.id,status==='accepted'?'running':status==='rejected'?'failed':'outcome_unknown',
          {handle:handle?JSON.stringify(handle):null,error:status==='accepted'?null:status==='rejected'?'submission_rejected':'acceptance_unknown'});
      });
    }
    // The durable submitting transition commits before any outbound inference.
    // A crash/lost receipt is outcome_unknown and never submits a replacement.
    for(const task of apiJobs){
      const before=this.#row(task.owner,task.id);
      if(before.status==='cancel_requested'||task.deadline<=now()){
        this.ctx.storage.transactionSync(()=>{this.#state(task.id,'cancelled',{error:before.status==='cancel_requested'?null:'deadline_before_submission'});});
        this.apiInFlight.delete(task.id);
        continue;
      }
      let result,error;
      try{
        result=await executeAPI(this.env,task.provider,JSON.parse(task.request),task.deadline-now());
        if(result.status==='completed'&&!await outputValid(JSON.parse(task.request).output_schema,valueOf(result.output))){
          result={...result,status:'invalid_output',output:null};error='output_schema_mismatch';
        }
      }catch(e){result={status:e.name==='SubmissionRejected'?'failed':'outcome_unknown',output:null};error=e.name==='SubmissionRejected'?'submission_rejected':'acceptance_unknown';}
      this.ctx.storage.transactionSync(()=>{
        const current=this.#row(task.owner,task.id);
        if(!['submitting','cancel_requested'].includes(current.status))return;
        const {output,...handle}=result;
        const report=objectNode([['revision',nodeFrom(current.revision)],['status',nodeFrom(result.status)],['output',output??nodeFrom(null)],['artifacts',nodeFrom([])]]);
        this.#state(task.id,result.status,{handle:JSON.stringify({task_id:task.id,...handle}),output:output?serialize(output):null,report_json:serialize(report),error:error??null});
      });
      this.apiInFlight.delete(task.id);
    }
  }

}

export class PigbibiTaskStore extends TaskStore {
  constructor(ctx, env) { super(ctx, env, "Pigbibi"); }
}

export class QuantTaskStore extends TaskStore {
  constructor(ctx, env) { super(ctx, env, "Quant"); }
}
