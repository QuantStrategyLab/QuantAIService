// Existing GitHub Actions caller grants, restricted to one configured project and REST resource.
const ISSUER = 'https://token.actions.githubusercontent.com';
const JWKS_URL = ISSUER + '/.well-known/jwks';
const TASK_SCOPES = ['tasks:read', 'tasks:submit'];
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const numericID = value => typeof value === 'string' && /^[1-9][0-9]*$/.test(value);
const rejected = () => {throw new Error('workload_token_rejected');};
let cachedKeys;

function decodePart(part) {
  if (!/^[A-Za-z0-9_-]+$/.test(part)) rejected();
  const bytes = Uint8Array.from(atob(part.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
  return bytes;
}

function decodeToken(token) {
  if (typeof token !== 'string' || token.length > 16384) rejected();
  const parts = token.split('.');
  if (parts.length !== 3) rejected();
  const json = part => JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(decodePart(part)));
  const header = json(parts[0]), claims = json(parts[1]);
  if (!object(header) || !object(claims)) rejected();
  return {parts, header, claims};
}

/** Unverified issuer is only a selector; it never authorizes a request or selects a key URL. */
export function githubWorkloadToken(request) {
  const authorization = request.headers.get('authorization');
  if (!authorization?.startsWith('Bearer ')) return null;
  const token = authorization.slice(7);
  try {return decodeToken(token).claims.iss === ISSUER ? token : null;} catch {return null;}
}

function configuredGrant(env, config) {
  const audience = env.GITHUB_WORKLOAD_AUDIENCE;
  if (typeof audience !== 'string' || !audience || /\s/.test(audience)) rejected();
  const target = new URL(audience);
  if (target.protocol !== 'https:' || !target.hostname || target.username || target.password || target.search || target.hash) rejected();
  const grants = JSON.parse(env.GITHUB_WORKLOAD_GRANTS_JSON || '[]');
  if (!Array.isArray(grants) || grants.length !== 1) rejected();
  const grant = grants[0], context = env.PROJECT_CONTEXTS?.[config.projectId];
  if (!object(grant) || !object(context) || context.organization !== config.organization ||
      typeof context.project !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(context.project) ||
      context.project.split('/').some(part => ['.', '..'].includes(part)) ||
      context.ownerId !== context.project.split('/')[0] || grant.project !== context.project ||
      grant.issuer !== ISSUER || typeof grant.identity !== 'string' ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(grant.identity) ||
      grant.provider != null || Object.keys(grant).some(k => !['issuer', 'identity', 'subject', 'project', 'scopes', 'claims', 'provider'].includes(k)) ||
      !Array.isArray(grant.scopes) || !grant.scopes.length || new Set(grant.scopes).size !== grant.scopes.length ||
      grant.scopes.some(scope => !TASK_SCOPES.includes(scope)) || !object(grant.claims)) rejected();
  const constraints = grant.claims, ref = constraints.ref;
  if (constraints.repository !== context.project || typeof ref !== 'string' ||
      !/^refs\/heads\/[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(ref) || ref.includes('..') || ref.includes('//') ||
      !ref.split('/').every(part => part && part !== '.') ||
      !Object.values(constraints).every(value => typeof value === 'string' && value.length > 0)) rejected();
  const workflowPrefix = context.project + '/.github/workflows/';
  const workflow = constraints.workflow_ref;
  if (typeof workflow !== 'string' || !workflow.startsWith(workflowPrefix) || !workflow.endsWith('@' + ref) ||
      !/^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml$/.test(workflow.slice(workflowPrefix.length, -(ref.length + 1)))) rejected();
  const standardSubject = `repo:${context.project}:ref:${ref}`;
  const [owner, repository] = context.project.split('/');
  const immutableSubject = numericID(constraints.repository_owner_id) && numericID(constraints.repository_id)
    ? `repo:${owner}@${constraints.repository_owner_id}/${repository}@${constraints.repository_id}:ref:${ref}` : null;
  if (grant.subject !== standardSubject && (immutableSubject === null || grant.subject !== immutableSubject)) rejected();
  return {grant, context, audience};
}

async function signingKey(kid) {
  if (!cachedKeys || cachedKeys.until <= Date.now()) {
    const response = await fetch(JWKS_URL, {method: 'GET', redirect: 'manual', signal: AbortSignal.timeout(5000)});
    if (response.status !== 200) rejected();
    const body = await response.json();
    if (!object(body) || !Array.isArray(body.keys) || !body.keys.length) rejected();
    cachedKeys = {keys: body.keys, until: Date.now() + 300000};
  }
  const matches = cachedKeys.keys.filter(key => object(key) && key.kid === kid && key.kty === 'RSA' &&
    (key.alg === undefined || key.alg === 'RS256') && (key.use === undefined || key.use === 'sig') &&
    (key.key_ops === undefined || Array.isArray(key.key_ops) && key.key_ops.includes('verify')));
  if (matches.length !== 1) rejected();
  const {n, e} = matches[0];
  return crypto.subtle.importKey('jwk', {kty: 'RSA', n, e, alg: 'RS256', ext: true, key_ops: ['verify']},
    {name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256'}, false, ['verify']);
}

/** Returns the existing service-binding identity shape; never a worker/provider identity. */
export async function authenticateGitHubWorkload(token, env, config) {
  const {grant, context, audience} = configuredGrant(env, config);
  const {parts, header, claims} = decodeToken(token);
  if (header.alg !== 'RS256' || typeof header.kid !== 'string' || !header.kid || header.kid.length > 256 ||
      header.typ !== undefined && header.typ !== 'JWT' || header.crit !== undefined || header.b64 !== undefined ||
      header.jku !== undefined || header.x5u !== undefined) rejected();
  if (claims.iss !== ISSUER || claims.aud !== audience || claims.sub !== grant.subject ||
      !['exp', 'iat', 'nbf'].every(name => Number.isSafeInteger(claims[name])) ||
      claims.exp <= Date.now() / 1000 || claims.iat > Date.now() / 1000 || claims.nbf > Date.now() / 1000 ||
      claims.exp <= claims.iat || claims.exp <= claims.nbf || !numericID(claims.actor_id) ||
      claims.repository_owner !== context.ownerId ||
      typeof claims.event_name === 'string' && claims.event_name.startsWith('pull_request') ||
      Object.entries(grant.claims).some(([name, value]) => claims[name] !== value)) rejected();
  const key = await signingKey(header.kid);
  const verified = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, decodePart(parts[2]),
    new TextEncoder().encode(parts[0] + '.' + parts[1]));
  if (!verified) rejected();
  let scopes = [...grant.scopes];
  if (claims.scope !== undefined) {
    if (typeof claims.scope !== 'string') rejected();
    scopes = scopes.filter(scope => claims.scope.split(/\s+/).includes(scope));
  }
  return Object.freeze({organization: config.organization, projectId: config.projectId, ownerId: context.ownerId,
    // Kept for the internal RPC shape only. The signed actor ID grants no authority.
    githubUserId: claims.actor_id, clientId: grant.identity, provider: null,
    scopes: Object.freeze(scopes), resource: config.origin + '/v1/tasks'});
}

export function workloadOperation(request) {
  const path = new URL(request.url).pathname;
  return request.method === 'POST' && (path === '/v1/tasks' || /^\/v1\/tasks\/[^/]+\/cancel$/.test(path)) ? 'tasks:submit'
    : request.method === 'GET' && (/^\/v1\/tasks\/[^/]+$/.test(path)) ? 'tasks:read' : null;
}
