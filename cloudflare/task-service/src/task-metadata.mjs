// This module projects only operational metadata; task content never leaves SQLite.
export const STUCK_QUEUE_MINUTES = 15;
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
  const rawLimit = params.get('limit') ?? '50';
  if (!/^[1-9][0-9]{0,8}$/.test(rawLimit)) invalid();
  return {since, provider, status, limit: Math.min(Number(rawLimit), 200), cursor: params.get('cursor')};
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
    CREATE INDEX IF NOT EXISTS tasks_owner_provider_status_created ON tasks(owner,provider,status,created DESC,id DESC);`);
}

function metadata(row) {
  return {id: row.id, provider: row.provider, status: row.status,
    created_at: iso(row.created), claimed_at: iso(row.claimed_at), completed_at: iso(row.completed_at),
    error_code: row.error === null ? null : ERROR_CODES.has(row.error) ? row.error : 'unknown_error'};
}

export function listTaskMetadata(sql, owner, query) {
  const {since, provider, status, limit, cursor} = query;
  const clauses = ['owner=?', 'created>=?'], args = [owner, since];
  if (provider !== null) {clauses.push('provider=?'); args.push(provider);}
  if (status !== null) {clauses.push('status=?'); args.push(status);}
  if (cursor !== null) {
    let value;
    try {if (cursor.length > 2048 || !/^[A-Za-z0-9_-]+$/.test(cursor)) invalid(); value = JSON.parse(atob(cursor.replaceAll('-', '+').replaceAll('_', '/')));} catch {invalid();}
    if (!Array.isArray(value) || value.length !== 6 || value[0] !== owner || value[1] !== since || value[2] !== provider || value[3] !== status ||
        !Number.isFinite(value[4]) || typeof value[5] !== 'string' || !/^[A-Za-z0-9_-]{1,200}$/.test(value[5])) invalid();
    clauses.push('(created<? OR (created=? AND id<?))'); args.push(value[4], value[4], value[5]);
  }
  const rows = sql.exec(`SELECT id,provider,status,created,claimed_at,completed_at,error FROM tasks
    WHERE ${clauses.join(' AND ')} ORDER BY created DESC,id DESC LIMIT ?`, ...args, limit + 1).toArray();
  const page = rows.slice(0, limit), last = page.at(-1);
  const next = rows.length > limit ? btoa(JSON.stringify([owner, since, provider, status, last.created, last.id])).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '') : null;
  return {tasks: page.map(metadata), next_cursor: next};
}

export function taskMetadataStats(sql, owner, {since}, timestamp) {
  const counts = sql.exec(`SELECT provider,status,COUNT(*) AS count FROM tasks WHERE owner=? AND created>=?
    GROUP BY provider,status ORDER BY provider,status`, owner, since).toArray();
  const stuck = sql.exec(`SELECT
    COALESCE(SUM(CASE WHEN (claimed_at IS NOT NULL OR attempt IS NOT NULL) AND status NOT IN (${terminalSQL}) THEN 1 ELSE 0 END),0) AS claimed_not_completed,
    COALESCE(SUM(CASE WHEN status='queued' AND created<? THEN 1 ELSE 0 END),0) AS queued_over_threshold
    FROM tasks WHERE owner=? AND created>=?`, timestamp - STUCK_QUEUE_MINUTES * 60, owner, since).one();
  return {counts, stuck: {...stuck, total: stuck.claimed_not_completed + stuck.queued_over_threshold}, queued_threshold_minutes: STUCK_QUEUE_MINUTES};
}
