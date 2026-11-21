# -*- coding: utf-8 -*-
"""Per-session injection that makes the DSH pair *really* callable in a taken-over session.

Two host-side gates sit OUTSIDE the llm_request payload, so a payload-only
rewrite is not enough (observed live 2026-09-29: direct ``bash`` calls were
rejected with "Tool 'bash' does not exist" while the payload showed the pair):

* **Tool-call intake validation** judges every incoming call name against
  ``agent.valid_tool_names`` (``agent/turn_tool_round.py``), a set derived from
  the agent's OWN snapshot (``agent.tools``) — not from the outbound payload
  the middleware rewrote.
* **tool_search** classifies plugin tools as deferrable, so registering the
  pair under a granted toolset (e.g. ``terminal``) also leaks them into every
  session's deferred catalog + ``tool_call`` bridge.

The fix mirrors the host's own post-build injection (memory / context-engine
tools, ``agent/agent_init.py::_inject_context_engine_tools``): append the two
wire defs to the LIVE agent and add both names to ``agent.valid_tool_names``.
On the registration side the pair lives under a toolset no session grants
(``dshsim``) with ``check_fn`` returning False — the host contract is
explicit that check_fn "gates exposure, never invocation"
(``tools/mcp_tool_agent.py``) — so in every non-minimal session the pair is
absent from defs, catalogs, bridges, payloads and validation lists, while
dispatch by name still works once injected.

Injection is idempotent and re-asserted on every llm_request build of a
minimal-mode session (agent rebuilds / pin restores therefore self-heal).
The desktop agent is reached the same way session-hold does it: the live
``tui_gateway.server._sessions`` record's ``agent``; CLI via
``get_plugin_manager()._cli_ref.agent``. Everything fails open.
"""

from __future__ import annotations

import logging
import sys

from . import constants as C

logger = logging.getLogger("plugin.hermes-dsh-simulator")

OUR_TOOL_NAMES = ("bash", "str_replace_editor")

# The pair silently forwards onto these host tools (see forward.py). Injecting
# it into an agent whose whole surface lacks them would expand a session the
# user disabled the terminal/file toolsets for, so the pair stays out there.
_FORWARD_HOST_TOOLS = ("terminal", "read_file", "write_file", "patch")

_ctx_ref = [None]
_warned: set = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning("hermes-dsh-simulator: " + message)


def bind_ctx(ctx) -> None:
    """Remember the plugin ctx so the CLI path can reach ``_cli_ref.agent``."""
    _ctx_ref[0] = ctx


def never_exposed() -> bool:
    """check_fn for the pair: gates *exposure* only (defs/catalogs).

    The host never consults check_fn at invocation time, so a False here keeps
    the pair invisible everywhere while leaving dispatch by name intact."""
    return False


# ---------------------------------------------------------------------------
# matching: find the live agent(s) serving a session key
# ---------------------------------------------------------------------------

def _norm_key(value) -> str:
    text = str(value or "").strip()
    prefix = "session:"
    return text[len(prefix):] if text.startswith(prefix) else text


def _tui_sessions():
    """Live desktop session table — uses the module ONLY when it is already
    loaded. The process that owns the records has imported it by definition;
    importing it here would drag the whole server into innocent processes."""
    for key in ("tui_gateway.server", "tui_gateway"):
        module = sys.modules.get(key)
        sessions = getattr(module, "_sessions", None) if module is not None else None
        if isinstance(sessions, dict):
            return sessions
    return None


def _find_agents(session_id: str):
    """Live agent objects serving *session_id* (desktop record / CLI ref)."""
    target = _norm_key(session_id)
    if not target:
        return []
    agents = []
    sessions = _tui_sessions()
    if isinstance(sessions, dict):
        for record in list(sessions.values()):
            if not isinstance(record, dict):
                continue
            agent = record.get("agent")
            if agent is None:
                continue
            keys = {_norm_key(record.get("session_key")), _norm_key(getattr(agent, "session_id", ""))}
            if target in keys:
                agents.append(agent)
    try:
        ctx = _ctx_ref[0]
        cli = getattr(getattr(ctx, "_manager", None), "_cli_ref", None) if ctx is not None else None
        agent = getattr(cli, "agent", None) if cli is not None else None
        if agent is not None:
            keys = {_norm_key(getattr(cli, "session_id", "")), _norm_key(getattr(agent, "session_id", ""))}
            if target in keys:
                agents.append(agent)
    except Exception:
        pass
    return agents


def _tools_lock():
    """The host's own serialization lock for in-place tools/valid-names swaps."""
    try:
        from tools.mcp_tool_agent import _agent_tools_lock

        return _agent_tools_lock
    except Exception:
        return None


