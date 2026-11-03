// This module projects only operational metadata; task content never leaves SQLite.
export const STUCK_QUEUE_MINUTES = 15;
export const DEFAULT_PAGE_SIZE = 50;
export const MAX_PAGE_SIZE = 200;
export const STATS_ROW_BUDGET = 1000;
const STATUSES = new Set(['queued', 'submitting', 'running', 'outcome_unknown', 'cancel_requested', 'completed', 'failed', 'invalid_output', 'cancelled']);
const ERROR_CODES = new Set(['deadline_before_submission', 'deadline_exceeded', 'acceptance_unknown', 'submission_rejected', 'output_schema_mismatch']);
const invalid = () => {throw Object.assign(new Error('invalid task query'), {name: 'ValueError'});};
const terminalSQL = "'completed','failed','invalid_output','cancelled'";
const iso = value => value === null ? null : new Date(value * 1000).toISOString();

export function taskQuery(params, stats = false) {
  const allowed = stats ? ['since', 'project'] : ['since', 'provider', 'status', 'limit', 'cursor', 'project'];
  for (const name of params.keys()) if (!allowed.includes(name) || params.getAll(name).length !== 1) invalid();
  let since = 0;
  if (params.has('since')) {
    const text = params.get('since');
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(text)) invalid();
    since = Date.parse(text) / 1000;
    const [year, month, day] = text.slice(0, 10).split('-').map(Number);
    const date = new Date(0); date.setUTCFullYear(year, month, 0);
    if (!Number.isFinite(since) || month < 1 || month > 12 || day < 1 || day > date.getUTCDate() || Number(text.slice(11, 13)) > 23) invalid();
  }
  const provider = params.get('provider'), status = params.get('status');
  if (provider !== null && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(provider) || status !== null && !STATUSES.has(status)) invalid();
  const rawLimit = params.get('limit') ?? String(DEFAULT_PAGE_SIZE);
  if (!/^[1-9][0-9]{0,8}$/.test(rawLimit)) invalid();
  return {since, provider, status, limit: Math.min(Number(rawLimit), MAX_PAGE_SIZE), cursor: params.get('cursor')};
}

export function initializeTaskMetadata(sql) {
  const columns = new Set(sql.exec('PRAGMA table_info(tasks)').toArray().map(column => column.name));
  if (!columns.has('claimed_at')) sql.exec('ALTER TABLE tasks ADD COLUMN claimed_at REAL');
  if (!columns.has('completed_at')) {
    sql.exec('ALTER TABLE tasks ADD COLUMN completed_at REAL');
    // Existing terminal rows retain their final transition time. Legacy claim times are unknown.
    sql.exec(`UPDATE tasks SET completed_at=updated WHERE status IN (${terminalSQL})`);
  }
  sql.exec(`CREATE INDEX IF NOT EXISTS tasks_owner_created_id ON tasks(owner,created DESC,id DESC);
    CREATE INDEX IF NOT EXISTS tasks_owner_status_created_id ON tasks(owner,status,created DESC,id DESC);
    CREATE INDEX IF NOT EXISTS tasks_owner_provider_created_id ON tasks(owner,provider,created DESC,id DESC);
    CREATE INDEX IF NOT EXISTS tasks_owner_provider_status_created ON tasks(owner,provider,status,created DESC,id DESC);`);
  // Initialization only: a persistent per-store key keeps cursors valid across restarts.
  // Never create/rotate keys while serving metadata GETs.
  sql.exec('CREATE TABLE IF NOT EXISTS task_metadata_cursor_key (id INTEGER PRIMARY KEY CHECK(id=1), material BLOB NOT NULL)');
  if (!sql.exec('SELECT id FROM task_metadata_cursor_key WHERE id=1').toArray().length)
    sql.exec('INSERT OR IGNORE INTO task_metadata_cursor_key(id,material) VALUES(1,?)', crypto.getRandomValues(new Uint8Array(32)).buffer);
}

function metadata(row) {
  return {id: row.id, provider: row.provider, status: row.status,
    created_at: iso(row.created), claimed_at: iso(row.claimed_at), completed_at: iso(row.completed_at),
    error_code: row.error === null ? null : ERROR_CODES.has(row.error) ? row.error : 'unknown_error'};
}

