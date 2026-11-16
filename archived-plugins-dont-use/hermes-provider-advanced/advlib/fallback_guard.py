"""OpenRouter 隐式兜底闸门 — `block-custom-openrouter-fallback`。

背景（2026-11-02 取证）：核心 ``hermes_cli/runtime_provider.py`` 的解析阶梯末级
（``_openrouter_fallback`` → ``_resolve_openrouter_runtime``）对**裸 custom / auto**
请求无条件落到 ``https://openrouter.ai/api/v1``，并在 ``OPENROUTER_API_KEY`` 在场时
直接带上该密钥发请求。禁用 openrouter 供应商的守卫只看 requested 名字、不看解析结果，
所以这条路径既不报错也不告警——用户配置全部正确，流量仍会静默泄漏到 OpenRouter。

本模块给该接缝加一层身份校验包装：开关打开时，凡是"非显式 openrouter 请求、又落回
OpenRouter 默认端点、且 base_url 不是 config.yaml 里显式写下的镜像"的解析，一律抛
``AuthError``（fail-loud），让网关的 fallback 链按配置继续走，而不是静默花钱。

设置读取（两路，先官方后裸名兜底）::

    plugins.entries.hermes-provider-advanced.settings.block-custom-openrouter-fallback: true
    plugins.entries.custom-plugins/hermes-provider-advanced.settings.block-custom-openrouter-fallback: true

裸名兜底是必须的：本插件部署在 ``plugins/custom-plugins/<name>``，注册键是路径派生的
``custom-plugins/<name>``，用户写在裸名条目下的配置 ctx.get_config 查不到（静默 fail-open）。
每次解析实时读配置（核心 load_config_readonly 本身按文件签名记忆化），改开关无需重启。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes.advanced_providers")

PLUGIN_NAME = "hermes-provider-advanced"
SETTING_KEY = "block-custom-openrouter-fallback"
_MARKER = "_hermes_advanced_providers_or_guard"
_LOCK = threading.Lock()
_CTX: Any = None
_BLOCKED_ONCE: set = set()


def configure(ctx: Any) -> None:
    """记住 PluginContext（register() 里调用），供设置读取走官方通道。"""
    global _CTX
    _CTX = ctx


# ------------------------------------------------------------------ settings

def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return value is True


def _entry_settings(entry: Any) -> dict:
    """entry 的 settings 子树；同时容忍直接平铺在 entry 下的旧写法。"""
    if not isinstance(entry, dict):
        return {}
    out: dict = {}
    settings = entry.get("settings")
    if isinstance(settings, dict):
        out.update(settings)
    for key, value in entry.items():
        if key != "settings":
            out.setdefault(key, value)
    return out


def _entry_candidates() -> list[str]:
    names: list[str] = []
    plugin_id = str(getattr(_CTX, "plugin_id", "") or "").strip()
    if plugin_id:
        names.append(plugin_id)
    for name in (PLUGIN_NAME, f"custom-plugins/{PLUGIN_NAME}"):
        if name not in names:
            names.append(name)
    return names


def setting_enabled() -> bool:
    """开关是否为真；读取失败一律 False（fail-open，保持核心原行为）。"""
    try:
        if _CTX is not None:
            try:
                value = _CTX.get_config(SETTING_KEY, default=None)
            except Exception:
                value = None
            if value is not None:
                return _truthy(value)
        from hermes_cli.config import load_config_readonly

        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
        for name in _entry_candidates():
            settings = _entry_settings(entries.get(name))
            if SETTING_KEY in settings:
                return _truthy(settings[SETTING_KEY])
    except Exception:
        logger.debug("advanced-providers: 兜底开关读取失败（按关闭处理）", exc_info=True)
    return False


# ------------------------------------------------------------------ decision

def _is_openrouter_host(url: str) -> bool:
    try:
        from utils import base_url_host_matches

        return bool(base_url_host_matches(url, "openrouter.ai"))
    except Exception:
        from urllib.parse import urlparse

        host = (urlparse(str(url or "")).hostname or "").lower()
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _configured_base_url() -> str:
    """config.yaml 里 model.base_url（显式镜像时予以放行）。"""
    try:
        from hermes_cli.runtime_provider import _get_model_config

        cfg = _get_model_config() or {}
        return str(cfg.get("base_url") or "").strip().rstrip("/")
    except Exception:
        return ""


def should_block(runtime: dict, requested_provider: str) -> tuple[bool, str]:
    """(是否阻断, 命中的 base_url)。

    放行：显式 ``openrouter`` 请求（用户自己要的），以及 base_url 等于 config.yaml
    ``model.base_url``（用户显式写死的镜像/代理）。其余落到 openrouter.ai 的解析一律阻断。
    """
    requested = str(requested_provider or "").strip().lower()
    if requested == "openrouter":
        return False, ""
    base_url = str((runtime or {}).get("base_url") or "").strip().rstrip("/")
    if not base_url or not _is_openrouter_host(base_url):
        return False, ""
    configured = _configured_base_url()
    if configured and configured == base_url:
        return False, ""
    return True, base_url


# ------------------------------------------------------------------ wrapper

def _block_message(requested_provider: str, base_url: str) -> str:
    return (
        "已阻断隐式 OpenRouter 兜底：请求 provider=%r 解析到了 %s，但 config.yaml 未显式配置该端点。"
        "开关 plugins.entries.%s.settings.%s=true 生效中。"
        "请在 model.provider 写具体提供商（或把该开关设为 false 恢复官方兜底行为）。"
        % (str(requested_provider or "auto"), base_url, PLUGIN_NAME, SETTING_KEY)
    )


def _wrap(original: Callable) -> Callable:
    def _resolve_openrouter_runtime(*args: Any, **kwargs: Any) -> dict:
        runtime = original(*args, **kwargs)
        try:
            if not setting_enabled():
                return runtime
            requested = kwargs.get("requested_provider")
            if requested is None and args:
                requested = args[0]
            blocked, base_url = should_block(runtime, str(requested or ""))
        except Exception:
            logger.debug("advanced-providers: 兜底判定异常（fail-open，保留核心行为）", exc_info=True)
            return runtime
        if not blocked:
            return runtime
        message = _block_message(str(requested or ""), base_url)
        try:
            from hermes_cli.auth import AuthError
        except Exception:  # 核心导入失败时退化为 RuntimeError，仍然拦住
            raise RuntimeError(message)
        key = (str(requested or ""), base_url)
        with _LOCK:
            first = key not in _BLOCKED_ONCE
            _BLOCKED_ONCE.add(key)
        if first:
            logger.warning("advanced-providers: %s", message)
        raise AuthError(message, provider=str(requested or "custom"),
                        code="openrouter_fallback_blocked")

    _resolve_openrouter_runtime.__name__ = getattr(original, "__name__", "_resolve_openrouter_runtime")
    _resolve_openrouter_runtime.__qualname__ = getattr(
        original, "__qualname__", _resolve_openrouter_runtime.__name__)
    _resolve_openrouter_runtime.__doc__ = (original.__doc__ or "") + (
        "\n[hermes-provider-advanced] block-custom-openrouter-fallback: 非显式 openrouter "
        "请求落到 OpenRouter 默认端点时抛 AuthError。")
    setattr(_resolve_openrouter_runtime, _MARKER, True)
    return _resolve_openrouter_runtime


def install() -> str:
    """把包装装到 ``hermes_cli.runtime_provider._resolve_openrouter_runtime``。

    返回 ``wrapped`` / ``already`` / ``missing`` / ``shape-changed``（与 transport_patches
    同语义，供 selfcheck 报告）。
    """
    import importlib

    try:
        module = importlib.import_module("hermes_cli.runtime_provider")
    except Exception as exc:
        logger.debug("advanced-providers: runtime_provider 不可用（%s）", exc)
        return "missing"
    attr = "_resolve_openrouter_runtime"
    original = getattr(module, attr, None)
    if original is None:
        return "missing"
    if getattr(original, _MARKER, False):
        return "already"
    if not callable(original) or getattr(original, "__name__", attr) != attr:
        return "shape-changed"
    try:
        setattr(module, attr, _wrap(original))
        return "wrapped"
    except Exception:
        logger.debug("advanced-providers: 兜底闸门安装失败", exc_info=True)
        return "missing"