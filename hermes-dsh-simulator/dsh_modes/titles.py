# -*- coding: utf-8 -*-
"""``[DSHSIM]`` title label — prefix the session NAME of taken-over sessions.

The label lives in the host's own session titles (``state.db`` ``sessions.title``
written through ``SessionDB.set_session_title`` — the same manual writer the
desktop's rename uses), so it persists across restarts, shows up in the
sidebar / session list, and is removed by ``/dshsim optout``.

* ``ensure_label`` — no-op unless the session already has a title; prepends
  ``[DSHSIM] `` exactly once. Called at take-over time and re-asserted per
  request by the middleware, so a title born AFTER take-over (auto-titling
  runs inside a later turn prologue) gets labeled on the next request.
* ``strip_label`` — removes the prefix on optout (clears the title when it
  contained nothing but the label).

Writes ride the session's OWN live agent DB handle when available
(``agent._session_db`` — the handle the gateway itself writes through), with
a shared ``SessionDB`` on ``$HERMES_HOME/state.db`` as fallback. When this
process owns the desktop gateway, a live ``session.title`` event is emitted so
the sidebar updates immediately. Everything fails open.
"""

from __future__ import annotations

import logging
import sys

from . import inject

logger = logging.getLogger("plugin.hermes-dsh-simulator")

PREFIX = "[DSHSIM]"

# Fallback SessionDB instance (constructed lazily, once per process).
_db_ref = [None]


def _fallback_db():
    if _db_ref[0] is not None:
        return _db_ref[0]
    try:
        from pathlib import Path

        from hermes_constants import get_hermes_home  # type: ignore
        from hermes_state import SessionDB  # type: ignore

        _db_ref[0] = SessionDB(Path(get_hermes_home()) / "state.db")
    except Exception as exc:
        logger.debug("dshsim label: no fallback session db (%s)", exc)
        return None
    return _db_ref[0]


def _db_for(session_key: str):
    """The DB handle to write titles through: live agent's own handle first."""
    try:
        for agent in inject._find_agents(session_key):
            db = getattr(agent, "_session_db", None)
            if db is not None:
                return db
    except Exception:
        pass
    return _fallback_db()


def _read_title(db, session_key: str):
    try:
        return db.get_session_title(session_key)
    except Exception:
        return None


def _max_title_len(db) -> int:
    try:
        return int(getattr(db, "MAX_TITLE_LENGTH", 100) or 100)
    except Exception:
        return 100


def _apply(db, session_key: str, title: str) -> bool:
    """Write a title with the host's manual (user-authority) writer."""
    try:
        return bool(db.set_session_title(session_key, title))
    except ValueError as exc:
        # Provenance/uniqeness/validation refusal — never break the turn over it.
        logger.warning("dshsim label: title write rejected (%s)", exc)
        return False
    except Exception as exc:
        logger.debug("dshsim label: title write failed (%s)", exc)
        return False


def ensure_label(session_key: str) -> bool:
    """Make the session title carry ``[DSHSIM] ``. True = has label afterwards."""
    key = str(session_key or "").strip()
    if not key:
        return False
    try:
        db = _db_for(key)
        if db is None:
            return False
        title = _read_title(db, key)
        if not title:
            # No stored row/title yet (fresh draft): nothing to prefix; the
            # next request after auto-titling names it retries.
            return False
        if title.startswith(PREFIX):
            return True
        room = max(8, _max_title_len(db) - len(PREFIX) - 1)
        base = title if len(title) <= room else title[:room].rstrip()
        new_title = "%s %s" % (PREFIX, base)
        if not _apply(db, key, new_title):
            return False
        _emit_title(key, new_title)
        return True
    except Exception:
        return False


def strip_label(session_key: str) -> bool:
    """Remove the ``[DSHSIM] `` prefix (optout). True = a label was removed."""
    key = str(session_key or "").strip()
    if not key:
        return False
    try:
        db = _db_for(key)
        if db is None:
            return False
        title = _read_title(db, key)
        if not title or not title.startswith(PREFIX):
            return False
        base = title[len(PREFIX):].strip()
        # Empty base clears the title (empty clears, per the host's own contract).
        if not _apply(db, key, base):
            return False
        _emit_title(key, base)
        return True
    except Exception:
        return False


def _loaded_tui_module():
    """The gateway module ONLY when this process already owns it."""
    return sys.modules.get("tui_gateway.server") or sys.modules.get("tui_gateway")


def _ui_sid_for(session_key: str) -> str:
    try:
        table = inject._tui_sessions()
        if isinstance(table, dict):
            for sid, record in list(table.items()):
                if isinstance(record, dict) and str(record.get("session_key") or "") == session_key:
                    return str(sid)
    except Exception:
        pass
    return ""


def _emit_title(session_key: str, title: str) -> None:
    """Live-refresh the desktop sidebar, mirroring the host's own rename event."""
    try:
        module = _loaded_tui_module()
        emit = getattr(module, "_emit", None) if module is not None else None
        if emit is None:
            return
        ui_sid = _ui_sid_for(session_key)
        if not ui_sid:
            return
        emit("session.title", ui_sid, {"session_id": session_key, "title": title or ""})
    except Exception:
        pass