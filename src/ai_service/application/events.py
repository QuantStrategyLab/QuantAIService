"""Event subscription scope checks for MCP workers."""
from __future__ import annotations


class EventAccessMixin:
    def event_worker(self, principal):
        self.require(principal, 'events:subscribe')
        if not self.event_bus(principal.provider):
            raise PermissionError('events are unavailable for this worker')
