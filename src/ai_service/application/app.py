"""Task service application: composes task, MCP, and worker capabilities.

The HTTP server calls into this object; it is not itself a transport.
"""
from __future__ import annotations

from ..notifications import NativeNotifications
from .access import ApplicationAccessMixin
from .events import EventAccessMixin
from .mcp import McpOperationsMixin
from .tasks import TaskOperationsMixin


class Application(TaskOperationsMixin, McpOperationsMixin, EventAccessMixin, ApplicationAccessMixin):
    def __init__(self, runtime, authorizer, events=None):
        self.runtime, self.auth, self.events = runtime, authorizer, events
        self.notifications = NativeNotifications(runtime)
