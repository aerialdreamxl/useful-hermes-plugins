# -*- coding: utf-8 -*-
"""llm_request middleware — the payload surgery that makes the model see DSH.

Two payload jobs plus a per-request reconcile, all exception-safe and fail-open:

* **Default (no mode)** — strip this plugin's two tools (``bash`` /
  ``str_replace_editor``) from every outbound tool list, in whatever wire
  shape the active protocol uses (OpenAI ``function`` / Responses ``function``
  / Anthropic ``input_schema`` / Bedrock ``toolSpec``), so the plugin is
  *never* visible in normal sessions.
* **minimal mode (per session)** — replace the tool list with exactly the two
  DSH schemas and overwrite the system-prompt slot with DSH's fixed prompt, in
  the active protocol's shape:

  ===================  ===================  =================================
  api_mode             system slot          tools slot
  ===================  ===================  =================================
  chat_completions     messages[0]          tools[type=function, function]
  codex_responses      instructions         tools[type=function, name]
  anthropic_messages   system               tools[name, input_schema]
  bedrock_converse     system[{text}]       toolConfig.tools[toolSpec]
  ===================  ===================  =================================

  Every wire shape mirrors the host's own serializer 1:1 — see ``wire.py``;
  the test suite proves equality against the live converter functions.
  OAuth ("Claude Code") Anthropic sessions keep Hermes' identity block, and
  our tool names ride the same ``mcp__`` wire prefix as every other tool there
  (the host's response intake strips it back for dispatch).

On top of the payload transform, every request reconciles the session's persistent
state (``_reconcile``): the take-over store re-arms the mode after a backend
restart, and minimal-mode titles carry the ``[DSHSIM]`` label. The payload
transform is stateless (idempotent, safe to run on every API attempt of a turn);
the reconcile is equally idempotent and fail-open.
"""

from __future__ import annotations

import logging

from . import constants as C
from . import inject
from . import registry
from . import state
from . import titles
from . import wire
from .registry import MODE_MINIMAL

logger = logging.getLogger("plugin.hermes-dsh-simulator")

OUR_TOOL_NAMES = ("bash", "str_replace_editor")
_MCP_PREFIX = "mcp__"
_CHAT_MODES = frozenset({"", "chat_completions"})
_warned = set()


def _warn_once(key, message, *args):
    if key not in _warned:
        _warned.add(key)
        logger.warning("hermes-dsh-simulator: " + message, *args)


def on_llm_request(request=None, session_id="", api_mode="", **context):
    """Registered as ``ctx.register_middleware("llm_request", ...)``.

    Contract: return ``{"request": {...}}`` to replace the outbound payload, or
    ``None`` to pass through.
    """
    try:
        if not isinstance(request, dict):
            return None
        if session_id:
            # Any request == the session is no longer ''message-less''.
            registry.mark_turn_seen(session_id)
            _reconcile(session_id)

        mode = registry.get_mode(session_id)

        if mode == MODE_MINIMAL:
            normalized = api_mode if isinstance(api_mode, str) else ""
            if normalized in _CHAT_MODES:
                new_request = _simple_chat(request)
            elif normalized == "codex_responses":
                new_request = _simple_codex(request)
            elif normalized == "anthropic_messages":
                new_request = _simple_anthropic(request)
            elif normalized == "bedrock_converse":
                new_request = _simple_bedrock(request)
            else:
                _warn_once("unknown:%s" % normalized,
                           "minimal mode does not know api_mode=%r; passing through untouched", normalized)
                return None
            if new_request is None:
                return None
            # The host validates incoming call names against the live agent's
            # valid_tool_names — the payload alone cannot win that gate. Make
            # the pair really callable (idempotent; see inject.py).
            inject.ensure_session_tools(session_id)
            return {"request": new_request}

        # Default: invisible plugin — strip our tools when the host lazily added them.
        new_request = _strip_ours(request)
        return {"request": new_request} if new_request is not None else None
    except Exception as exc:  # never break a turn over the mode transform
        try:
            logger.warning("hermes-dsh-simulator llm_request middleware failed open: %s", exc)
        except Exception:
            pass
        return None


def _reconcile(session_id: str) -> None:
    """Mode / persistent store / [DSHSIM] label convergence for one session.

    Runs on every outbound request, idempotent and fail-open: a session in the
    persistent take-over store re-arms its mode even after a backend restart
    (self-heal), and every minimal-mode session's title carries the label.
    """
    try:
        mode = registry.get_mode(session_id)
        taken = state.is_taken(session_id)
        if mode == MODE_MINIMAL:
            if not taken:
                state.add(session_id, surface="auto")
            titles.ensure_label(session_id)
        elif taken:
            registry.set_mode(session_id, MODE_MINIMAL)
            titles.ensure_label(session_id)
    except Exception:
        pass


def _strip_ours(request: dict):
    """Remove our two tools from the payload's tool list(s), any wire shape.

    Covers the top-level ``tools`` list (chat / codex / anthropic) and Bedrock's
    nested ``toolConfig.tools``. Returns a new request or ``None`` when nothing
    was stripped."""
    new_request = None

    tools = request.get("tools")
    if isinstance(tools, list):
        filtered = [entry for entry in tools if wire.tool_wire_name(entry) not in OUR_TOOL_NAMES]
        if len(filtered) != len(tools):
            new_request = dict(request)
            new_request["tools"] = filtered

    tool_config = request.get("toolConfig")
    if isinstance(tool_config, dict) and isinstance(tool_config.get("tools"), list):
        filtered = [entry for entry in tool_config["tools"] if wire.tool_wire_name(entry) not in OUR_TOOL_NAMES]
        if len(filtered) != len(tool_config["tools"]):
            if new_request is None:
                new_request = dict(request)
            new_request["toolConfig"] = dict(tool_config)
            new_request["toolConfig"]["tools"] = filtered

    return new_request


