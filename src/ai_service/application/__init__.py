"""Application layer: task API, MCP, and worker tool dispatch.

Invoked by ``ai_service.server`` transport. Does not bind sockets or parse raw
HTTP framing; keep transport concerns in ``server.py``.
"""
from .app import Application

__all__ = ['Application']
