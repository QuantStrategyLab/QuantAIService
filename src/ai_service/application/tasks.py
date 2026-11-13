"""Task API operations (lookup, submit, read, messages, cancel).

Called from the HTTP transport in ``ai_service.server``; this module does not
own sockets or status-line framing.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from ..runtime import NotFound


class TaskOperationsMixin:
    def public_record(self, record):
        result = dict(record)
        if self.runtime.github_comments and 'request' in result and 'id' in result:
            result['github_comments'] = self.runtime.github_comments.status(result['id'])
        if isinstance(result.get('handle'), dict):
            # Inference adapter keeps its result in the private durable handle until
            # schema validation. Only the validated top-level output is public.
            result['handle'] = {key: value for key, value in result['handle'].items() if key != 'output'}
        return result

    def worker(self, principal, task_id):
        self.require(principal, 'tasks:work')
        record = self.runtime.get(principal.project, task_id)
        if not principal.provider or record['provider'] != principal.provider:
            raise PermissionError('worker is not assigned to this task')
        return record

    def read_task(self, principal, task_id):
        self.require(principal, 'tasks:read')
        # owner is a project, not a subject. Preserve GET's provider boundary.
        return (self.worker(principal, task_id) if principal.provider
                else self.runtime.get(principal.project, task_id))

    def dispatch(self, method, target, headers, body=None, *, principal=None):
        path = urlsplit(target).path
        if method == 'GET' and path == '/.well-known/oauth-protected-resource':
            return 200, self.auth.resource_metadata()
        principal = principal or self.authenticate(headers)
        if path == '/mcp' and method == 'POST':
            reply = self.mcp(principal, body)
            return (202, None) if reply is None else (200, reply)
        if path == '/mcp' and method == 'GET':
            # This stateless endpoint does not offer an SSE listening stream.
            return 405, {'error': 'method_not_allowed'}
        selectors = parse_qs(urlsplit(target).query, keep_blank_values=True).get('project', [None])
        if len(selectors) != 1:
            raise ValueError('select exactly one project')
        if path == '/v1/tasks/lookup' and method == 'GET':
            if body is not None:
                raise ValueError('lookup takes no body')
            try:
                principal = self.project_context(principal, selectors[0])
                self.require(principal, 'tasks:read')
                record = self.runtime.lookup(principal.project, headers.get('Idempotency-Key'),
                                             headers.get('X-Request-SHA256'))
                # Check the very same object visibility as GET before exposing its ID.
                # A changed snapshot is unknown; it does not authorize new work.
                if self.read_task(principal, record['id']) != record:
                    raise NotFound('task not found')
            except (PermissionError, NotFound):
                raise NotFound('task not found') from None
            return 200, {'id': record['id']}
        principal = self.project_context(principal, selectors[0])
        if path == '/v1/tasks' and method == 'POST':
            self.require(principal, 'tasks:submit')
            return 202, self.public_record(self.runtime.submit(principal.project, headers.get('Idempotency-Key'), body))
        parts = path.strip('/').split('/')
        if len(parts) in {3, 4} and parts[:2] == ['v1', 'tasks']:
            task_id = parts[2]
            operation = parts[3] if len(parts) == 4 else ''
            if method == 'GET':
                record = self.read_task(principal, task_id)
                if operation == '':
                    return 200, {**self.public_record(record), 'messages': self.runtime.messages(principal.project, task_id)}
                if operation == 'events':
                    after = int(parse_qs(urlsplit(target).query).get('after', ['0'])[0])
                    return 200, {'events': self.runtime.events(principal.project, task_id, after)}
            if method == 'POST':
                self.require(principal, 'tasks:submit')
                if operation == 'messages':
                    return 200, self.public_record(self.runtime.message(principal.project, task_id, headers.get('Idempotency-Key'), body))
                if operation == 'cancel':
                    if body not in (None, {}):
                        raise ValueError('cancel takes no arguments')
                    return 200, self.public_record(self.runtime.cancel(principal.project, task_id))
        raise NotFound('endpoint not found')
