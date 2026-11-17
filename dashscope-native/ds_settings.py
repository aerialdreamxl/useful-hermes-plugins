"""Shared settings tree + endpoint/key derivation for dashscope-native v0.5.

Authoritative layout = PROMPT.md §2 (owner's exp.yaml template):
  top level: base_url / key_env (metered) + tp_base_url / tp_key_env (Token Plan)
  per block: enabled = metered variant, enabled_tp = Token Plan variant
  per-block params: bare name = metered lane; ``tp_``-prefixed twin = TP lane,
  TP value precedence tp_X -> bare X (fallback when tp_X absent).

DIY rules kept: per-key auto-fill only seeds bare keys (a seeded tp_* key
would lock metered defaults into the TP lane and kill the fallback); values
written through the official ctx.set_config; key_env stores a VARIABLE NAME,
never the secret itself.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

SettingGetter = Callable[[str, Any], Any]

DEFAULTS: Dict[str, Any] = {
    # top-level shared pairs (PROMPT.md §2/§5)
    "base_url": "dashscope://cn-beijing",
    "key_env": "DASHSCOPE_API_KEY",
    "tp_base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com",
    "tp_key_env": "DASHSCOPE_TOKEN_PLAN_API_KEY",
    # chat native wire (§3)
    "model_api_mode.enabled": True,
    # web MCP (§6.2) — metered only, no documented TP web endpoint
    "web.enabled": True,
    # voice (§4) — bare keys = metered lane, tp_ twins = TP lane
    "voice.tts.enabled": True,
    "voice.tts.enabled_tp": True,
    "voice.tts.model": "qwen3-tts-flash",
    "voice.tts.voice": "Cherry",
    "voice.tts.sample_rate": 24000,
    "voice.tts.stream_pcm": True,
    "voice.tts.tp_model": "qwen-audio-3.0-tts-plus",
    "voice.tts.tp_voice": "longanxiaoxin",
    "voice.tts.tp_sample_rate": 22050,
    "voice.stt.enabled": True,
    "voice.stt.enabled_tp": True,
    "voice.stt.model": "qwen3-asr-flash",
    "voice.stt.tp_model": "qwen-audio-3.0-asr-flash",
    # t2i (§6.1) — one provider class, two construction parameter sets
    "t2i.enabled": True,
    "t2i.enabled_tp": True,
    "t2i.model": "wan2.7-image",
    "t2i.models_ttl_s": 3600,
}

_MISSING = object()


def seed(ctx: Any) -> None:
    """Per-key auto-fill: only keys ABSENT from settings are written.

    Explicitly stored ""/false/0 are user intent and never overwritten
    (DIY 规范, 2026-11-09). tp_* keys here are part of the owner's template
    defaults; an absent tp_* key later means "fall back to bare value".
    """
    for key, default in DEFAULTS.items():
        if ctx.get_config(key, _MISSING) is _MISSING:
            ctx.set_config(key, default)


def lane_value(get_setting: SettingGetter, key: str, default: Any,
               *, tp: bool = False) -> Any:
    """Read one block parameter for the requested lane.

    TP lane: tp_<last segment> -> bare key -> default (PROMPT.md §4 ruling:
    tp_ prefix = per-lane override, missing = share the metered value).
    """
    if tp:
        head, _, last = key.rpartition(".")
        tp_key = (head + ".tp_" + last) if head else ("tp_" + last)
        value = get_setting(tp_key, _MISSING)
        if value is not _MISSING and value is not None and value != "":
            return value
    value = get_setting(key, _MISSING)
    if value is _MISSING or value is None or value == "":
        return default
    return value


def rest_host(get_setting: SettingGetter) -> str:
    """Derive the metered REST host from the shared base_url (§5.2 rule 3).

    dashscope://<region>[@<workspace>] -> workspace gateway if present,
    else the region's public host. A plain https:// base_url passes through.
    """
    from .ds_uri import parse_dashscope_uri

    base = str(get_setting("base_url", DEFAULTS["base_url"]) or "").strip()
    ref = parse_dashscope_uri(base)  # malformed dashscope:// raises loudly
    if ref is None:
        return base.rstrip("/")
    if ref.workspace:
        return f"https://{ref.gateway_host}"
    if ref.region == "cn-beijing":
        return "https://dashscope.aliyuncs.com"
    # intl regions share the intl public gateway (SDK _config parity)
    return "https://dashscope-intl.aliyuncs.com"


def tp_host(get_setting: SettingGetter) -> str:
    return str(get_setting("tp_base_url", DEFAULTS["tp_base_url"]) or "").rstrip("/")
