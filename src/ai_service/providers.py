"""Native persistent-agent triggers. Results arrive through authenticated tools."""
from __future__ import annotations

from urllib.parse import urlsplit

from .runtime import SubmissionRejected
from .transport import PublicHTTPS, json_body


class GrokWebhookProvider:
    mode = 'agent'
    callback_driven = True

    def __init__(self, webhook_url, key, task_resource, *, transport=None):
        parsed = urlsplit(webhook_url)
        resource = urlsplit(task_resource)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Grok webhook must be a configured HTTPS URL')
        if resource.scheme != 'https' or not resource.hostname or resource.username or resource.password:
            raise ValueError('task resource must be HTTPS')
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise ValueError('Grok webhook key is required')
        self._url, self._key = webhook_url, key
        self._resource = task_resource
        self._transport = transport or PublicHTTPS()

    def submit(self, request, *, task_id, owner):
        # The fixed routine reads the task through project-scoped OAuth tools.
        # No credentials, business materials or executable instructions in the trigger.
        self.notify(task_id=task_id, owner=owner, change='requested', revision=1)
        return {'task_id': task_id, 'acceptance': 'routine_started',
                'model_verification': 'unavailable'}

    def notify(self, *, task_id, owner, change, revision):
        reply = self._transport.request(self._url,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key},
            body=json_body({'type': 'task.requested', 'task_id': task_id,
                            'project': owner, 'resource': self._resource,
                            'change': change, 'revision': revision}))
        if reply.status >= 500:
            raise RuntimeError('routine acceptance unknown')
        if reply.status != 200:
            # Cursor explicitly documents non-200 as "a run did not start".
            raise SubmissionRejected('native routine rejected the trigger')

    def cancel(self, handle):
        # No documented native cancellation API. The task tool exposes cancel_requested;
        # only the authenticated execution attempt can confirm it stopped.
        return None


class ClaudeRoutineProvider(GrokWebhookProvider):
    """Claude Code subscription routine, not an Anthropic inference API key."""

    def __init__(self, webhook_url, key, task_resource, *, transport=None):
        import re
        parsed = urlsplit(webhook_url)
        if (parsed.scheme != 'https' or parsed.netloc != 'api.anthropic.com'
                or re.fullmatch(r'/v1/claude_code/routines/trig_[A-Za-z0-9]+/fire', parsed.path) is None
                or parsed.query or parsed.fragment):
            raise ValueError('Claude routine must use the official fire endpoint')
        super().__init__(webhook_url, key, task_resource, transport=transport)

    def notify(self, *, task_id, owner, change, revision):
        event = {'type': 'task.requested', 'task_id': task_id, 'project': owner,
                 'resource': self._resource, 'change': change, 'revision': revision}
        reply = self._transport.request(self._url,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key,
                     'anthropic-version': '2026-10-21'},
            body=json_body({'text': json_body(event).decode('utf-8')}))
        if reply.status >= 500:
            raise RuntimeError('routine acceptance unknown')
        if reply.status != 200:
            raise SubmissionRejected('native routine rejected the trigger')


class MuseCloudAgentProvider:
    """Launch a Cursor Cloud Agent; results arrive through muse worker MCP claim."""

    mode = 'agent'
    callback_driven = True

    def __init__(self, launch_url, key, task_resource, *, model=None, repository=None, transport=None):
        import base64
        parsed = urlsplit(launch_url)
        resource = urlsplit(task_resource)
        if (parsed.scheme != 'https' or parsed.netloc != 'api.cursor.com'
                or parsed.path != '/v1/agents' or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError('Muse launch URL must be https://api.cursor.com/v1/agents')
        if resource.scheme != 'https' or not resource.hostname or resource.username or resource.password:
            raise ValueError('task resource must be HTTPS')
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise ValueError('Muse Cloud Agent API key is required')
        if model is not None and (not isinstance(model, str) or not model or '\r' in model or '\n' in model):
            raise ValueError('Muse model id is invalid')
        if repository is not None:
            repo = urlsplit(repository)
            if repo.scheme != 'https' or not repo.hostname or repo.username or repo.password or repo.fragment:
                raise ValueError('Muse repository must be a configured HTTPS URL')
        self._url, self._key = launch_url, key
        self._resource = task_resource
        self._model = model
        self._repository = repository
        self._transport = transport or PublicHTTPS()
        self._authorization = 'Basic ' + base64.b64encode((key + ':').encode('ascii')).decode('ascii')

    def submit(self, request, *, task_id, owner):
        self.notify(task_id=task_id, owner=owner, change='requested', revision=1)
        return {'task_id': task_id, 'acceptance': 'agent_started',
                'model_verification': 'unavailable'}

    def notify(self, *, task_id, owner, change, revision):
        # Identifiers only — no materials. The Cloud Agent uses a preconfigured
        # Muse Wonder + muse worker MCP connection to claim/report.
        event = {'type': 'task.requested', 'task_id': task_id, 'project': owner,
                 'resource': self._resource, 'change': change, 'revision': revision}
        launch = {
            'prompt': {
                'text': (
                    'AI Service task notification. Identifiers only. '
                    'Authenticate to the MCP resource as the muse worker, '
                    'then claim_task and report_task_result.\n'
                    + json_body(event).decode('utf-8')
                ),
            },
        }
        if self._model:
            launch['model'] = {'id': self._model}
        if self._repository:
            launch['repos'] = [{'url': self._repository}]
        reply = self._transport.request(
            self._url,
            headers={'Content-Type': 'application/json', 'Authorization': self._authorization},
            body=json_body(launch),
        )
        if reply.status >= 500:
            raise RuntimeError('routine acceptance unknown')
        if reply.status not in {200, 201}:
            raise SubmissionRejected('native routine rejected the trigger')

    def cancel(self, handle):
        return None
