"""Owner-bound App authentication and a durable, conservative comment outbox.

Private key references are opaque. An approved host adapter must supply key_loader;
this module never reads a key file/environment or falls back to user credentials.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import jwt

from .auth import grant_allows_project, project_belongs_to, valid_owner
from .runtime import Conflict
from .transport import PublicHTTPS, json_body

API = 'https://api.github.com'


class AppRejected(ValueError):
    """A definite authentication, routing or API rejection (safe fixed message)."""


def validate_apps(config):
    apps = config.get('github_apps')
    if apps is None:
        return
    owner = config.get('owner')
    if not valid_owner(owner) or not isinstance(apps, dict) or not apps:
        raise ValueError('App comments require an owner and explicit worker routes')
    bindings = {}
    for identity, route in apps.items():
        if (not isinstance(identity, str) or not identity or not isinstance(route, dict)
                or set(route) != {'provider', 'app_id', 'installation_id', 'private_key_ref'}
                or any(type(route[name]) is not int or route[name] <= 0 for name in ('app_id', 'installation_id'))
                or not isinstance(route['private_key_ref'], str)
                or re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', route['private_key_ref']) is None
                or not isinstance(route['provider'], str)
                or config['providers'].get(route['provider'], {}).get('type') not in {'dot_events', 'grok_webhook', 'claude_routine'}):
            raise ValueError('invalid App worker route')
        grants = [g for g in config['grants'] if g['identity'] == identity and 'tasks:work' in g['scopes']]
        if not grants or any(g.get('provider') != route['provider'] for g in grants):
            raise ValueError('App route must match an admitted worker provider')
        # Several connections to the same provider may share the same App.
        binding = (route['provider'], route['installation_id'], route['private_key_ref'])
        if route['app_id'] in bindings and bindings[route['app_id']] != binding:
            raise ValueError('conflicting App binding')
        bindings[route['app_id']] = binding
    installations = {}
    references = {}
    for route in apps.values():
        for seen, name in ((installations, 'installation_id'), (references, 'private_key_ref')):
            if route[name] in seen and seen[route[name]] != route['app_id']:
                raise ValueError('conflicting App credential or installation')
            seen[route[name]] = route['app_id']
    for grant in config['grants']:
        if grant.get('provider') and 'tasks:work' in grant['scopes'] and grant['identity'] not in apps:
            raise ValueError('App route missing for an admitted worker')


class GitHubApps:
    def __init__(self, config, *, key_loader=None, transport=None):
        validate_apps(config)
        self.owner = config['owner']
        self.routes = json.loads(json.dumps(config['github_apps']))
        self.grants = config['grants']
        self.projects = frozenset(config['projects'])
        self.key_loader = key_loader
        self.transport = transport or PublicHTTPS(max_response_bytes=2_000_000)
        self._tokens = {}
        self._lock = threading.Lock()

    def select(self, project, worker, provider):
        route = self.routes.get(worker)
        if (project not in self.projects or not project_belongs_to(project, self.owner)
                or route is None or route['provider'] != provider
                or not any(g['identity'] == worker and g.get('provider') == provider
                           and 'tasks:work' in g['scopes'] and grant_allows_project(g, project)
                           for g in self.grants)):
            raise AppRejected('App route rejected')
        return route.copy()

    def _request(self, path, token, *, method='GET', body=None):
        # Paths are constructed locally; callers never supply an API host/URL.
        reply = self.transport.request(API + path, method=method, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json', 'User-Agent': 'ai-service-app-comments',
            'X-GitHub-Api-Version': '2022-11-28'}, body=b'' if body is None else json_body(body))
        if reply.status >= 500 or reply.status in {408, 425, 429}:
            raise RuntimeError('GitHub API temporarily unavailable')
        if reply.status not in {200, 201}:
            raise AppRejected('GitHub API rejected')
        return json.loads(reply.body)

    def credentials(self, project, worker, provider, expected):
        route = self.select(project, worker, provider)
        if route != expected:
            raise AppRejected('App binding changed')
        cache_key = (self.owner, worker, provider, project, *route.values())
        with self._lock:
            cached = self._tokens.get(cache_key)
            if cached and time.time() < cached[0] - 60:
                return cached[1:]
            if self.key_loader is None:
                raise RuntimeError('approved App key loader unavailable')
            try:
                now = int(time.time())
                signed = jwt.encode({'iss': str(route['app_id']), 'iat': now - 60, 'exp': now + 540},
                                    self.key_loader(route['private_key_ref']), algorithm='RS256')
                app = self._request('/app', signed)
                installation = self._request('/app/installations/' + str(route['installation_id']), signed)
                if (app['id'] != route['app_id'] or app['owner']['login'] != self.owner
                        or not isinstance(app['slug'], str) or re.fullmatch(r'[a-z0-9-]+', app['slug']) is None
                        or installation['id'] != route['installation_id']
                        or installation['app_id'] != route['app_id']
                        or installation['account']['login'] != self.owner
                        or installation.get('suspended_at') is not None
                        or installation['permissions'].get('issues') != 'write'):
                    raise AppRejected('App identity rejected')
                issued = self._request('/app/installations/' + str(route['installation_id']) + '/access_tokens', signed,
                                       method='POST', body={'repositories': [project.split('/')[1]],
                                                            'permissions': {'issues': 'write'}})
                expiry = datetime.fromisoformat(issued['expires_at'].replace('Z', '+00:00'))
                permissions = issued['permissions']
                if (expiry.tzinfo is None or expiry <= datetime.now(timezone.utc)
                        or not isinstance(issued['token'], str) or not issued['token']
                        or any(c in issued['token'] for c in '\r\n')
                        or not isinstance(permissions, dict) or permissions.get('issues') != 'write'
                        or set(permissions) - {'issues', 'metadata'}
                        or permissions.get('metadata', 'read') != 'read'
                        or [r['full_name'] for r in issued['repositories']] != [project]):
                    raise AppRejected('installation token scope rejected')
                value = (expiry.timestamp(), issued['token'], app['slug'])
                self._tokens[cache_key] = value
                return value[1:]
            except AppRejected:
                raise
            except Exception:
                # Never retain an exception whose message could contain a key/JWT/token.
                raise RuntimeError('App authentication unavailable') from None

    def issue(self, project, number, token):
        issue = self._request(f'/repos/{project}/issues/{number}', token)
        if (issue.get('number') != number or 'pull_request' in issue
                or issue.get('repository_url') != f'{API}/repos/{project}'):
            raise AppRejected('issue target rejected')

    @staticmethod
    def comment_matches(comment, body, route, slug, project, number):
        return (isinstance(comment, dict) and type(comment.get('id')) is int and comment['id'] > 0
                and comment.get('body') == body
                and comment.get('issue_url') == f'{API}/repos/{project}/issues/{number}'
                and comment.get('performed_via_github_app', {}).get('id') == route['app_id']
                and comment.get('user', {}).get('type') == 'Bot'
                and comment.get('user', {}).get('login') == slug + '[bot]')

    def find(self, project, number, token, body, route, slug):
        # Bounded pages: an incomplete/failed scan remains UNKNOWN, never a resend.
        for page in range(1, 101):
            comments = self._request(f'/repos/{project}/issues/{number}/comments?per_page=100&page={page}', token)
            if not isinstance(comments, list):
                raise ValueError('invalid comment page')
            for comment in comments:
                if self.comment_matches(comment, body, route, slug, project, number):
                    return comment['id']
            if len(comments) < 100:
                return None
        return None

    def post(self, project, number, token, body):
        # Exactly one attempt. Interpret failure only after durable sending intent.
        return self.transport.request(f'{API}/repos/{project}/issues/{number}/comments', method='POST',
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'User-Agent': 'ai-service-app-comments',
                     'X-GitHub-Api-Version': '2022-11-28'}, body=json_body({'body': body}))


class GitHubComments:
    def __init__(self, runtime, apps):
        self.runtime, self.apps = runtime, apps
        with runtime.connection() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS github_comments (
                delivery_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, project TEXT NOT NULL,
                worker TEXT NOT NULL, provider TEXT NOT NULL, issue INTEGER NOT NULL,
                route TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
                comment_id INTEGER, error TEXT, checked REAL NOT NULL DEFAULT 0)''')
        runtime.github_comments = self

    def validate_issue(self, project, request):
        issue = request.get('issue')
        if issue is None:
            if 'issue' in request:
                raise ValueError('issue must be an explicit reference')
            return
        if (not isinstance(issue, dict) or set(issue) != {'repository', 'number'}
                or issue['repository'] != project or type(issue['number']) is not int or issue['number'] <= 0
                or project not in self.apps.projects or not project_belongs_to(project, self.apps.owner)
                or request.get('mode') != 'agent'):
            raise ValueError('issue must bind the same admitted agent project')

    def enqueue(self, db, row, operation):
        issue = json.loads(row['request']).get('issue')
        if issue is None:
            return
        route = self.apps.select(row['owner'], row['worker'], row['provider'])
        key = hashlib.sha256(json_body([self.apps.owner, row['owner'], row['id'], row['attempt'],
                                       row['revision'], operation])).hexdigest()
        attempt = hashlib.sha256(row['attempt'].encode()).hexdigest()[:16]
        body = (f"Task {row['id']}: {operation} / {row['status']}. "
                f"Attempt {attempt}, revision {row['revision']}.\n\n<!-- ai-service-comment:v1:{key} -->")
        serialized_route = json.dumps(route, sort_keys=True)
        old = db.execute('SELECT body,route FROM github_comments WHERE delivery_key=?', (key,)).fetchone()
        if old:
            if old['body'] != body or old['route'] != serialized_route:
                raise Conflict('comment key binds different content or App')
            return
        db.execute('INSERT INTO github_comments(delivery_key,task_id,project,worker,provider,issue,route,body,state) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (key, row['id'], row['owner'], row['worker'], row['provider'],
                                               issue['number'], serialized_route, body, 'pending'))

    def status(self, task_id):
        with self.runtime.connection() as db:
            return [dict(row) for row in db.execute('SELECT delivery_key,state,comment_id,error FROM github_comments '
                                                    'WHERE task_id=? ORDER BY rowid', (task_id,))]

    def _state(self, key, state, *, comment_id=None, error=None):
        with self.runtime.connection() as db:
            db.execute('UPDATE github_comments SET state=?,comment_id=?,error=? WHERE delivery_key=?',
                       (state, comment_id, error, key))

    def flush(self):
        # Cross-process lock covers recovery AND networking; another sender cannot
        # recover a live POST. A process death releases the lock, leaving sending.
        fd = os.open(self.runtime.path.with_suffix('.comments.lock'), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            with self.runtime.connection() as db:
                db.execute("UPDATE github_comments SET state='unknown',error='interrupted_send' WHERE state='sending'")
                rows = db.execute("SELECT * FROM github_comments WHERE state IN ('pending','unknown') ORDER BY checked,rowid LIMIT 100").fetchall()
            for row in rows:
                self._deliver(row)
        finally:
            os.close(fd)

    def _deliver(self, row):
        key, project, number, body = row['delivery_key'], row['project'], row['issue'], row['body']
        with self.runtime.connection() as db:
            db.execute('UPDATE github_comments SET checked=? WHERE delivery_key=?', (time.time(), key))
        unknown = row['state'] == 'unknown'
        try:
            route = json.loads(row['route'])
            token, slug = self.apps.credentials(project, row['worker'], row['provider'], route)
            self.apps.issue(project, number, token)
        except AppRejected:
            self._state(key, 'unknown' if unknown else 'rejected', error='preflight_rejected')
            return
        except Exception:
            self._state(key, 'unknown' if unknown else 'pending', error='preflight_unavailable')
            return
        if unknown:
            try:
                found = self.apps.find(project, number, token, body, route, slug)
            except Exception:
                return
            if found is not None:
                self._state(key, 'sent', comment_id=found)
            return
        self._state(key, 'sending')
        try:
            reply = self.apps.post(project, number, token, body)
            if reply.status in {400, 401, 403, 404, 410, 422, 429}:
                self._state(key, 'rejected', error='comment_rejected')
                return
            if reply.status == 201:
                comment = json.loads(reply.body)
                if self.apps.comment_matches(comment, body, route, slug, project, number):
                    self._state(key, 'sent', comment_id=comment['id'])
                    return
        except Exception:
            pass
        self._state(key, 'unknown', error='comment_outcome_unknown')
