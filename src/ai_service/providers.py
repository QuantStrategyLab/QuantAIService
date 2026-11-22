"""Native persistent-agent triggers. Results arrive through authenticated tools."""
from __future__ import annotations

from urllib.parse import urlsplit

from .runtime import SubmissionRejected
from .transport import PublicHTTPS, json_body


class WebhookTriggerProvider:
    """Generic asynchronous outbound webhook trigger for named agent platforms."""
    mode = 'agent'
    callback_driven = True
    acceptance = 'webhook_triggered'

    def __init__(self, webhook_url, key, task_resource, *, transport=None):
        parsed = urlsplit(webhook_url)
        resource = urlsplit(task_resource)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('agent webhook must be a configured HTTPS URL')
        if resource.scheme != 'https' or not resource.hostname or resource.username or resource.password:
            raise ValueError('task resource must be HTTPS')
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise ValueError('agent webhook key is required')
        self._url, self._key = webhook_url, key
        self._resource = task_resource
        self._transport = transport or PublicHTTPS()

    def submit(self, request, *, task_id, owner):
        # The fixed agent reads the task through project-scoped OAuth MCP tools.
        # No credentials, business materials or executable instructions in the trigger.
        self.notify(task_id=task_id, owner=owner, change='requested', revision=1)
        return {'task_id': task_id, 'acceptance': self.acceptance,
                'model_verification': 'unavailable'}

    def notify(self, *, task_id, owner, change, revision, handle=None):
        reply = self._transport.request(self._url,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._key},
            body=json_body({'type': 'task.requested', 'task_id': task_id,
                            'project': owner, 'resource': self._resource,
                            'change': change, 'revision': revision}))
        if reply.status >= 500:
            raise RuntimeError('agent trigger acceptance unknown')
        if not (200 <= reply.status < 300):
            raise SubmissionRejected('agent webhook rejected the trigger')

    def cancel(self, handle):
        # No documented native cancellation API. The task tool exposes cancel_requested;
        # only the authenticated execution attempt can confirm it stopped.
        return None


class MuseWebhookProvider(WebhookTriggerProvider):
    """Muse Wonder asynchronous webhook trigger adapter."""
    acceptance = 'webhook_triggered'


class GrokWebhookProvider(WebhookTriggerProvider):
    acceptance = 'routine_started'

    def notify(self, *, task_id, owner, change, revision, handle=None):
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

    def notify(self, *, task_id, owner, change, revision, handle=None):
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


class JulesAPIProvider(WebhookTriggerProvider):
    """Google Jules cloud coding agent session trigger."""
    acceptance = 'jules_started'

    def __init__(self, webhook_url, key, task_resource, *, transport=None):
        parsed = urlsplit(webhook_url)
        if parsed.scheme != 'https' or parsed.netloc != 'jules.googleapis.com' or parsed.path != '/v1alpha/sessions' or parsed.query or parsed.fragment:
            raise ValueError('Jules API must use the official sessions endpoint')
        super().__init__(webhook_url, key, task_resource, transport=transport)

    def notify(self, *, task_id, owner, change, revision, handle=None):
        event = {'type': 'task.requested', 'task_id': task_id, 'project': owner,
                 'resource': self._resource, 'change': change, 'revision': revision}
        reply = self._transport.request(self._url,
            headers={'Content-Type': 'application/json', 'X-Goog-Api-Key': self._key},
            body=json_body({'title': f'{owner}:{task_id}', 'prompt': json_body(event).decode('utf-8')}))
        if reply.status >= 500:
            raise RuntimeError('agent trigger acceptance unknown')
        if not (200 <= reply.status < 300):
            raise SubmissionRejected('Jules API rejected the trigger')
