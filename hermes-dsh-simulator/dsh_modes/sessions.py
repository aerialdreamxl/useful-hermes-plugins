# -*- coding: utf-8 -*-
"""Surface-aware session resolution + the ''new session only'' gate.

The slash-command handler runs outside any turn and receives only the raw
argument string, so it must locate the session it serves:

1. ContextVar-bound session (authoritative when present). The desktop/TUI
   ``command.dispatch`` path wraps plugin-command execution in
   ``_set_session_context(session_key, ...)`` — a ContextVar write; read it
   STRICTLY, never through the ``os.environ`` fallback: a child process
   inherits its launcher's ``HERMES_SESSION_KEY`` (desktop/gateway turn shells
   export it), and that stale foreign key must never outrank a live
   in-process signal.
2. Approval contextvar (turn-bound).
3. Desktop ladder over ``tui_gateway.server._sessions`` (same approach proven
   for earlier session-bound plugins): exactly one *running* record wins;
   otherwise a single record wins; otherwise the most recently active record.
   Two or more running records -> refuse (ambiguous).
4. CLI: ``get_plugin_manager()._cli_ref`` carries the interactive session.
5. ``os.environ`` fallback — LAST resort only (headless/cron children).

The gate then asks: does this session already have messages?
* Our own turn tracker (``registry.turn_seen``) — set by the llm_request
  middleware, so any session that already ran a request refuses.
* Desktop live record: non-empty ``history`` / ``display_history_prefix`` /
  ``resume_message_count`` / ``running`` / ``resume_hydrating`` -> refuse.
* CLI (best effort): ``_cli_ref.agent`` with a non-empty message list -> refuse.

Everything is defensive: on surprise we prefer refusing the *enable* (the
user asked for ''only message-less sessions'') while ``optout`` stays
unconditional and never touches the gate.
"""

from __future__ import annotations

import logging

from . import registry

logger = logging.getLogger("plugin.hermes-dsh-simulator")


def _get_session_env(name: str, default: str = "") -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, default) or default)
    except Exception:
        return default


def _contextvar_session_key() -> str:
    """The session key bound as a ContextVar in THIS process/task — strict, no env fallback.

    ``gateway.session_context.get_session_env`` documents its ``os.environ`` fallback as
    intentional for CLI/cron, but an agent-launched child (desktop/gateway turn shell)
    inherits its LAUNCHER's ``HERMES_SESSION_KEY``; trusting it here once aimed
    ``/dshsim simple`` at the launcher's session instead of the CLI's own (observed
    live 2026-09-29: mode keyed to the desktop chat while the CLI turn ran under its own
    id — payload rewrite, guard and injection all missed). ContextVar-only keeps inherited
    stale values out; the env read survives as the last-resort branch of resolve_session.
    """
    try:
        from gateway import session_context as sc

        value = sc._SESSION_KEY.get()
        return value if isinstance(value, str) and value else ""
    except Exception:
        return ""


def _approval_key() -> str:
    """The turn-bound approval session key — STRICT contextvar read, no env fallback.

    ``tools.approval.get_current_session_key`` falls back to
    ``session_context.get_session_env`` → ``os.environ``, which re-imports a launcher's
    stale ``HERMES_SESSION_KEY`` (the very inheritance bug step 1 hardens against; found
    live in v0.3.1's CLI retest — the fallback silently re-selected the stale key).
    The contextvar alone is authoritative: turns bind it via
    ``set_current_session_key``; outside a turn it is empty and resolution moves on to
    process-local signals.
    """
    try:
        from tools.approval_context import _approval_session_key

        value = _approval_session_key.get()
        return value if isinstance(value, str) and value else ""
    except Exception:
        return ""


def _tui_sessions():
    try:
        import tui_gateway.server as tgs  # type: ignore

        return getattr(tgs, "_sessions", None)
    except Exception:
        return None


def _cli_ref():
    try:
        from hermes_cli.plugins import get_plugin_manager  # type: ignore

        return getattr(get_plugin_manager(), "_cli_ref", None)
    except Exception:
        return None


