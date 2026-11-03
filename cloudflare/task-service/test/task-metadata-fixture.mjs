import {WorkerEntrypoint} from 'cloudflare:workers';
import {PigbibiTaskStore, QuantTaskStore} from '../src/state.mjs';
import {createAuthApplication, configuration} from '../src/auth.mjs';
import {initializeTaskMetadata} from '../src/task-metadata.mjs';
export {TaskService} from '../src/task-service.mjs';
const fixture = Base => class extends Base {
  seed(rows) {
    for (const row of rows) this.sql.exec(`INSERT INTO tasks
      (id,owner,idem,digest,provider,request,status,created,updated,deadline,attempt,worker,claimed_at,completed_at,error,output,handle)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, row.id,row.owner,row.id,'synthetic-digest',row.provider,
      JSON.stringify({mode:'agent',objective:'PRIVATE PROMPT',output_schema:{type:'object'}}),row.status,
      row.created,row.updated??row.created,Date.now()/1000+3600,row.attempt??null,row.attempt?'synthetic-worker':null,
      row.claimed_at??null,row.completed_at??null,row.error??null,'"PRIVATE RESULT"','{"secret":"PRIVATE SECRET"}');
  }
  migrateLegacy() {
    this.sql.exec('ALTER TABLE tasks DROP COLUMN claimed_at');
    this.sql.exec('ALTER TABLE tasks DROP COLUMN completed_at');
    initializeTaskMetadata(this.sql);
    initializeTaskMetadata(this.sql);
  }
  async measureMetadata(owner, query, stats=false) {
    const sql=this.sql, cursors=[];
    this.sql={exec(...args){const cursor=sql.exec(...args);cursors.push(cursor);return cursor;}};
    let result, error;
    try {result=await (stats?this.metadataStats(owner,query):this.listMetadata(owner,query));}
    catch (e) {error=e.name;}
    finally {this.sql=sql;}
    return {result,error,cost:cursors.map(c=>({read:c.rowsRead,written:c.rowsWritten}))};
  }
  reinitialize() {initializeTaskMetadata(this.sql);}
  dump() {return ['tasks','events','messages'].map(table=>this.sql.exec(`SELECT * FROM ${table} ORDER BY rowid`).toArray());}
  sizes() {return ['tasks','events','messages'].map(table=>this.sql.exec(`SELECT COUNT(*) AS n FROM ${table}`).one().n);}
  async alarm() {}
};
export class PersonalMetadataStore extends fixture(PigbibiTaskStore) {}
export class QuantMetadataStore extends fixture(QuantTaskStore) {}
export default class MetadataFixture extends WorkerEntrypoint {
  async providerAccess(path) {
    const response=await this.env.TASK_SERVICE.handleAuthenticated(new Request(this.env.PUBLIC_ORIGIN+path),{
      organization:this.env.ORGANIZATION,projectId:this.env.PROJECT_ID,ownerId:'ExampleOwner',
      githubUserId:'10001',clientId:'synthetic-provider',provider:'dot',scopes:['tasks:list','tasks:read','tasks:work'],
      resource:this.env.PUBLIC_ORIGIN+'/v1/tasks'});
    return response.status;
  }
  fetch(request) {return createAuthApplication(configuration(this.env)).fetch(request,this.env,this.ctx);}
}
