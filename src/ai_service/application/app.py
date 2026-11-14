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
    def __init__(self, runtime, authorizer, events=None, event_buses=None):
        self.runtime, self.auth, self.events = runtime, authorizer, events
        # Dot and Muse each own an EventSubscriptions bus keyed by provider name.
        self.event_buses = dict(event_buses or {})
        if events is not None and getattr(events, 'provider_name', None):
            self.event_buses.setdefault(events.provider_name, events)
        self.notifications = NativeNotifications(runtime)

    def event_bus(self, provider):
        buses = getattr(self, 'event_buses', None) or {}
        return buses.get(provider) or (
            self.events if self.events and getattr(self.events, 'provider_name', None) == provider else None)
