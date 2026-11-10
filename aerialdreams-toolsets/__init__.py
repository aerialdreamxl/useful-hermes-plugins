"""aerialdreams-toolsets — AerialdreamXL's multi-toolset Hermes plugin.

One package, five toolsets: bilibili (anonymous), bilibili_unsafe (account),
multimedia-utils (ffmpeg/ASR/Omni), desktop-utils (clipboard/screenshot),
android-utils (adb/fastboot). All groups register by default; settings can
disable a group individually.
"""

from __future__ import annotations

import importlib
import logging
import os

from .common import config

logger = logging.getLogger(__name__)

_GROUPS = ["bilibili", "bilibili_unsafe", "multimedia", "desktop", "android"]

_SETTING_KEYS = [
    "group_bilibili_enabled", "group_bilibili_unsafe_enabled",
    "group_multimedia_enabled", "group_desktop_enabled", "group_android_enabled",
    "multimedia_api_key_env", "multimedia_base_url",
    "asr_sync_model", "asr_async_model", "asr_wait_seconds",
    "asr_poll_interval", "asr_watch_max_seconds",
    "omni_model", "omni_max_file_mb",
]

_SECTION = (
    "## aerialdreams-toolsets\n"
    "Personal toolbox, five toolsets:\n"
    "- `bilibili`: anonymous Bilibili reads (search/info/comments/danmaku/subtitle/download/user/space/hot/related/collection).\n"
    "- `bilibili_unsafe`: account-bound Bilibili tools (QR login, HD download, like/coin/fav, DM, comment/danmaku write, favorites, watch-later, history, AI subtitle, follow, space dynamics). "
    "They act AS THE USER: only run them on an explicit user instruction, and show the exact text/action before posting.\n"
    "- `multimedia-utils`: ffmpeg (probe/convert/trim/mux/extract_audio/extract_frames/thumbnail/burn_subtitle/gif), "
    "dashscope_asr (sync short audio; async filetrans with timestamps and a steer-back when done), "
    "dashscope_qwenomni_analysis (audio/video + custom prompt -> Qwen-Omni analysis; >10MB is compressed then trimmed).\n"
    "- `desktop-utils`: clipboard read/write, screenshot (full/region/window).\n"
    "- `android-utils`: adb (devices/shell/push/pull/logcat/screenshot/install/props/am/pm), fastboot (flash/oem require confirm=true).\n"
)


def _config_fallback(key: str):
    """Hand-edited config often uses the bare plugin name even when the plugin
    directory is nested (``custom-plugins/<name>``); check both entry keys."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        entries = ((cfg.get("plugins") or {}).get("entries") or {})
        for entry_id in ("aerialdreams-toolsets", "custom-plugins/aerialdreams-toolsets"):
            node = entries.get(entry_id) or {}
            settings = node.get("settings") or {}
            if key in settings:
                return settings[key]
    except Exception:
        pass
    return None


def register(ctx) -> None:
    home = getattr(ctx, "hermes_home", None) or os.environ.get("HERMES_HOME")
    config.set_home(str(home) if home else "")
    manifest = getattr(ctx, "manifest", None)
    plugin_name = (getattr(manifest, "name", "") if manifest is not None else "") \
        or "aerialdreams-toolsets"
    config.set_plugin_id(plugin_name)

    values = {}
    for key in _SETTING_KEYS:
        try:
            value = ctx.get_config(key)
        except Exception:
            value = None
        if value is None:
            value = _config_fallback(key)
        if value is not None:
            values[key] = value
    config.set_settings(values)

    for name in _GROUPS:
        if not config.group_enabled(name):
            logger.info("aerialdreams-toolsets: group %s disabled by settings", name)
            continue
        try:
            module = importlib.import_module(".groups.%s" % name, __package__)
            module.register(ctx)
        except Exception:
            logger.exception("aerialdreams-toolsets: group %s failed to register", name)

    try:
        ctx.register_system_prompt_section("aerialdreams-toolsets", _SECTION, max_chars=2400)
    except Exception:
        logger.debug("aerialdreams-toolsets: system prompt section skipped", exc_info=True)