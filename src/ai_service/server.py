"""HTTP/MCP transport for the authenticated v2 task API.

Bind behind an HTTPS proxy. This module owns sockets, request framing, status
codes and auth header extraction. Application operations live in
``ai_service.application``; composition lives in ``ai_service.wiring``.

``Application``, ``build_application`` and ``validate_configuration`` remain
re-exported here for compatibility with existing tests and callers. Prefer
importing ``Application`` from ``ai_service.application`` in new code.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version  # noqa: F401
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import ValidationError

from .application import Application
from .auth import AuthenticationError, ProjectAdmissionError
from .mcp_logging import AuthDiagnostics, MCPLog, METHODS
from .runtime import Conflict, NotFound, TaskRuntime  # noqa: F401
from .transport import json_body
from . import wiring


def validate_configuration(config):
    """Compatibility wrapper; implementation is ``wiring.validate_configuration``."""
    return wiring.validate_configuration(config)


def check_worker_tools(config, identity, expected_projects):
    """Compatibility wrapper; implementation is ``wiring.check_worker_tools``."""
    return wiring.check_worker_tools(config, identity, expected_projects)


def build_application(config, *, github_key_loader=None, github_transport=None):
    """Compatibility wrapper; implementation is ``wiring.build_application``."""
    return wiring.build_application(
        config, github_key_loader=github_key_loader, github_transport=github_transport
    )

__all__ = [
    'Application',
    'TaskRuntime',
    'build_application',
    'check_worker_tools',
    'main',
    'make_server',
    'validate_configuration',
    'version',
]


def make_server(application, port, *, max_request_bytes=2_000_000, mcp_log_level='INFO'):
    if type(max_request_bytes) is not int or not 1 <= max_request_bytes <= 16_000_000:
        raise ValueError('invalid request size limit')
    audit = MCPLog(mcp_log_level)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass  # No tokens, request bodies or private task identifiers in access logs.

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            self.connection.settimeout(15)
            extra = {}
            is_mcp = urlsplit(self.path).path == '/mcp'
            principal, diagnostics = None, AuthDiagnostics()
            decision, reason, method = 'not_attempted', 'invalid_request', 'auth'
            try:
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('chunked requests are not supported')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= max_request_bytes:
                    raise ValueError('request size exceeds service limit')
                headers = {name: self.headers.get(name) for name in ['Authorization', 'Idempotency-Key', 'X-Request-SHA256']}
                discovery = self.command == 'GET' and urlsplit(self.path).path == '/.well-known/oauth-protected-resource'
                if not discovery:
                    decision = 'denied'
                    principal = application.authenticate(headers)
                    diagnostics = principal.diagnostics or AuthDiagnostics(effective_scopes=tuple(principal.scopes))
                    decision, reason = 'allowed', 'granted'
                if (self.command == 'GET' and urlsplit(self.path).path == '/v1/tasks/lookup'
                        and (length or any(len(self.headers.get_all(name, [])) != 1
                                           for name in ['Idempotency-Key', 'X-Request-SHA256']))):
                    raise ValueError('lookup requires single binding headers and no body')
                if discovery and length:
                    raise ValueError('discovery takes no body')
                body = None
                if length:
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError('incomplete request')
                    body = json.loads(raw)
                if is_mcp:
                    requested = body.get('method') if isinstance(body, dict) else None
                    method = requested if isinstance(requested, str) and requested in METHODS else 'unknown'
                    if self.command == 'GET':
                        method = 'http/get'
                status, payload = application.dispatch(self.command, self.path, headers, body, principal=principal)
                if status == 405:
                    reason = 'method_not_allowed'
            except AuthenticationError as error:
                diagnostics, reason = error.diagnostics, error.reason_code
                status, payload = 401, {'error': 'authentication_required'}
                resource = urlsplit(application.auth.resource)
                metadata = resource.scheme + '://' + resource.netloc + '/.well-known/oauth-protected-resource'
                extra['WWW-Authenticate'] = 'Bearer resource_metadata="' + metadata + '"'
            except ProjectAdmissionError as error:
                principal, reason = error.principal, 'project_not_admitted'
                diagnostics = principal.diagnostics or AuthDiagnostics(effective_scopes=tuple(principal.scopes))
                status, payload = 403, {'error': 'access_denied'}
            except PermissionError:
                reason = 'access_denied'
                status, payload = 403, {'error': 'access_denied'}
            except NotFound:
                reason = 'not_found'
                status, payload = 404, {'error': 'not_found'}
            except Conflict:
                reason = 'task_conflict'
                status, payload = 409, {'error': 'task_conflict'}
            except (ValueError, TypeError, ValidationError):
                reason = 'invalid_request'
                status, payload = 400, {'error': 'invalid_request'}
            except Exception:
                reason = 'internal_error'
                status, payload = 500, {'error': 'internal_error'}
            provider_matched = None if principal is None else bool(application.event_bus(principal.provider))
            try:
                encoded = b'' if status == 202 and payload is None else json_body(payload)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(encoded)
                self.wfile.flush()
                self.close_connection = True
            except Exception:
                if is_mcp:
                    audit.emit(method=method, status=status, decision=decision, reason=reason,
                               diagnostics=diagnostics, provider_matched=provider_matched, payload=payload,
                               write_failed=True)
                raise
            if is_mcp:
                audit.emit(method=method, status=status, decision=decision, reason=reason,
                           diagnostics=diagnostics, provider_matched=provider_matched, payload=payload)

    class BoundedServer(ThreadingHTTPServer):
        def handle_error(self, *_):
            # The default traceback includes the peer address and exception data.
            pass

        def __init__(self, *args):
            self.slots = threading.BoundedSemaphore(16)
            super().__init__(*args)

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self.slots.release()

    server = BoundedServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main(configuration_guard=None):
    parser = argparse.ArgumentParser(description='Generic native-agent task service')
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=8090)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--check-config', action='store_true', help='validate configuration without starting the service')
    checks.add_argument('--check-worker-tools', action='store_true', help='check owner-worker schemas without starting the service or authenticating')
    parser.add_argument('--worker-identity', help='exact configured worker identity for --check-worker-tools')
    parser.add_argument('--expect-project', action='append', help='exact expected project; repeat for every admitted project')
    args = parser.parse_args()
    if args.check_worker_tools and (not args.worker_identity or not args.expect_project):
        parser.error('--check-worker-tools requires --worker-identity and --expect-project')
    if not args.check_worker_tools and (args.worker_identity is not None or args.expect_project is not None):
        parser.error('worker selectors require --check-worker-tools')
    config_path = Path(args.config)
    try:
        if config_path.stat().st_mode & 0o077:
            raise ValueError('config must be readable only by its owner')
        raw = config_path.read_bytes()
        config = json.loads(raw)
        if configuration_guard:
            configuration_guard(config)
        if args.check_worker_tools:
            summary = check_worker_tools(config, args.worker_identity, args.expect_project)
            summary['config_sha256'] = hashlib.sha256(raw).hexdigest()
            print(json.dumps(summary))
            return
        if args.check_config:
            _, policies = validate_configuration(config)
            print(json.dumps({'valid': True, 'projects': len(policies),
                              'providers': len(config['providers'])}))
            return
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        if args.check_config or args.check_worker_tools:
            print(json.dumps({'valid': False, 'error': 'configuration rejected'}))
            raise SystemExit(1) from None
        parser.error('configuration rejected; check owner-only permissions and configuration')
    application = build_application(config)
    lock_path = application.runtime.path.with_suffix('.service.lock')
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        parser.error('a dispatcher already owns this state database')
    stop = threading.Event()
    server = make_server(application, args.port, max_request_bytes=config.get("max_request_bytes", 2_000_000),
                         mcp_log_level=config.get('mcp_log_level', 'INFO'))
    def drive():
        while not stop.is_set():
            try:
                application.runtime.tick()
                application.notifications.flush()
                if application.runtime.github_comments:
                    application.runtime.github_comments.flush()
                for bus in application.event_buses.values():
                    bus.flush()
                if application.events and application.events not in application.event_buses.values():
                    application.events.flush()
            except Exception:
                # Stop admitting requests if the durable dispatcher cannot advance them.
                stop.set()
                server.shutdown()
                return
            stop.wait(1)
    dispatcher = threading.Thread(target=drive, daemon=True)
    dispatcher.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        dispatcher.join(timeout=30)
        os.close(lock_fd)


if __name__ == '__main__':
    main()