const encode = value => btoa(String.fromCharCode(...value)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
const text = new TextEncoder();

async function cursorKey(sql) {
  const material = sql.exec('SELECT material FROM task_metadata_cursor_key WHERE id=1').one().material;
  return crypto.subtle.importKey('raw', material, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

export async function listTaskMetadata(sql, owner, query) {
  const {since, provider, status, limit, cursor} = query;
  const clauses = ['owner=?', 'created>=?'], args = [owner, since];
  if (provider !== null) {clauses.push('provider=?'); args.push(provider);}
  if (status !== null) {clauses.push('status=?'); args.push(status);}
  // Authenticated encryption conceals the entire position; identity and filters
  // are authenticated context only, never part of the public cursor payload.
  const additionalData = text.encode(JSON.stringify(['task-list-v1', owner, since, provider, status, 'created-desc,id-desc']));
  const key = await cursorKey(sql);
  if (cursor !== null) {
    let value;
    try {
      if (cursor.length > 2048 || !/^[A-Za-z0-9_-]+$/.test(cursor)) invalid();
      const data = Uint8Array.from(atob(cursor.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
      if (data.length < 29 || encode(data) !== cursor) invalid();
      const clear = await crypto.subtle.decrypt({name:'AES-GCM', iv:data.slice(0,12), additionalData}, key, data.slice(12));
      value = JSON.parse(new TextDecoder().decode(clear));
    } catch {invalid();}
    if (!Array.isArray(value) || value.length !== 2 || !Number.isFinite(value[0]) ||
        typeof value[1] !== 'string' || !/^[A-Za-z0-9_-]{1,200}$/.test(value[1])) invalid();
    // A row-value range lets SQLite seek at the page boundary, including ties.
    clauses.push('(created,id)<(?,?)'); args.push(value[0], value[1]);
  }
  const index = provider !== null ? status !== null ? 'tasks_owner_provider_status_created' : 'tasks_owner_provider_created_id'
    : status !== null ? 'tasks_owner_status_created_id' : 'tasks_owner_created_id';
  const rows = sql.exec(`SELECT id,provider,status,created,claimed_at,completed_at,error FROM tasks INDEXED BY ${index}
    WHERE ${clauses.join(' AND ')} ORDER BY created DESC,id DESC LIMIT ?`, ...args, limit + 1).toArray();
  const page = rows.slice(0, limit), last = page.at(-1);
  let next = null;
  if (rows.length > limit) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const sealed = new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM', iv, additionalData}, key, text.encode(JSON.stringify([last.created, last.id]))));
    next = encode(new Uint8Array([...iv, ...sealed]));
  }
  return {tasks: page.map(metadata), next_cursor: next};
}

export function taskMetadataStats(sql, owner, {since}, timestamp) {
  // Read at most budget+1 index entries; never run COUNT/SUM over retained history.
  // Over-budget requests fail instead of silently returning incomplete counts.
  const rows = sql.exec(`SELECT provider,status,created,claimed_at,attempt FROM tasks INDEXED BY tasks_owner_created_id
    WHERE owner=? AND created>=? ORDER BY created DESC,id DESC LIMIT ?`, owner, since, STATS_ROW_BUDGET + 1).toArray();
  if (rows.length > STATS_ROW_BUDGET) invalid();
  const groups = new Map(), terminal = new Set(['completed','failed','invalid_output','cancelled']);
  const stuck = {claimed_not_completed:0, queued_over_threshold:0};
  for (const row of rows) {
    const key = JSON.stringify([row.provider, row.status]);
    if (!groups.has(key)) groups.set(key, {provider:row.provider, status:row.status, count:0});
    groups.get(key).count++;
    if ((row.claimed_at !== null || row.attempt !== null) && !terminal.has(row.status)) stuck.claimed_not_completed++;
    if (row.status === 'queued' && row.created < timestamp - STUCK_QUEUE_MINUTES * 60) stuck.queued_over_threshold++;
  }
  const compare = (a,b) => a < b ? -1 : a > b ? 1 : 0;
  const counts = [...groups.values()].sort((a,b) => compare(a.provider,b.provider) || compare(a.status,b.status));
  return {counts, stuck: {...stuck, total: stuck.claimed_not_completed + stuck.queued_over_threshold}, queued_threshold_minutes: STUCK_QUEUE_MINUTES};
}
