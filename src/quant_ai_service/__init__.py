"""Configured owner admission for the independent Quant task service.

Task routing uses shared ``task.routing.v1`` data under each
project's ``routing`` map; scenario ids are not hard-coded in Python.

``ai_service`` is the runtime implementation package (HTTP server, MCP, task
runtime). ``quant_ai_service`` remains the published compatibility entry: CLI
``quant-ai-service`` (``quant_ai_service.server:main``), owner routing validation,
and stable import paths for downstream repos. Prefer ``from ai_service...`` for
new in-repo code; keep ``quant_ai_service`` for setuptools entry points and
existing consumers.
"""
