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

Two bounds keep the store honest:

* **TTL** — a takeover older than ``SESSION_TTL_DAYS`` is dropped at load (and
  therefore skipped by :func:`hydrate`), so a restart can never silently revive
  a months-old "changed brain" session from an unrelated conversation.
* **Writes** — read-modify-write runs under a write lock (an OS file lock
  across processes plus a process mutex between threads) and publishes through
  a unique temp name, so a gateway process and a CLI process adding sessions at
  the same time cannot overwrite each other's records.

Tests (and only tests) can point the store at a scratch file via the
``HERMES_DSHSIM_STORE`` environment variable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("plugin.hermes-dsh-simulator")

FILENAME = "dshsim-sessions.json"
VERSION = 1
_ENV_OVERRIDE = "HERMES_DSHSIM_STORE"
SESSION_TTL_DAYS = 7        # stored takeovers older than this die at load
_LOCK_TIMEOUT_S = 2.0       # write lock: abandon the write after this long

_lock = threading.RLock()
_range_lock = threading.RLock()     # process-local half of the write lock
_thread_local = threading.local()   # per-thread lock depth + held lock handle
_cache: dict = {"mtime": None, "data": None}


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


def _stamp(value):
    """Epoch seconds from an ISO timestamp; None when unusable.

    A naive timestamp is read as local time (``_now`` writes an aware one) so a
    hand-edited file cannot flip the age sign by a timezone.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except Exception:
        return None


def _entry_stamp(entry: dict):
    """The newest usable timestamp on an entry (created/updated variants included)."""
    newest = None
    for field in ("enabled_at", "created", "created_at", "updated_at"):
        value = _stamp(entry.get(field))
        if value is not None and (newest is None or value > newest):
            newest = value
    return newest


def _expired(entry, now: float) -> bool:
    """True when the entry is older than ``SESSION_TTL_DAYS``.

    An entry with no usable timestamp is also expired: a takeover we cannot age
    must not be revived across restarts, so hydrate refuses it.
    """
    if not isinstance(entry, dict):
        return True
    stamp = _entry_stamp(entry)
    return stamp is None or (now - stamp) > SESSION_TTL_DAYS * 86400


def _load_uncached(path: Path):
    """Parsed store, or None when the file exists but could not be read right now.

    None (rather than an empty store) lets the cache keep its last good view
    across a transient Windows sharing violation; a merely CORRUPT file still
    degrades to an empty store so the next write rebuilds it.
    """
    try:
        if not path.is_file():
            return _empty()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            logger.warning("dshsim store: unreadable (%s); keeping the last good view", exc)
            return None
        except ValueError as exc:
            logger.warning("dshsim store: corrupt (%s); rebuilding from empty", exc)
            return _empty()
    except Exception as exc:
        logger.warning("dshsim store: unreadable (%s); keeping the last good view", exc)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
        return _empty()
    data = _empty()
    data["version"] = raw.get("version") or VERSION
    data["updated_at"] = str(raw.get("updated_at") or "")
    now = time.time()
    dropped = 0
    for key, value in raw["sessions"].items():
        if not isinstance(key, str) or not key:
            continue
        entry = dict(value) if isinstance(value, dict) else {}
        if _expired(entry, now):
            dropped += 1
            continue
        data["sessions"][key] = entry
    if dropped:
        logger.debug(
            "dshsim store: dropped %s session(s) past the %s-day TTL",
            dropped, SESSION_TTL_DAYS,
        )
    return data


def _cached() -> dict:
    """The cached store dict (revalidated against the file's mtime)."""
    with _lock:
        path = _store_path()
        try:
            mtime = path.stat().st_mtime_ns if path.is_file() else 0
        except Exception:
            mtime = 0
        if _cache["data"] is None or _cache["mtime"] != mtime:
            loaded = _load_uncached(path)
            if loaded is None:
                return _cache["data"] or _empty()
            _cache["data"] = loaded
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


def _lock_path() -> Path:
    path = _store_path()
    return path.with_name(path.name + ".lock")


def _try_file_lock(handle) -> bool:
    """Non-blocking exclusive lock on an open lock file. No create/delete dance:
    the OS drops the lock when the holder dies, so a crash never wedges the store."""
    try:
        if os.name == "nt":
            import msvcrt  # local import: Windows-only module

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # local import: POSIX-only module

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _drop_file_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _acquire() -> bool:
    """Take the store's write lock: process-local mutex, then an OS file lock.

    The file lock is what keeps a gateway process and a CLI process from
    clobbering each other; the mutex is what serializes threads inside one
    process (OS file locks do not reliably exclude a second handle from the same
    process). Re-entrant per thread so the helpers may call ``_write`` while
    already holding it. ``False`` means the lock stayed busy past
    ``_LOCK_TIMEOUT_S``; the caller must then abandon its write.
    """
    depth = getattr(_thread_local, "depth", 0)
    if depth:
        _thread_local.depth = depth + 1
        return True
    if not _range_lock.acquire(timeout=_LOCK_TIMEOUT_S):
        return False
    lock_file = _lock_path()
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("dshsim store: lock dir unavailable (%s)", exc)
        _range_lock.release()
        return False
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            handle = open(str(lock_file), "a+b")
        except OSError as exc:
            logger.warning("dshsim store: lock file unavailable (%s)", exc)
            _range_lock.release()
            return False
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")  # a byte must exist before a range lock
                handle.flush()
            handle.seek(0)
            if _try_file_lock(handle):
                _thread_local.handle = handle
                _thread_local.depth = 1
                return True
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
        if time.monotonic() >= deadline:
            _range_lock.release()
            return False
        time.sleep(0.02)


def _release() -> None:
    """Drop one level of the write lock; the outermost level frees the file lock."""
    depth = getattr(_thread_local, "depth", 0)
    if depth > 1:
        _thread_local.depth = depth - 1
        return
    _thread_local.depth = 0
    if not depth:
        return
    handle = getattr(_thread_local, "handle", None)
    _thread_local.handle = None
    if handle is not None:
        _drop_file_lock(handle)
        try:
            handle.close()
        except OSError:
            pass
    try:
        _range_lock.release()
    except RuntimeError:
        pass


def _write(data: dict) -> bool:
    """Persist *data* atomically, under the cross-process lock.

    A lock that stays busy past the timeout abandons the write (``False``)
    instead of racing another process.
    """
    if not _acquire():
        logger.warning("dshsim store: write abandoned (lock busy)")
        return False
    try:
        return _write_unlocked(data)
    finally:
        _release()


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """``os.replace`` with a short retry: on Windows a reader holding the store
    open makes the swap fail with a sharing violation for a few milliseconds."""
    last = None
    for attempt in range(6):
        try:
            os.replace(str(tmp), str(path))
            return
        except OSError as exc:
            last = exc
            time.sleep(0.02 * (attempt + 1))
    raise last  # type: ignore[misc]


def _write_unlocked(data: dict) -> bool:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["version"] = payload.get("version") or VERSION
        payload["updated_at"] = _now()
        # Unique temp name: two writers must never share a staging file, or one
        # replace() would publish the other's half-written bytes.
        tmp = path.with_name("%s.%s.%s.tmp" % (path.name, os.getpid(), uuid.uuid4().hex))
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace_with_retry(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
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


def _read_fresh() -> dict:
    """Re-read the store from disk while holding the write lock.

    The in-memory cache may predate another process's write, so a
    read-modify-write must start from the bytes on disk: otherwise it would
    publish a payload that silently drops the other writer's sessions.
    """
    path = _store_path()
    data = _load_uncached(path)
    if data is None:
        # Unreadable while we own the write lock: abandon this mutation rather
        # than publish an empty store over another writer's records.
        logger.warning("dshsim store: read-modify-write abandoned (store unreadable)")
        return None
    with _lock:
        _cache["data"] = data
        try:
            _cache["mtime"] = path.stat().st_mtime_ns
        except Exception:
            _cache["mtime"] = None
    return data


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
    """Register (idempotently) a taken-over session. Returns write success.

    Read-modify-write under the cross-process lock, starting from the bytes on
    disk: a concurrent writer's sessions survive this call instead of being
    overwritten.
    """
    key = str(session_key or "").strip()
    if not key:
        return False
    if not _acquire():
        logger.warning("dshsim store: session %s not persisted (lock busy)", key)
        return False
    try:
        data = _read_fresh()
        if data is None:
            return False
        sessions = dict(data.get("sessions") or {})
        prior = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
        sessions[key] = {
            "enabled_at": prior.get("enabled_at") or _now(),
            "surface": prior.get("surface") or str(surface or ""),
        }
        data = dict(data)
        data["sessions"] = sessions
        return _write_unlocked(data)
    finally:
        _release()


def remove(session_key: str) -> bool:
    """Drop a session's entry. Returns True only when one existed."""
    key = str(session_key or "").strip()
    if not key:
        return False
    if not _acquire():
        logger.warning("dshsim store: session %s not removed (lock busy)", key)
        return False
    try:
        data = _read_fresh()
        if data is None:
            return False
        sessions = dict(data.get("sessions") or {})
        if key not in sessions:
            return False
        sessions.pop(key, None)
        data = dict(data)
        data["sessions"] = sessions
        _write_unlocked(data)
        return True
    finally:
        _release()


def hydrate() -> int:
    """Re-arm every stored session's mode in the in-process registry.

    Called once at plugin registration: a backend restart then keeps the stored
    sessions inside the TTL window (the store is the source of truth for who is
    under the simulator). An expired entry is skipped here as well as at load,
    so a stale takeover is never revived. Returns the number of sessions armed.
    """
    armed = 0
    try:
        from . import registry  # local import: keep import order cycle-free

        mode = getattr(registry, "MODE_MINIMAL", None)
        if not mode:
            return 0
        now = time.time()
        for key, value in load()["sessions"].items():
            if _expired(value, now):
                logger.debug("dshsim store: hydrate skipped expired session %s", key)
                continue
            registry.set_mode(key, mode)
            armed += 1
    except Exception as exc:
        logger.debug("dshsim store: hydrate failed open: %s", exc)
    return armed