import {
  AuthorizationError,
  authorizationErrorRedirect,
  CimdFetchError,
  OAuthAuthorizationServer,
  OAuthResourceServer,
  insufficientScope,
} from '@cloudflare/workers-oauth-provider';
import {githubWorkloadToken, authenticateGitHubWorkload, workloadOperation} from './workload.mjs';

// Names come from existing server-controlled grants. Never rename an admitted alias.
const validProvider = (value) => value===null || typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value);
// Preserve the current Python service's scope vocabulary.
export const SCOPES = ['tasks:read', 'tasks:submit', 'tasks:work', 'events:subscribe', 'offline_access'];
const TRUST_HEADERS = ['authorization', 'x-owner-id', 'x-owner', 'x-project-id', 'x-project',
  'x-provider', 'x-user-id', 'x-github-user-id', 'x-authenticated-identity', 'x-organization'];
const escape = (value) => String(value).replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);
const deny = (error, status = 403) => Response.json({ error }, { status, headers: { 'Cache-Control': 'no-store' } });
const requiredScopesFor = (request, isMcp) => [isMcp ? 'tasks:work'
  : (['GET', 'HEAD'].includes(request.method) ? 'tasks:read' : 'tasks:submit')];
const advertisedScopesFor = (resource, request, env) => {
  if (!resource.endsWith('/mcp')) return requiredScopesFor(request, false);
  const configured = Object.values(env.AUTH_CLIENTS ?? {}).filter((client) =>
    client?.provider !== null && validProvider(client?.provider) &&
    Array.isArray(client.resources) && client.resources.includes(resource) && Array.isArray(client.scopes));
  return SCOPES.filter((scope) => configured.some((client) => client.scopes.includes(scope)));
};

/** Server-controlled configuration, never request headers or body. One instance per organization. */
export function configuration(env) {
  const organization = env.ORGANIZATION;
  if (!['pigbibi', 'quant'].includes(organization)) throw new TypeError('Invalid organization');
  const origin = new URL(env.PUBLIC_ORIGIN);
  if (origin.origin !== env.PUBLIC_ORIGIN || origin.username || origin.password) throw new TypeError('PUBLIC_ORIGIN must be an origin');
  if (!env.PROJECT_ID || env.PROJECT_ID.startsWith('CONFIGURE_')) throw new TypeError('Exact existing PROJECT_ID required');
  return { organization, projectId: env.PROJECT_ID, origin: origin.origin };
}

/** Admission is provisioned explicitly. Empty or missing configuration authorizes nobody. */
let admissionsCache;
function admissions(env, config) {
  const source = env.ADMISSIONS_JSON || '[]';
  if (!admissionsCache || admissionsCache.source !== source ||
      admissionsCache.organization !== config.organization || admissionsCache.projectId !== config.projectId) {
    const values = JSON.parse(source);
    if (!Array.isArray(values)) throw new TypeError('ADMISSIONS_JSON must be an array');
    const seen = new Set();
    for (const item of values) {
      if (!item || item.organization !== config.organization || item.projectId !== config.projectId ||
          typeof item.githubUserId !== 'string' || !/^[1-9][0-9]*$/.test(item.githubUserId) || typeof item.ownerId !== 'string' || !item.ownerId ||
          !Array.isArray(item.clientIds) || !item.clientIds.length || !item.clientIds.every((v) => typeof v === 'string' && v.length) ||
          !Array.isArray(item.providers) || !item.providers.length || !item.providers.every(v=>v!==null&&validProvider(v)) ||
          !Array.isArray(item.scopes) || !item.scopes.length || !item.scopes.every((v) => SCOPES.includes(v)) ||
          seen.has(item.githubUserId)) throw new TypeError('Invalid or ambiguous admission');
      seen.add(item.githubUserId);
    }
    // Parsed once per binding content; callers only read, so freeze the snapshot.
    admissionsCache = {source, organization: config.organization, projectId: config.projectId,
      values: Object.freeze(values.map((item) => Object.freeze(item)))};
  }
  return admissionsCache.values;
}

