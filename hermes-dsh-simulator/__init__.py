# -*- coding: utf-8 -*-
"""hermes-dsh-simulator — DSH four-mode replica for Hermes Agent (minimal first).

``/dshsim minimal`` mirrors DeepSeek Harness's minimal mode for one
message-less session: the model sees exactly DSH's fixed system prompt plus the
``bash`` / ``str_replace_editor`` schemas (copied 1:1 from the DSH reference
source), silently forwarded onto Hermes' ``terminal`` and file tools.
``/dshsim optout`` restores native behavior at any time.

Take-overs are **persistent**: the session id is recorded in
``$HERMES_HOME/dshsim-sessions.json`` (see ``dsh_modes/state.py``), the plugin
re-arms every stored session at registration (so a backend restart no longer
drops the mode), taken-over session titles carry a ``[DSHSIM] `` label
(``dsh_modes/titles.py``, re-asserted per request), and optout removes both.
With no mode set the plugin never interferes: the pair is registered invisibly
(private toolset + exposure-gating check_fn) and injected per-session only
into taken-over sessions, where direct calls must pass the host's tool-call
intake validation.

All registration flows through ``register(ctx)``. Heavy imports stay inside
call time; the package root only wires modules together.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("plugin.hermes-dsh-simulator")


def register(ctx) -> None:
    from .dsh_modes import commands as commands_mod
    from .dsh_modes import constants as C
    from .dsh_modes import forward
    from .dsh_modes import guard
    from .dsh_modes import inject
    from .dsh_modes import middleware
    from .dsh_modes import state as state_mod
    from .dsh_modes import translate

    forwarders = forward.make_forwarders()
    inject.bind_ctx(ctx)

    def _bash_tool(args, **kw):
        return translate.handle_bash(args if isinstance(args, dict) else {}, kw, forwarders)

    def _editor_tool(args, **kw):
        return translate.handle_editor(args if isinstance(args, dict) else {}, kw, forwarders)

    # The pair is registered under a toolset no session grants (``dshsim``)
    # with a check_fn that always returns False: that combination removes them
    # from every session's tool defs, deferred catalog, tool_call bridge,
    # payloads and validation lists (check_fn gates EXPOSURE, never
    # invocation). They become really-callable only via the per-session
    # injection in dsh_modes/inject.py — never via visible registration.
    ctx.register_tool(
        name="bash",
        toolset="dshsim",
        schema=C.active_bash_schema(),
        handler=_bash_tool,
        check_fn=inject.never_exposed,
        description=(
            "DSH minimal-mode replica of DSH's bash tool (managed by /dshsim; "
            "hidden outside minimal mode)"
        ),
        emoji="🐚",
    )
    ctx.register_tool(
        name="str_replace_editor",
        toolset="dshsim",
        schema=C.editor_schema(),
        handler=_editor_tool,
        check_fn=inject.never_exposed,
        description=(
            "DSH minimal-mode replica of DSH's str_replace_editor tool (managed by "
            "/dshsim; hidden outside minimal mode)"
        ),
        emoji="✏️",
    )
    ctx.register_command(
        "dshsim",
        commands_mod.handle,
        description="Enter/leave the DSH simulator's minimal mode for this session (message-less sessions only; optout any time)",
        args_hint="[minimal|optout]",
    )
    ctx.register_middleware("llm_request", middleware.on_llm_request)
    ctx.register_hook("pre_tool_call", guard.on_pre_tool_call)

    # Re-arm persisted take-overs: the store is the source of truth for
    # sessions under the simulator, so a backend restart keeps them taken
    # over — the middleware re-asserts mode + [DSHSIM] label per request.
    try:
        armed = state_mod.hydrate()
        if armed:
            logger.info("hermes-dsh-simulator: re-armed %d persisted take-over(s)", armed)
    except Exception:
        pass

    logger.info("hermes-dsh-simulator registered (flavor=%s)", C.active_flavor())