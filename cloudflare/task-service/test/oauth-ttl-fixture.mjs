// Local workerd fixture only; never imported by the production entry point.
import assert from 'node:assert/strict';
import {createAuthApplication} from '../src/auth.mjs';

export default {
  async fetch() {
    const originalNow = Date.now;
    let now = 2000000000000;
    Date.now = () => now;
    try {
      const records = new Map();
      let writes = 0;
      const kv = {
        async get(key, options) {
          const value = records.get(key)?.value;
          return value === undefined ? null :
            (options === 'json' || options?.type === 'json' ? JSON.parse(value) : value);
        },
        async put(key, value, options) {
          writes++;
          records.set(key, {value, ttl: options?.expirationTtl});
        },
        async delete(key) { records.delete(key); },
        async list({prefix = ''} = {}) {
          return {keys: [...records.keys()].filter(name => name.startsWith(prefix)).map(name => ({name})), list_complete: true};
        },
      };
      const env = {OAUTH_KV: kv}, origin = 'https://tasks.example.test';
      const resource = origin + '/mcp';
      const {authorizationServer: server} = createAuthApplication({origin});
      const api = server.getOAuthApi(env);
      const client = await api.createClient({clientName: 'Synthetic TTL test',
        redirectUris: ['http://localhost:8989/callback'], tokenEndpointAuthMethod: 'none',
        grantTypes: ['authorization_code', 'refresh_token'], responseTypes: ['code']});
      const verifier = 'synthetic-verifier-for-local-oauth-lifetime-test-only';
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
      const challenge = btoa(String.fromCharCode(...new Uint8Array(digest)))
        .replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
      const parsed = await api.parseAuthRequest(new Request(origin + '/authorize?' + new URLSearchParams({
        response_type: 'code', client_id: client.clientId, redirect_uri: 'http://localhost:8989/callback',
        scope: 'tasks:read', resource, code_challenge: challenge, code_challenge_method: 'S256',
      })));
      const grant = await api.completeAuthorization({request: parsed, userId: 'synthetic-user',
        scope: ['tasks:read'], metadata: {synthetic: true}, props: {synthetic: true}});
      const exchange = async body => {
        const response = await server.fetch(new Request(origin + '/oauth/token', {
          method: 'POST', body: new URLSearchParams({...body, client_id: client.clientId, resource}),
        }), env, {});
        assert.equal(response.status, 200);
        return response.json();
      };
      const issued = await exchange({grant_type: 'authorization_code',
        code: new URL(grant.redirectTo).searchParams.get('code'),
        redirect_uri: 'http://localhost:8989/callback', code_verifier: verifier});
      assert.equal(issued.expires_in, 3600, 'issued access token lifetime');
      const issuedAt = now / 1000;
      const beforeReuse = writes;
      for (const seconds of [0, 901, 1800, 3599]) {
        now = (issuedAt + seconds) * 1000;
        const valid = await server.validateToken(resource, issued.access_token, env);
        assert.ok(valid, 'reuse before expiry');
        assert.equal(valid.expiresAt, issuedAt + 3600, 'persisted access token expiry');
      }
      assert.equal(writes, beforeReuse, 'reuse must not write or silently extend expiry');
      assert.ok([...records.values()].some(record => record.ttl === 3600), 'KV access token TTL');
      // A client may refresh shortly before expiry; the refreshed pair has a fresh one-hour lifetime.
      const refreshedAt = now / 1000;
      const rotated = await exchange({grant_type: 'refresh_token', refresh_token: issued.refresh_token});
      assert.equal(rotated.expires_in, 3600, 'refreshed access token lifetime');
      assert.ok(rotated.access_token !== issued.access_token, 'access token rotation');
      assert.ok(rotated.refresh_token !== issued.refresh_token, 'refresh token rotation');
      const beforeRotatedReuse = writes;
      now = (issuedAt + 3601) * 1000;
      assert.equal(await server.validateToken(resource, issued.access_token, env), null, 'expired token rejected');
      now = (refreshedAt + 3599) * 1000;
      const valid = await server.validateToken(resource, rotated.access_token, env);
      assert.ok(valid, 'refreshed token reusable before expiry');
      assert.equal(valid.expiresAt, refreshedAt + 3600);
      assert.equal(await server.validateToken(origin + '/v1/tasks', rotated.access_token, env), null, 'wrong audience rejected');
      assert.equal(writes, beforeRotatedReuse, 'refreshed token reuse must not write');
      await api.completeAuthorization({request: parsed, userId: 'synthetic-user',
        scope: ['tasks:read'], metadata: {synthetic: true}, props: {synthetic: true}});
      assert.equal(await server.validateToken(resource, rotated.access_token, env), null, 'revocation still enforced');
      return Response.json({passed: true});
    } catch (error) {
      if (error.code === 'ERR_ASSERTION' && typeof error.actual === 'number' && typeof error.expected === 'number') {
        return Response.json({passed: false, actual: error.actual, expected: error.expected});
      }
      return Response.json({passed: false});
    } finally {
      Date.now = originalNow;
    }
  },
};
