# -*- coding: utf-8 -*-
"""Per-session mode state (in-memory) + turn tracker.

The in-memory map is the hot path (the same process that dispatches
``/dshsim`` later builds the LLM requests, so a dict is enough for a
session's lifetime). Persistence lives in ``state.py``: every take-over is
recorded in ``$HERMES_HOME/dshsim-sessions.json``, registration re-arms all
stored sessions (``state.hydrate``), and the llm_request middleware
re-asserts both the mode and the ``[DSHSIM]`` label per request — a backend
restart no longer drops take-overs. ``/dshsim optout`` remains available at
all times regardless.

Modes are keyed by the same ``session_key`` that the llm_request middleware
receives as ``session_id`` (desktop: the live session record's
``session_key``; CLI: the CLI session id).
"""

from __future__ import annotations

import threading

MODE_MINIMAL = "minimal"

_lock = threading.RLock()
_modes: dict = {}
_turn_seen: set = set()


def set_mode(session_key: str, mode: str) -> None:
    if not session_key:
        return
    with _lock:
        _modes[session_key] = mode


def clear_mode(session_key: str) -> bool:
    """Returns True when a mode was previously set for this session."""
    with _lock:
        return _modes.pop(session_key, None) is not None


def get_mode(session_key: str):
    if not session_key:
        return None
    with _lock:
        return _modes.get(session_key)


def mark_turn_seen(session_key: str) -> None:
    """A real LLM request was built for this session -> it is no longer 'message-less'."""
    if not session_key:
        return
    with _lock:
        _turn_seen.add(session_key)


def turn_seen(session_key: str) -> bool:
    if not session_key:
        return False
    with _lock:
        return session_key in _turn_seen


def snapshot() -> dict:
    with _lock:
        return {"modes": dict(_modes), "turns_seen": sorted(_turn_seen)}