def session_record(session_key: str):
    """The live desktop/TUI record for *session_key*, when this process serves one."""
    sessions = _tui_sessions()
    if not isinstance(sessions, dict) or not session_key:
        return None
    for record in sessions.values():
        if isinstance(record, dict) and str(record.get("session_key") or "") == session_key:
            return record
    return None


def resolve_session():
    """Resolve + (under ``HERMES_DSHSIM_DEBUG=1``) log which rung answered.

    The ladder lives in ``_resolve_session_impl``.
    """
    key, surface, reason = _resolve_session_impl()
    try:
        import os as _os

        if _os.environ.get("HERMES_DSHSIM_DEBUG"):
            logger.info("resolve_session -> key=%r surface=%s reason=%s", key, surface, reason)
    except Exception:
        pass
    return key, surface, reason


def _resolve_session_impl():
    """Return ``(session_key | None, surface, reason)`` for the invoking chat.

    Ladder (first hit wins) — strictest process-local signal first, so an inherited
    ``HERMES_SESSION_KEY`` can never outrank a live one:

    1. ContextVar-bound key (desktop ``command.dispatch`` seam).
    2. approval contextvar (turn-bound).
    3. desktop session table (ambiguous running set -> refuse).
    4. CLI ``_cli_ref.session_id``.
    5. ``os.environ`` fallback (headless/cron children) — last resort only.
    """
    bound = _contextvar_session_key()
    sessions = _tui_sessions()

    if bound:
        surface = "desktop" if session_record(bound) is not None else "bound"
        return bound, surface, "bound session context"

    approval = _approval_key()
    if approval:
        return approval, "approval", "approval context key"

    if isinstance(sessions, dict) and sessions:
        records = [
            r for r in sessions.values() if isinstance(r, dict) and str(r.get("session_key") or "")
        ]
        running = [r for r in records if r.get("running")]
        if len(running) == 1:
            return str(running[0]["session_key"]), "desktop", "single running session"
        if len(running) > 1:
            return None, "desktop", "multiple running sessions"
        if len(records) == 1:
            return str(records[0]["session_key"]), "desktop", "single open session"
        if records:
            newest = max(records, key=lambda r: float(r.get("last_active") or r.get("created_at") or 0))
            return str(newest["session_key"]), "desktop", "most recently active session"

    ref = _cli_ref()
    if ref is not None:
        for candidate in (getattr(ref, "session_id", ""),
                          getattr(getattr(ref, "agent", None), "session_id", "")):
            key = str(candidate or "")
            if key:
                return key, "cli", "cli_ref.session_id"

    env_key = _get_session_env("HERMES_SESSION_KEY", "")
    if env_key:
        return env_key, "env", "environment fallback"

    return None, "unknown", "no session signal available"


def record_has_messages(record):
    """``True``/``False`` for a desktop record, ``None`` when there is no record."""
    if not isinstance(record, dict):
        return None
    if record.get("running"):
        return True
    if record.get("resume_hydrating"):
        return True
    if record.get("history"):
        return True
    if record.get("display_history_prefix"):
        return True
    try:
        if int(record.get("resume_message_count") or 0) > 0:
            return True
    except Exception:
        pass
    return False


def _cli_agent_has_messages(session_key: str):
    ref = _cli_ref()
    if ref is None or str(getattr(ref, "session_id", "") or "") != session_key:
        return None
    agent = getattr(ref, "agent", None)
    if agent is None:
        return False
    for attr in ("messages", "conversation_history"):
        try:
            value = getattr(agent, attr, None)
        except Exception:
            continue
        if isinstance(value, (list, tuple)) and len(value) > 0:
            return True
    return False


def can_enable(session_key: str):
    """The ''new session only'' gate. Returns ``(ok, reason)``.

    Used only for ``minimal``; ``optout`` never consults this."""
    if not session_key:
        return False, "no session"
    if registry.turn_seen(session_key):
        return False, "a turn already ran for this session in this process"
    has = record_has_messages(session_record(session_key))
    if has is True:
        return False, "session already has messages"
    if has is None:
        cli_has = _cli_agent_has_messages(session_key)
        if cli_has is True:
            return False, "session already has messages"
    return True, "message-less session"