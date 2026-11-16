"""Account store — $HERMES_HOME/.advanced-custom-providers.json (v2 spec §3).

Two-tier map ``platform -> account_name -> credential dict``. Plaintext per
owner ruling; the ONLY runtime dependency (external export files are import
material, not read at request time — R1').

Safety:
- atomic write via the core's ``atomic_json_write`` (temp+fsync+os.replace,
  mode preserved) when available, else a stdlib tmp+os.replace fallback.
- cross-process lock (msvcrt on Windows, fcntl elsewhere) around read-modify-
  write, mirroring credential_pool's discipline — a CLI ``login`` and a
  runtime refresh must not clobber each other's rotation.
- secrets NEVER appear in any return value meant for display; callers use
  :func:`summary` which redacts token fields.

Shape per platform is adapter-defined (store stays schema-agnostic): codex
carries id/access/refresh/account_id/last_refresh/email/expired, antigravity
just refresh_token, etc. The store guarantees only the two-tier keys +
JSON round-trip + concurrency.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

# fields treated as secret for redaction / "has token" summaries
_SECRET_FIELDS = {"access_token", "id_token", "refresh_token", "api_key",
                  "token", "authorization", "code_verifier"}
# name-safe: reject path separators / control chars in platform & account names
_NAME_RE = re.compile(r"^[\w.@:+\- ]{1,200}$")

_WRITE_LOCK = threading.Lock()  # intra-process; inter-process lock below


class AccountStoreError(RuntimeError):
    pass


def store_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / ".advanced-custom-providers.json"


def _load_unlocked(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AccountStoreError(f"account store unreadable: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _save_unlocked(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from utils import atomic_json_write

        # mode 0o600 only meaningful on POSIX; harmless attempt on Windows.
        atomic_json_write(path, data, mode=0o600)
    except Exception:
        _fallback_atomic(path, data)


def _fallback_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".adv-accts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class _InterProcessLock:
    """Cross-process lock via an adjacent lockfile (O_EXCL), fronted by an
    in-process RLock so intra-process contention never reaches the file.

    Why the front matters (live repro 2026-10-30): O_EXCL serialization is
    process-scoped. While thread A holds the lockfile, thread B in the SAME
    process gets FileExistsError, reads the holder pid (its own), sees
    OpenProcess succeed, concludes "holder alive" — and spins to a full
    timeout. Every concurrent-thread path (the single-flight refresh, the
    takeover logging, aux + main clients) inherits that false deadlock.
    With the RLock first, exactly one thread per process ever touches the
    lockfile, and re-entrant acquisitions in the same thread (get() inside
    a mutate()) skip the file dance entirely via a depth counter.

    Retries stay bounded so a dead holder's stale lock file is broken."""

    #: one in-process mutex per lockfile path, shared by all instances
    _PROC_LOCKS: "dict[str, threading.RLock]" = {}
    _PROC_LOCKS_GUARD = threading.Lock()
    _DEPTH = threading.local()

    def __init__(self, path: Path, *, timeout: float = 10.0) -> None:
        self._lockfile = path.with_name(path.name + ".lock")
        self._timeout = timeout
        self._fd: int | None = None
        self._file_held = False
        key = str(self._lockfile)
        with _InterProcessLock._PROC_LOCKS_GUARD:
            self._proc_lock = _InterProcessLock._PROC_LOCKS.get(key)
            if self._proc_lock is None:
                self._proc_lock = threading.RLock()
                _InterProcessLock._PROC_LOCKS[key] = self._proc_lock

    def __enter__(self):
        self._proc_lock.acquire()
        # Re-entrant on this thread? The file lock is already ours — depth>1
        # must NOT re-O_EXCL (same-pid FileExistsError would deadlock).
        depth = getattr(_InterProcessLock._DEPTH, "depth", 0)
        _InterProcessLock._DEPTH.depth = depth + 1
        if depth > 0:
            return self
        import time as _t

        deadline = _t.monotonic() + self._timeout
        while True:
            try:
                self._fd = os.open(self._lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                self._file_held = True
                return self
            except FileExistsError:
                # break a stale lock whose holder pid is gone
                if self._holder_dead():
                    try:
                        os.remove(self._lockfile)
                        continue
                    except OSError:
                        pass
                if _t.monotonic() >= deadline:
                    break
                _t.sleep(0.1)
            except FileNotFoundError:
                # Windows race: holder released (os.remove) between our
                # FileExistsError and this retry's O_EXCL open — the create
                # half of the open still observed the dying entry. Retryable.
                if _t.monotonic() >= deadline:
                    break
                _t.sleep(0.02)
            except OSError as exc:
                self._release_proc()
                raise AccountStoreError(f"cannot lock account store: {exc}") from exc
        self._release_proc()
        raise AccountStoreError("account store lock timeout")

    def _release_proc(self) -> None:
        depth = getattr(_InterProcessLock._DEPTH, "depth", 1)
        _InterProcessLock._DEPTH.depth = max(depth - 1, 0)
        self._proc_lock.release()

    def _holder_dead(self) -> bool:
        try:
            pid = int(self._lockfile.read_text().strip() or "0")
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return False
        try:
            import ctypes

            if os.name == "nt":
                PROCESS_QUERY = 0x1000
                k32 = ctypes.windll.kernel32
                h = k32.OpenProcess(PROCESS_QUERY, False, pid)
                if h:
                    k32.CloseHandle(h)
                    return False
                return True  # OpenProcess failed => gone or no access; assume stale
        except Exception:
            pass
        return False

    def __exit__(self, *exc):
        depth = getattr(_InterProcessLock._DEPTH, "depth", 1)
        if self._file_held:
            # Only the outermost frame of the file-holding thread tears the
            # lockfile down; inner re-entrant contexts just decrement.
            _InterProcessLock._DEPTH.depth = depth - 1
            self._file_held = False
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            try:
                os.remove(self._lockfile)
            except OSError:
                pass
        else:
            _InterProcessLock._DEPTH.depth = max(depth - 1, 0)
        self._proc_lock.release()
        return False


class AccountStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path_override = path

    @property
    def path(self) -> Path:
        return self._path_override or store_path()

    def load(self) -> dict:
        # No lock: saves are atomic os.replace renames, so a reader either
        # sees the old or the new complete file — never a torn one. Locking
        # reads turned every concurrent writer into a false "lock timeout"
        # (a waiting reader queued behind a slow writer's cross-process
        # lockfile spin). _load_unlocked tolerates a missing file (fresh
        # store) and is the same read the mutate() critical section uses.
        return _load_unlocked(self.path)

    def mutate(self, fn: Callable[[dict], Any]) -> Any:
        """Atomic read-modify-write: ``fn(data) -> result``; data mutated in
        place and persisted when fn returns non-``SKIP``. Returns fn's result
        (or None when it returns SKIP)."""
        with _WRITE_LOCK, _InterProcessLock(self.path):
            data = _load_unlocked(self.path)
            result = fn(data)
            if result is not _SKIP:
                _save_unlocked(self.path, data)
            return result

    def put(self, platform: str, name: str, cred: dict) -> None:
        platform = _validate_name(platform, "platform")
        name = _validate_name(name, "account")
        if not isinstance(cred, dict):
            raise AccountStoreError("credential must be a dict")

        def _w(data: dict):
            data.setdefault(platform, {})[name] = cred
        self.mutate(_w)

    def get(self, platform: str, name: str) -> Optional[dict]:
        data = self.load()
        acct = (data.get(_validate_name(platform, "platform")) or {}).get(
            _validate_name(name, "account"))
        return acct if isinstance(acct, dict) else None

    def delete(self, platform: str, name: str) -> bool:
        platform = _validate_name(platform, "platform")
        name = _validate_name(name, "account")

        def _w(data: dict):
            bucket = data.get(platform)
            if isinstance(bucket, dict) and name in bucket:
                del bucket[name]
                if not bucket:
                    data.pop(platform, None)
                return True
            return False
        return bool(self.mutate(_w))

    def list_accounts(self) -> list[tuple[str, str, dict]]:
        """(platform, name, redacted_summary) — safe for CLI display."""
        out = []
        for platform, bucket in (self.load()).items():
            if not isinstance(bucket, dict):
                continue
            for name, cred in bucket.items():
                out.append((platform, name, summary(cred) if isinstance(cred, dict) else {}))
        return out


_SKIP = object()


def _validate_name(value: str, kind: str) -> str:
    value = str(value or "").strip()
    if not _NAME_RE.match(value):
        raise AccountStoreError(f"invalid {kind} name {value!r}")
    return value


def redact(cred: dict) -> dict:
    """Copy with every secret field replaced by '<set>'/absent (never a value)."""
    out = {}
    for k, v in cred.items():
        if k.lower() in _SECRET_FIELDS:
            out[k] = "<set>" if str(v or "").strip() else ""
        else:
            out[k] = v
    return out


def summary(cred: dict) -> dict:
    """Display-safe account view: identity/expiry fields verbatim, secrets -> bool."""
    return {
        "email": cred.get("email") or "",
        "account_id": cred.get("account_id") or "",
        "expired": cred.get("expired") or "",
        "last_refresh": cred.get("last_refresh") or "",
        "has_access": bool(str(cred.get("access_token") or "").strip()),
        "has_refresh": bool(str(cred.get("refresh_token") or "").strip()),
    }