# --------------------------------------------------------------------------
# minimal mode, one handler per protocol
# --------------------------------------------------------------------------

def _simple_chat(request: dict):
    """chat_completions: system prompt is messages[0]; tools are OpenAI defs."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    new_request = dict(request)
    new_request["tools"] = _host_sanitize_tools(wire.chat_tools(), str(request.get("model") or ""))
    new_messages = list(messages)
    for index, message in enumerate(new_messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").lower() not in ("system", "developer"):
            continue
        replaced = dict(message)
        content = message.get("content")
        if isinstance(content, list):
            replaced["content"] = [{"type": "text", "text": C.active_prompt()}]
        else:
            replaced["content"] = C.active_prompt()
        new_messages[index] = replaced
        break
    new_request["messages"] = new_messages
    return new_request


def _host_sanitize_tools(tools, model):
    """Re-apply the host chat transport's own tool sanitization to the DSH tool
    defs — currently the Moonshot/Kimi strict-JSON-Schema flavor, which the host
    applies when ``is_moonshot_model(model)`` (see chat_completions._base_kwargs)."""
    try:
        from agent.moonshot_schema import is_moonshot_model, sanitize_moonshot_tools

        if is_moonshot_model(model):
            return sanitize_moonshot_tools(tools)
    except Exception:
        pass
    return tools


def _simple_codex(request: dict):
    """codex_responses: system prompt is the ``instructions`` string; the
    conversation rides as Responses ``input`` items."""
    if not isinstance(request.get("input"), list):
        return None
    new_request = dict(request)
    new_request["instructions"] = C.active_prompt()
    new_request["tools"] = wire.responses_tools()
    # Mirrors what the host's builder emits whenever a tool set is present.
    new_request.setdefault("tool_choice", "auto")
    new_request.setdefault("parallel_tool_calls", True)
    return new_request


def _simple_anthropic(request: dict):
    """anthropic_messages: system prompt is the top-level ``system`` slot."""
    if not isinstance(request.get("messages"), list):
        return None
    new_request = dict(request)
    oauth = _oauth_style_anthropic(request)
    system = request.get("system")
    if oauth and isinstance(system, list) and system and isinstance(system[0], dict):
        # OAuth ("Claude Code") sessions: Hermes prepends its identity block and
        # expects it kept; replace everything AFTER it with the DSH prompt.
        new_request["system"] = [system[0], {"type": "text", "text": C.active_prompt()}]
    else:
        new_request["system"] = C.active_prompt()
    tools = wire.anthropic_tools()
    if oauth:
        tools = [_mcp_prefixed(tool) for tool in tools]
    new_request["tools"] = tools
    new_request.setdefault("tool_choice", {"type": "auto"})
    return new_request


def _simple_bedrock(request: dict):
    """bedrock_converse: system prompt is ``system[{text}]``; tools live under
    ``toolConfig``. Both may carry trailing ``cachePoint`` markers — preserved."""
    if not isinstance(request.get("messages"), list):
        return None
    new_request = dict(request)

    cache_points = []
    system = request.get("system")
    if isinstance(system, list):
        cache_points = [el for el in system if isinstance(el, dict) and "cachePoint" in el]
    new_request["system"] = [{"text": C.active_prompt()}] + cache_points

    tool_config = request.get("toolConfig")
    if isinstance(tool_config, dict):
        tool_cache_points = [el for el in (tool_config.get("tools") or [])
                             if isinstance(el, dict) and "cachePoint" in el]
        new_request["toolConfig"] = {"tools": wire.bedrock_tools() + tool_cache_points}
    elif _bedrock_supports_tools(str(request.get("modelId") or "")):
        new_request["toolConfig"] = {"tools": wire.bedrock_tools()}
    else:
        # Bedrock hard-rejects toolConfig on non-tool models (ValidationException);
        # when the host stripped tools for that reason, do not add them back.
        _warn_once("bedrock:no-tool-model",
                   "minimal mode: this Bedrock model appears not to support tool use; "
                   "leaving toolConfig absent (the model will see the DSH prompt only)")
    return new_request


def _oauth_style_anthropic(request: dict) -> bool:
    """Detect Hermes' OAuth ("Claude Code") Anthropic wire style: a prepended
    identity block and/or ``mcp__``-prefixed tool names."""
    system = request.get("system")
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and str(block.get("text") or "").startswith("You are Claude Code"):
                return True
    tools = request.get("tools")
    if isinstance(tools, list):
        for entry in tools:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str) \
                    and entry["name"].startswith(_MCP_PREFIX):
                return True
    return False


def _mcp_prefixed(tool: dict) -> dict:
    prefixed = dict(tool)
    name = prefixed.get("name")
    if isinstance(name, str) and not name.startswith(_MCP_PREFIX):
        prefixed["name"] = _MCP_PREFIX + name
    return prefixed


def _bedrock_supports_tools(model_id: str) -> bool:
    """Whether Converse accepts toolConfig for this model — follows the host's
    own gate when importable; conservative (False) when it is not."""
    try:
        from agent.bedrock_adapter import _model_supports_tool_use

        return bool(_model_supports_tool_use(model_id))
    except Exception:
        return False