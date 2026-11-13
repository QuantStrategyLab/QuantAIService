"""Task service application: composes HTTP, MCP, and worker capabilities."""
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
