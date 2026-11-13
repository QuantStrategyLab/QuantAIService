"""MCP JSON-RPC operations: tools, events, and worker methods.

Invoked by the HTTP ``/mcp`` transport path; framing stays in ``server.py``.
"""
from __future__ import annotations

import json

from importlib.metadata import PackageNotFoundError
from jsonschema import Draft202012Validator, ValidationError

from ..events import CallbackEndpointError
from ..runtime import Conflict, NotFound
from ..worker_tools import worker_tools


class McpOperationsMixin:
    def mcp(self, principal, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ValueError('a JSON-RPC request is required')
        if 'id' not in message:
            params = message.get('params', {})
            if (message.get('method') != 'notifications/initialized'
                    or not isinstance(params, dict)
                    or ('_meta' in params and not isinstance(params['_meta'], dict))):
                raise ValueError('invalid MCP notification')
            # Authentication and project admission already ran in dispatch.
            # Client initialization does not change task state or permissions.
            return None
        identifier = message['id']
        try:
            method, params = message.get('method'), message.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('params must be an object')
            if '_meta' in params:
                if not isinstance(params['_meta'], dict):
                    raise ValueError('protocol metadata must be an object')
                # MCP envelope metadata is untrusted; it never supplies task
                # arguments, identity, scopes or project admission.
                params = {key: value for key, value in params.items() if key != '_meta'}
            if method == 'initialize':
                self.require(principal, 'tasks:work')
                requested = params.get('protocolVersion')
                if (not isinstance(requested, str)
                        or not isinstance(params.get('capabilities'), dict)
                        or not isinstance(params.get('clientInfo'), dict)):
                    raise ValueError('invalid initialization')
                supported = {'2025-03-26', '2025-06-18', '2025-11-25'}
                from .. import server as srv
                try:
                    server_version = srv.version('quant-ai-service')
                except PackageNotFoundError:
                    server_version = 'development'
                result = {'protocolVersion': requested if requested in supported else '2025-11-25',
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'quant-ai-service', 'version': server_version}}
            elif method == 'ping':
                result = {}
            elif method == 'server/discover':
                caps = {'tools': {}}
                if self.events and 'events:subscribe' in principal.scopes and principal.provider == self.events.provider_name:
                    caps['events'] = {}
                result = {'resultType': 'complete', 'supportedVersions': ['2026-07-28'], 'capabilities': caps}
            elif method == 'tools/list':
                if 'tasks:work' not in principal.scopes:
                    self.event_worker(principal)
                result = {'tools': self.tools(principal)}
            elif method == 'tools/call':
                result = self.call_tool(principal, params)
            elif method in {'events/list', 'events/subscribe', 'events/unsubscribe'}:
                self.event_worker(principal)
                if method == 'events/list':
                    result = self.events.catalog()
                elif method == 'events/subscribe':
                    if not isinstance(params.get('arguments'), dict):
                        raise ValueError('project filter is required')
                    self.project_context(principal, params['arguments'].get('project'))
                    result = self.events.subscribe(principal.identity, params)
                else:
                    if not isinstance(params.get('arguments'), dict):
                        raise ValueError('project filter is required')
                    self.project_context(principal, params['arguments'].get('project'))
                    result = self.events.unsubscribe_request(principal.identity, params)
            else:
                return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32601, 'message': 'method not found'}}
            return {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        except CallbackEndpointError as error:
            return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32015,
                    'message': 'CallbackEndpointError', 'data': {'reason': error.reason}}}
        except (ValueError, PermissionError, NotFound, ValidationError) as error:
            code = -32009 if isinstance(error, Conflict) else -32003 if isinstance(error, PermissionError) else -32602
            # Schema errors contain supplied values; keep private materials out of errors.
            description = 'task conflict' if isinstance(error, Conflict) else 'access denied' if isinstance(error, PermissionError) else 'invalid arguments'
            return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': description}}

    def tools(self, principal):
        return worker_tools(principal, self.runtime.policies,
                            self.events.provider_name if self.events else None)

    def call_tool(self, principal, params):
        if set(params) != {'name', 'arguments'} or not isinstance(params['arguments'], dict):
            raise ValueError('tool name and arguments are required')
        name, args = params['name'], params['arguments']
        if name in {'get_event_subscription', 'delete_event_subscription'}:
            self.event_worker(principal)
        elif name in {'get_task', 'claim_task', 'report_task_result'}:
            self.require(principal, 'tasks:work')
        definitions = {tool['name']: tool for tool in self.tools(principal)}
        if name not in definitions:
            raise ValueError('unknown tool')
        Draft202012Validator(definitions[name]['inputSchema']).validate(args)
        principal = self.project_context(principal, args.get('project'))
        if name in {'get_event_subscription', 'delete_event_subscription'}:
            operation = self.events.status if name == 'get_event_subscription' else self.events.unsubscribe
            record = operation(principal.identity, args['subscription_id'], owner=principal.project)
        elif name == 'get_task':
            record = self.worker(principal, args['task_id'])
            record = {**record, 'messages': self.runtime.messages(principal.project, args['task_id'])}
        elif name == 'claim_task':
            self.worker(principal, args['task_id'])
            record = self.runtime.claim(principal.project, args['task_id'], principal.provider,
                                        principal.identity, args['attempt_id'])
        else:
            self.worker(principal, args['task_id'])
            record = self.runtime.report(principal.project, args['task_id'], principal.provider,
                                          principal.identity, args['attempt_id'], args['revision'],
                                          args['status'], args.get('output'), args.get('artifacts'))
        record = self.public_record(record)
        return {'content': [{'type': 'text', 'text': json.dumps(record, ensure_ascii=False)}],
                'structuredContent': record, 'isError': False}
