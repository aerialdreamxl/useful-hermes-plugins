# -*- coding: utf-8 -*-
"""Persistent takeover store — ``$HERMES_HOME/dshsim-sessions.json``.

Every session the DSH simulator takes over is recorded here so the takeover
survives process restarts: plugin registration re-arms each stored session's
mode (registry), and the middleware re-asserts both the mode and the
``[DSHSIM]`` title label on every outbound request of a stored session.
``/dshsim optout`` removes the entry and unlabels the title.

Format::

    {
      "version": 1,
      "updated_at": "2026-09-29T18:50:00+08:00",
      "sessions": {
        "20260929_172726_13bf5e": {
          "enabled_at": "2026-09-29T17:27:00+08:00",
          "surface": "desktop"
        }
      }
    }

Everything fails open: an unreadable file is treated as empty (and rebuilt on
the next write); a failed write degrades only persistence, never the
in-process mode. Reads are served from an mtime-validated in-memory cache, so
the per-request ``is_taken`` check costs one ``stat`` in the steady state.

Tests (and only tests) can point the store at a scratch file via the
``HERMES_DSHSIM_STORE`` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("plugin.hermes-dsh-simulator")

FILENAME = "dshsim-sessions.json"
VERSION = 1
_ENV_OVERRIDE = "HERMES_DSHSIM_STORE"

_lock = threading.RLock()
_cache = {"mtime": None, "data": None}


def _store_path() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / FILENAME


def _now() -> str:
    try:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _empty() -> dict:
    return {"version": VERSION, "updated_at": "", "sessions": {}}


def _load_uncached(path: Path) -> dict:
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
                data = _empty()
                data["version"] = raw.get("version") or VERSION
                data["updated_at"] = str(raw.get("updated_at") or "")
                data["sessions"] = {
                    str(key): dict(value) if isinstance(value, dict) else {}
                    for key, value in raw["sessions"].items()
                    if isinstance(key, str) and key
                }
                return data
    except Exception as exc:
        logger.warning("dshsim store: unreadable (%s); treating as empty", exc)
    return _empty()


def _cached() -> dict:
    """The cached store dict (revalidated against the file's mtime)."""
    with _lock:
        path = _store_path()
        try:
            mtime = path.stat().st_mtime_ns if path.is_file() else 0
        except Exception:
            mtime = 0
        if _cache["data"] is None or _cache["mtime"] != mtime:
            _cache["data"] = _load_uncached(path)
            _cache["mtime"] = mtime
        return _cache["data"]


def load() -> dict:
    """A read-safe copy of the current store content. Never raises."""
    data = _cached()
    return {
        "version": data.get("version", VERSION),
        "updated_at": str(data.get("updated_at") or ""),
        "sessions": dict(data.get("sessions") or {}),
    }


def _write(data: dict) -> bool:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["version"] = payload.get("version") or VERSION
        payload["updated_at"] = _now()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
        with _lock:
            _cache["data"] = payload
            try:
                _cache["mtime"] = path.stat().st_mtime_ns
            except Exception:
                _cache["mtime"] = None
        return True
    except Exception as exc:
        logger.warning("dshsim store: write failed (%s)", exc)
        return False


def keys() -> list:
    return sorted(load()["sessions"].keys())


def entry(session_key: str):
    key = str(session_key or "").strip()
    if not key:
        return None
    value = load()["sessions"].get(key)
    return dict(value) if isinstance(value, dict) else None


def is_taken(session_key: str) -> bool:
    key = str(session_key or "").strip()
    return bool(key) and key in load()["sessions"]


def add(session_key: str, surface: str = "") -> bool:
    """Register (idempotently) a taken-over session. Returns write success."""
    key = str(session_key or "").strip()
    if not key:
        return False
    with _lock:
        data = _cached()
        sessions = dict(data.get("sessions") or {})
        prior = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
        sessions[key] = {
            "enabled_at": prior.get("enabled_at") or _now(),
            "surface": prior.get("surface") or str(surface or ""),
        }
        data = dict(data)
        data["sessions"] = sessions
        return _write(data)


def remove(session_key: str) -> bool:
    """Drop a session's entry. Returns True only when one existed."""
    key = str(session_key or "").strip()
    if not key:
        return False
    with _lock:
        data = _cached()
        sessions = dict(data.get("sessions") or {})
        existed = key in sessions
        sessions.pop(key, None)
        if not existed:
            return False
        data = dict(data)
        data["sessions"] = sessions
        _write(data)
        return True


def hydrate() -> int:
    """Re-arm every stored session's mode in the in-process registry.

    Called once at plugin registration: a backend restart then keeps all
    sessions taken over before the restart (the store is the source of truth
    for 'who is under the simulator'). Returns the number of sessions armed.
    """
    armed = 0
    try:
        from . import registry  # local import: keep import order cycle-free

        mode = getattr(registry, "MODE_MINIMAL", None)
        if not mode:
            return 0
        for key in keys():
            registry.set_mode(key, mode)
            armed += 1
    except Exception as exc:
        logger.debug("dshsim store: hydrate failed open: %s", exc)
    return armed