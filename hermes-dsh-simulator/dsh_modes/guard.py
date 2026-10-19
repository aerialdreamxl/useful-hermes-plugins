# -*- coding: utf-8 -*-
"""pre_tool_call veto — while minimal mode is active, only the two DSH-surface
tools may run.

Scoped strictly to sessions with a registered mode: every other session, and
every non-mode tool call, returns ``None`` (never interferes). The block
message becomes the tool result the model sees.
"""

from __future__ import annotations

from . import registry
from .registry import MODE_MINIMAL

_ALLOWED = frozenset({"bash", "str_replace_editor"})

_BLOCK_MESSAGE = (
    "DSH minimal mode ([DSHSIM]) is active for this session and exposes only the "
    "`bash` and `str_replace_editor` tools. Retry with one of those: shell "
    "commands go through `bash`, file edits through `str_replace_editor`."
)


def on_pre_tool_call(tool_name="", session_id="", **context):
    """Registered as ``ctx.register_hook("pre_tool_call", ...)``.

    Contract: return ``{"action": "block", "message": ...}`` to veto, or
    ``None`` to let the call proceed.
    """
    try:
        if registry.get_mode(session_id) != MODE_MINIMAL:
            return None
        if tool_name in _ALLOWED:
            return None
        return {"action": "block", "message": _BLOCK_MESSAGE}
    except Exception:
        return None