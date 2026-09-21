"""Agent Trace Viewer — agent-side stub.

This plugin's functionality lives entirely in two sibling surfaces:

- ``dashboard/plugin_api.py`` — a read-only FastAPI router over ``state.db``,
  mounted by the web server at ``/api/plugins/agent-trace-viewer/``.
- ``desktop/plugin.js`` — the desktop app page that renders traces and
  exports OpenAI ``messages[]`` JSON.

Nothing is registered on the agent side, so ``register`` is intentionally a
no-op. The module still must exist: every directory plugin needs a
``plugin.yaml`` plus an ``__init__.py`` exposing ``register(ctx)``.
"""

from __future__ import annotations


def register(ctx) -> None:  # noqa: ARG001 — signature required by the loader
    """No agent-side tools or hooks; all features are UI/backend only."""
    return None
