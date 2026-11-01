"""Named agent routes preserve optional enablement and worker isolation offline."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_service.auth import Principal
from ai_service.providers import GrokWebhookProvider
from ai_service.runtime import Policy
from ai_service.server import build_application, validate_configuration
from ai_service.transport import HttpReply

ROOT = Path(__file__).resolve().parents[2]


class NamedAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = json.loads((ROOT / 'config/quant.example.json').read_text())
        self.assertFalse(self.config['enabled'])
        self.config.update(enabled=True, state_path=str(Path(temporary.name) / 'tasks.sqlite'))
        self.project = next(iter(self.config['projects']))
        self.request = {'mode': 'agent', 'profile': 'grokbot', 'model': 'configured-grokbot',
                        'objective': 'Synthetic advisory only', 'materials': ['synthetic'],
                        'output_schema': {'type': 'object'}}

    def build(self):
        # Never consume the real environment, even if the binding happens to exist.
        with patch('ai_service.server.os.environ.get', return_value='synthetic-test-only') as env:
            app = build_application(self.config)
        env.assert_called_once_with('AI_SERVICE_GROKBOT_WEBHOOK_KEY')
        return app

    def test_examples_load_enabled_grokbot_and_leave_duet_unavailable(self):
        app = self.build()
        self.assertIsInstance(app.runtime.providers['grokbot'], GrokWebhookProvider)
        self.assertNotIn('claudeduet', app.runtime.providers)
        with self.assertRaises(PermissionError):
            app.runtime.submit(self.project, 'duet-disabled',
                {**self.request, 'profile': 'claudeduet', 'model': 'configured-claudeduet'})
        self.assertEqual(app.runtime.policies[self.project].providers['agent'], 'dot')
        self.assertEqual(app.runtime.policies[self.project].providers['agent:second-opinion'], 'grok')
        for name in ('grokbot', 'claudeduet'):
            grant = next(g for g in self.config['grants'] if g.get('provider') == name)
            self.assertEqual(set(grant['scopes']), {'tasks:read', 'tasks:work'})
            self.assertIn(name.upper(), grant['claims']['azp'])

    def test_duet_cannot_be_enabled_as_a_claude_code_routine(self):
        self.config['providers']['claudeduet']['enabled'] = True
        with patch('ai_service.server.os.environ.get', side_effect=AssertionError('credential read')):
            with self.assertRaisesRegex(ValueError, 'Duet invocation is not configured'):
                validate_configuration(self.config)
            with self.assertRaises(ValueError):
                build_application(self.config)

    def test_grokbot_requires_explicit_enablement_and_credential(self):
        for enabled in (None, False):
            c = copy.deepcopy(self.config)
            c['providers']['grokbot']['enabled'] = enabled
            with patch('ai_service.server.os.environ.get', side_effect=AssertionError('credential read')):
                app = build_application(c)
            self.assertNotIn('grokbot', app.runtime.providers)
        with patch('ai_service.server.os.environ.get', return_value=None):
            with self.assertRaises(ValueError):
                build_application(self.config)

    def test_routed_grokbot_claim_report_and_provider_project_model_boundaries(self):
        app = self.build()
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append(json.loads(kwargs['body']))
                return HttpReply(200, b'')
        app.runtime.providers['grokbot']._transport = Transport()
        task = app.runtime.submit(self.project, 'grokbot-task', self.request)
        app.runtime.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {'type', 'task_id', 'project', 'resource', 'change', 'revision'})
        self.assertEqual(app.runtime.get(self.project, task['id'])['status'], 'running')
        scopes = frozenset({'tasks:read', 'tasks:work'})
        worker = Principal('grokbot-worker', self.project, scopes, 'grokbot')
        for wrong in ('dot', 'grok', 'claude', 'claudeduet'):
            with self.subTest(provider=wrong), self.assertRaises(PermissionError):
                app.read_task(Principal('other-worker', self.project, scopes, wrong), task['id'])
        with self.assertRaises(PermissionError):
            app.project_context(worker, 'other-org/OtherProject')
        with self.assertRaises(PermissionError):
            app.require(worker, 'tasks:submit')
        with self.assertRaises(PermissionError):
            app.runtime.submit(self.project, 'wrong-model', {**self.request, 'model': 'unapproved'})
        app.runtime.policies[self.project + 'Unadmitted'] = Policy({'agent': 'dot'}, {'dot': {'configured-dot'}})
        with self.assertRaises(PermissionError):
            app.runtime.submit(self.project + 'Unadmitted', 'wrong-profile', self.request)
        claimed = app.runtime.claim(self.project, task['id'], worker.provider, worker.identity, 'attempt')
        app.runtime.report(self.project, task['id'], worker.provider, worker.identity, 'attempt',
                           claimed['revision'], 'completed', {})
        self.assertEqual(app.read_task(worker, task['id'])['status'], 'completed')