def _def_name(entry) -> str:
    if isinstance(entry, dict):
        return str((entry.get("function") or {}).get("name") or "")
    return ""


# ---------------------------------------------------------------------------
# the mutations
# ---------------------------------------------------------------------------

def _surface_allows_injection(agent, have: set) -> bool:
    """Whether this agent's active surface may host the forwarded pair.

    ``have`` is the live tool-def name set. When the surface is known (a
    non-empty list), at least one host tool the pair forwards onto must be
    present: a session whose terminal/file toolsets are disabled must not
    gain a ``bash`` that resurrects them behind the user's back. An empty or
    unreadable list is not trusted as "all disabled" (it can be a half-built
    agent), so those callers keep the previous behaviour and only warn.
    """
    if not have:
        return True
    if any(name in have for name in _FORWARD_HOST_TOOLS):
        return True
    _warn_once(
        "surface:%s" % (getattr(agent, "session_id", "") or "unknown"),
        "minimal mode: live agent has none of the host tools the DSH pair "
        "forwards onto (%s) — the terminal/file surface looks disabled, "
        "not injecting" % ", ".join(_FORWARD_HOST_TOOLS),
    )
    return False


def _inject(agent) -> bool:
    """Append missing wire defs + names to one live agent (idempotent)."""
    tools = getattr(agent, "tools", None)
    if not isinstance(tools, list):
        _warn_once("notools:%s" % (getattr(agent, "session_id", "") or "unknown"),
                   "minimal mode: live agent tools list is unreadable — "
                   "active tool surface unknown; injecting nothing (fail open)")
        return False
    schemas = (C.active_bash_schema(), C.editor_schema())
    lock = _tools_lock()
    locked = False
    if lock is not None:
        try:
            lock.acquire()
            locked = True
        except Exception:
            locked = False
    try:
        have = {_def_name(t) for t in tools if isinstance(t, dict)}
        if not have:
            _warn_once("emptysurface:%s" % (getattr(agent, "session_id", "") or "unknown"),
                       "minimal mode: live agent tool list is empty — active tool "
                       "surface unknown; keeping previous inject-anyway behaviour")
        if not _surface_allows_injection(agent, have):
            return False
        changed = False
        for schema in schemas:
            name = str(schema.get("name") or "")
            if name and name not in have:
                tools.append({"type": "function", "function": schema})
                have.add(name)
                changed = True
        names = getattr(agent, "valid_tool_names", None)
        if not isinstance(names, set):
            names = set()
            try:
                agent.valid_tool_names = names
            except Exception:
                return changed
        for schema in schemas:
            name = str(schema.get("name") or "")
            if name and name not in names:
                names.add(name)
                changed = True
        return changed
    finally:
        if locked:
            try:
                lock.release()
            except Exception:
                pass


def _strip(agent) -> bool:
    """Remove the pair from one live agent (optout path)."""
    lock = _tools_lock()
    locked = False
    if lock is not None:
        try:
            lock.acquire()
            locked = True
        except Exception:
            locked = False
    try:
        changed = False
        tools = getattr(agent, "tools", None)
        if isinstance(tools, list):
            kept = [t for t in tools if _def_name(t) not in OUR_TOOL_NAMES]
            if len(kept) != len(tools):
                agent.tools = kept
                changed = True
        names = getattr(agent, "valid_tool_names", None)
        if isinstance(names, set):
            for name in OUR_TOOL_NAMES:
                if name in names:
                    names.discard(name)
                    changed = True
        return changed
    finally:
        if locked:
            try:
                lock.release()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# public entry points (fail-open)
# ---------------------------------------------------------------------------

def ensure_session_tools(session_id: str) -> bool:
    """Idempotently make the pair callable on the live agent(s) of a session."""
    try:
        touched = False
        for agent in _find_agents(session_id):
            try:
                touched = _inject(agent) or touched
            except Exception as exc:
                logger.debug("dshsim inject failed for one agent: %s", exc)
        return touched
    except Exception:
        return False


def strip_session_tools(session_id: str) -> bool:
    """Remove the pair from a session's live agent (best effort).

    After optout the outbound payload is already scrubbed by the middleware's
    default strip; this converges the agent-side state immediately instead of
    waiting for the next rebuild."""
    try:
        removed = False
        for agent in _find_agents(session_id):
            try:
                removed = _strip(agent) or removed
            except Exception as exc:
                logger.debug("dshsim strip failed for one agent: %s", exc)
        return removed
    except Exception:
        return False