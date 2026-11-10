"""Plugin settings + paths, captured once at register() time.

All groups default to enabled; settings only ever turn things off or point the
ASR/Omni calls at a different endpoint. ``ctx.get_config`` reads
``plugins.entries.<plugin_id>.settings.<dotted.key>``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

_settings: dict = {}
_home: str = ""
_plugin_id: str = "aerialdreams-toolsets"


def set_home(home: Optional[str]) -> None:
    global _home
    _home = str(home or os.environ.get("HERMES_HOME") or "")


def home() -> str:
    return _home or os.environ.get("HERMES_HOME") or ""


def set_plugin_id(pid: str) -> None:
    global _plugin_id
    _plugin_id = pid or _plugin_id


def plugin_id() -> str:
    return _plugin_id


def set_settings(values: dict) -> None:
    global _settings
    _settings = dict(values or {})


_ALIASES = {
    "multimedia.api_key_env": "multimedia_api_key_env",
    "multimedia.base_url": "multimedia_base_url",
    "multimedia.asr.sync_model": "asr_sync_model",
    "multimedia.asr.async_model": "asr_async_model",
    "multimedia.asr.wait_seconds": "asr_wait_seconds",
    "multimedia.asr.poll_interval": "asr_poll_interval",
    "multimedia.asr.watch_max_seconds": "asr_watch_max_seconds",
    "multimedia.omni.model": "omni_model",
    "multimedia.omni.max_file_mb": "omni_max_file_mb",
}


def cfg(key: str, default: Any = None) -> Any:
    """Flat-key lookup (dotted legacy keys alias to the flat settings keys)."""
    key = _ALIASES.get(key, key)
    cur: Any = _settings
    for part in key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            cur = None
            break
    return default if cur is None else cur


def group_enabled(name: str) -> bool:
    return bool(cfg("group_%s_enabled" % name.replace("-", "_"), True))


def scratch_dir(*parts: str) -> str:
    base = os.path.join(home() or os.getcwd(), "cache", "scratch", "aerialdreams")
    path = os.path.join(base, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def plugin_data_dir() -> str:
    path = os.path.join(home() or os.getcwd(), "plugins-data", _plugin_id)
    os.makedirs(path, exist_ok=True)
    return path