function consentPage(description, handle, organization) {
  return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>授权 ${escape(organization)}</title>
<h1>允许 ${escape(description.clientName)} 访问 ${escape(organization)}？</h1>
<p>${description.clientDomain ? `客户端域名：<strong>${escape(description.clientDomain)}</strong>` : '客户端名称由申请方提供，尚未经域名验证。'}</p>
<p>回调目标：<strong>${escape(description.redirectHost)}</strong></p>
${description.redirectIsLoopback ? '<p>授权将返回本机应用。请仅在你刚发起登录时继续。</p>' : ''}
<form method="post"><input type="hidden" name="handle" value="${escape(handle)}">
${description.scope.map((scope) => `<label><input type="checkbox" name="scope" value="${escape(scope)}" checked>${escape(scope)}</label><br>`).join('')}
<p>继续后将通过 GitHub 确认你的身份。仅允许已批准的账号与客户端。</p>
<button name="decision" value="approve">继续</button> <button name="decision" value="deny">拒绝</button></form></html>`;
}

/**
 * Only this closure may construct a downstream identity. The injected handler is a local-test
 * capability; the production entry point below never supplies one.
 */
export function createAuthApplication(config, { taskHandler } = {}) {
  const resources = [`${config.origin}/mcp`, `${config.origin}/v1/tasks`];
  const authorizationServer = new OAuthAuthorizationServer({
    issuer: config.origin,
    resources,
    scopesSupported: SCOPES,
    accessTokenTTL: 900,
    // Pre-registered clients only until exact native-host registrations are approved.
    clientIdMetadataDocumentEnabled: false,
    allowTokenExchangeGrant: false,
    onError: ({ code }) => { /* Never log bearer tokens, claims, callback URLs or request data. */ },
  });

  async function downstream(request, identity, env) {
    const headers = new Headers(request.headers);
    for (const name of TRUST_HEADERS) headers.delete(name);
    const cleanRequest = new Request(request, { headers });
    if (taskHandler) return taskHandler(cleanRequest, identity, env);
    if (!env.TASK_SERVICE?.handleAuthenticated) return deny('task_service_not_configured', 503);
    // Service binding RPC is the trust boundary. No identity header crosses public HTTP.
    return env.TASK_SERVICE.handleAuthenticated(cleanRequest, identity);
  }

  async function workload(request, env, token, decoded) {
    let identity;
    try {identity = await authenticateGitHubWorkload(token, env, config, decoded);}
    catch {return deny('workload_token_rejected', 401);}
    const scope = workloadOperation(request);
    if (!scope || !identity.scopes.includes(scope)) return deny('workload_operation_denied');
    return downstream(request, identity, env);
  }

  async function authorized(request, env, ctx) {
    const isMcp = new URL(request.url).pathname.startsWith('/mcp');
    const needed = requiredScopesFor(request, isMcp);
    if (!needed.every((scope) => ctx.auth.scope.includes(scope))) return insufficientScope(ctx.auth, needed);
    const clientPolicy=env.AUTH_CLIENTS?.[ctx.auth.clientId];
    if(!clientPolicy||!clientPolicy.resources.includes(ctx.auth.audience)||ctx.auth.scope.some(s=>!clientPolicy.scopes.includes(s)))return deny('client_policy_required');
    const props = ctx.props;
    const provider = props?.provider;
    if (!props || props.provider!==clientPolicy.provider || props.organization !== config.organization || props.projectId !== config.projectId ||
        !validProvider(provider) || typeof props.githubUserId !== 'string' ||
        ctx.auth.userId !== `github-${props.githubUserId}`) return deny('identity_mismatch');
    const admission = admissions(env, config).find((item) => item.githubUserId === props.githubUserId &&
      item.ownerId === props.ownerId && item.clientIds.includes(ctx.auth.clientId) && (provider===null || item.providers.includes(provider)) &&
      ctx.auth.scope.every((scope) => item.scopes.includes(scope)));
    if (!admission) return deny('admission_required');
    const identity = Object.freeze({
      organization: config.organization,
      projectId: config.projectId,
      // ownerId is an explicitly admitted existing repo owner; it is never inferred from GitHub.
      ownerId: admission.ownerId,
      githubUserId: admission.githubUserId,
      provider,
      scopes: Object.freeze([...ctx.auth.scope]),
      resource: ctx.auth.audience,
      clientId: ctx.auth.clientId,
    });
    return downstream(request, identity, env);
  }

  const resourceServer = (resource, request, env) => new OAuthResourceServer({
    resourceMetadata: { resource, authorization_servers: [config.origin] },
    // Advertise configured MCP worker capabilities so clients can request them at consent.
    // The library only advertises these scopes; authorized() retains operation enforcement.
    // REST still asks only for this operation's minimum, not read+submit for a POST.
    requiredScopes: advertisedScopesFor(resource, request, env),
    validateToken: (env) => (audience, token) => authorizationServer.validateToken(audience, token, env),
    handler: { fetch: authorized },
  });

  async function authorize(request, env) {
    const oauth = authorizationServer.getOAuthApi(env);
    try {
      if (request.method === 'GET') {
        const parsed = await oauth.parseAuthRequest(request);
        const rawResources = new URL(request.url).searchParams.getAll('resource');
        if (rawResources.length !== 1 || !resources.includes(rawResources[0])) {
          throw new AuthorizationError('invalid_target', { description: 'An exact resource is required' });
        }
        if (!parsed.codeChallenge || parsed.codeChallengeMethod !== 'S256') {
          throw new AuthorizationError('invalid_request', { description: 'PKCE S256 is required' });
        }
        if (!parsed.scope.length || parsed.scope.some((scope) => !SCOPES.includes(scope))) {
          throw new AuthorizationError('invalid_scope', { description: 'Unsupported or empty scope' });
        }
        const clientPolicy=env.AUTH_CLIENTS?.[parsed.clientId];
        if(!clientPolicy||!clientPolicy.resources.includes(rawResources[0])||parsed.scope.some(s=>!clientPolicy.scopes.includes(s)))return deny('client_policy_required');
        if (!admissions(env, config).length) return deny('admission_required');
        const description = await oauth.describeConsent(parsed);
        const consent = await oauth.beginConsent(parsed);
        consent.headers.set('Content-Type', 'text/html; charset=utf-8');
        return new Response(consentPage(description, consent.handle, config.organization), { headers: consent.headers });
      }
      if (request.method === 'POST') {
        const form = await request.formData();
        const handle = String(form.get('handle') || '');
        if (form.get('decision') === 'deny') {
          const denied = await oauth.denyConsent(request, handle);
          return new Response(null, { status: 302, headers: denied.headers });
        }
        if (form.get('decision') !== 'approve') return deny('invalid_decision', 400);
        if(!env.GITHUB_CLIENT_ID||!env.GITHUB_CLIENT_SECRET)return deny('github_login_not_configured',503);
        const approved=await oauth.approveConsent(request,handle,{scope:form.getAll('scope').map(String)});
        const clientPolicy=env.AUTH_CLIENTS?.[approved.request.clientId];
        if(!clientPolicy||approved.request.scope.some(s=>!clientPolicy.scopes.includes(s)))return deny('client_policy_required');
        const verifier=crypto.randomUUID()+crypto.randomUUID();
        const challenge=btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier))))).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
        const upstream=await oauth.beginUpstream(approved.request,{data:{verifier},headers:approved.headers});
        const target=new URL('https://github.com/login/oauth/authorize');
        target.search=new URLSearchParams({client_id:env.GITHUB_CLIENT_ID,redirect_uri:config.origin+'/callback',scope:'read:user',state:upstream.state,code_challenge:challenge,code_challenge_method:'S256'}).toString();
        upstream.headers.set('Location',target.href);
        return new Response(null,{status:302,headers:upstream.headers});
      }
      return deny('method_not_allowed', 405);
    } catch (error) {
      if (error instanceof AuthorizationError) {
        if (error.redirectTo) return Response.redirect(error.redirectTo, 302);
        return deny(error.code, 400);
      }
      if (error instanceof CimdFetchError) return deny('client_metadata_unavailable', 400);
      throw error;
    }
  }

  async function callback(request,env){
    const oauth=authorizationServer.getOAuthApi(env);
    let stage='upstream_state';
    try{
      const upstream=await oauth.finishUpstream(request),url=new URL(request.url);
      if(url.searchParams.has('error')){upstream.headers.set('Location',authorizationErrorRedirect(upstream.request,'access_denied'));return new Response(null,{status:302,headers:upstream.headers});}
      const code=url.searchParams.get('code');
      if(!code||!env.GITHUB_CLIENT_ID||!env.GITHUB_CLIENT_SECRET)return deny('github_login_unavailable',503);
      if(typeof upstream.data?.verifier!=='string')return deny('upstream_verifier_missing',400);
      stage='github_exchange';
      const exchanged=await fetch('https://github.com/login/oauth/access_token',{method:'POST',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({client_id:env.GITHUB_CLIENT_ID,client_secret:env.GITHUB_CLIENT_SECRET,code,redirect_uri:config.origin+'/callback',code_verifier:upstream.data.verifier}),redirect:'manual',signal:AbortSignal.timeout(10000)});
      stage='github_exchange_response';
      const token=await exchanged.json();
      if(!exchanged.ok||typeof token.access_token!=='string')return deny('github_exchange_failed',403);
      stage='github_identity';
      const userReply=await fetch('https://api.github.com/user',{headers:{Authorization:'Bearer '+token.access_token,Accept:'application/vnd.github+json','User-Agent':'personal-ai-cloudflare','X-GitHub-Api-Version':'2022-11-28'},redirect:'manual',signal:AbortSignal.timeout(10000)});
      const user=await userReply.json();
      if(!userReply.ok||!Number.isSafeInteger(user.id)||user.id<1)return deny('github_identity_failed',403);
      const clientPolicy=env.AUTH_CLIENTS?.[upstream.request.clientId];
      const admission=admissions(env,config).find(a=>a.githubUserId===String(user.id)&&a.clientIds.includes(upstream.request.clientId));
      if(!clientPolicy||!clientPolicy.resources.includes(upstream.request.resource)||!admission||upstream.request.scope.some(s=>!clientPolicy.scopes.includes(s)||!admission.scopes.includes(s))||clientPolicy.provider!==null&&!admission.providers.includes(clientPolicy.provider))return deny('admission_required');
      stage='grant_completion';
      const complete=await oauth.completeAuthorization({request:upstream.request,userId:'github-'+user.id,metadata:{identityProvider:'github'},scope:upstream.request.scope,props:{organization:config.organization,projectId:config.projectId,ownerId:admission.ownerId,githubUserId:String(user.id),provider:clientPolicy.provider}});
      upstream.headers.set('Location',complete.redirectTo);
      return new Response(null,{status:302,headers:upstream.headers});
    }catch(error){if(error instanceof AuthorizationError)return deny('upstream_state_rejected',400);return Response.json({error:'github_login_unavailable',stage,failure_type:error.name},{status:503,headers:{'Cache-Control':'no-store'}});}
  }

  return {
    authorizationServer,
    async fetch(request, env, ctx) {
      const url = new URL(request.url);
      if (url.origin !== config.origin) return deny('unexpected_origin', 421);
      try {
        if (url.pathname === '/authorize') return await authorize(request, env);
        if (url.pathname === '/callback' && request.method==='GET') return callback(request,env);
        if (url.pathname === '/v1/tasks' || url.pathname.startsWith('/v1/tasks/')) {
          const parsed = githubWorkloadToken(request);
          if (parsed !== null) return await workload(request, env, parsed.token, parsed.decoded);
        }
        for (let index = 0; index < resources.length; index++) {
          const path = new URL(resources[index]).pathname;
          if (url.pathname === path || url.pathname.startsWith(`${path}/`) ||
              url.pathname === `/.well-known/oauth-protected-resource${path}`) {
            return await resourceServer(resources[index], request, env).fetch(request, env, ctx);
          }
        }
        if (url.pathname === '/.well-known/oauth-authorization-server' || url.pathname === '/oauth/token') {
          return await authorizationServer.fetch(request, env, ctx);
        }
        return deny('not_found', 404);
      } catch {
        return deny('authentication_service_unavailable', 503);
      }
    },
  };
}
