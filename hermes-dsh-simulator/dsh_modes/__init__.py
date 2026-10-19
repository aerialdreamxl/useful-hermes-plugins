# -*- coding: utf-8 -*-
"""hermes-dsh-simulator — internal package.

Modules:
* constants  — the DSH minimal-mode model-facing surface (prompt + tool schemas +
  result/error texts), copied 1:1 from the DSH reference source.
* registry   — per-session mode state (in-memory) + turn tracker.
* sessions   — surface-aware session resolution + the new-session gate.
* translate  — the translation layer: DSH-shaped tool calls -> Hermes tools,
  plus DSH-shaped rendering of the results.
* forward    — binds the translation layer to the real Hermes tool handlers.
* middleware — llm_request payload surgery (system prompt + tool list).
* guard      — pre_tool_call veto for non-surface tools while a mode is active.
* commands   — the /dshsim slash command.
* state      — persistent take-over store ($HERMES_HOME/dshsim-sessions.json).
* titles     — [DSHSIM] title-label injection on taken-over sessions.
"""

__all__ = [
    "constants",
    "registry",
    "sessions",
    "translate",
    "forward",
    "middleware",
    "guard",
    "commands",
    "state",
    "titles",
]