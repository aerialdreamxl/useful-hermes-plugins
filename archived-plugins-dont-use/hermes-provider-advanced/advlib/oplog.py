"""Operational log lines for the OAuth takeover path (中文可读).

Three facts decide almost every OAuth failure, and none of them were visible
in the log before this module:

1. was a proxy actually injected, or did the request go out direct?
2. which fingerprint implementation produced the identity (``hermes`` native,
   ``cpa`` port, ``sub2api`` port) — and did it fall back?
3. which server did the request actually reach?

Wording lives here, in one place, because four call sites report the same
facts (per-request hook, client build, token refresh, login flow). Callers pass
nothing but facts; levels are chosen here:

- proxy injected / client ready / token refreshed → INFO
- per-request takeover → INFO (one line per request: this is the line you grep
  when a provider "doesn't work")
- refresh failure stays at the caller's WARNING (it has the exception text)

Everything is fail-safe: a logging helper that raises would break a request
that was otherwise fine, so every formatter is wrapped.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("hermes.advanced_providers")

#: Human labels for the resolved fingerprint, matching how the ports are named
#: in the handover doc.
_SCHEME_LABELS = {
    "hermes": "hermes原生",
    "cpa": "CPA移植",
    "sub2api": "Sub2API移植",
}

_PROXY_LOGGED: "set[tuple]" = set()
_CLIENT_LOGGED: "set[tuple]" = set()


def scheme_label(name: str) -> str:
    key = str(name or "").strip().lower()
    return _SCHEME_LABELS.get(key, key or "未知")


def redact_proxy(value: str) -> str:
    """Proxy URL safe to log: ``http://u:p@host:7890`` → ``http://host:7890``."""
    raw = str(value or "")
    try:
        parts = urlsplit(raw)
        if parts.username or parts.password:
            netloc = parts.hostname or ""
            if parts.port:
                netloc = f"{netloc}:{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        pass
    return raw


def server_of(url: str) -> str:
    """``https://host/some/path?q`` → ``https://host`` (what the user reads)."""
    raw = str(url or "")
    try:
        parts = urlsplit(raw)
        if parts.scheme and parts.hostname:
            return f"{parts.scheme}://{parts.hostname}"
    except Exception:
        pass
    return raw or "未知服务器"


def proxy_injected(proxy: str, *, platform: str = "", account: str = "",
                   scope: str = "推理链路") -> None:
    """INFO once per (scope, platform, account, proxy) — proxy is a client-level
    fact, so repeating it on every request would bury the interesting lines."""
    shown = redact_proxy(proxy)
    if not shown:
        return
    key = (scope, platform, account, shown)
    if key in _PROXY_LOGGED:
        return
    _PROXY_LOGGED.add(key)
    try:
        logger.info("成功注入代理服务器 %s（作用域=%s 平台=%s 账号=%s）",
                    shown, scope, platform or "-", account or "-")
    except Exception:
        pass


def proxy_skipped(proxy: str, *, platform: str = "", account: str = "",
                  scope: str = "推理链路", reason: str = "") -> None:
    """WARNING once per key: a proxy was configured but the client build fell
    through to a direct connection — the failure mode that looks like "the
    provider is broken" while the network is fine."""
    if not str(proxy or "").strip():
        return
    key = (scope, platform, account, redact_proxy(proxy), "skipped")
    if key in _PROXY_LOGGED:
        return
    _PROXY_LOGGED.add(key)
    try:
        logger.warning("代理服务器 %s 未能注入，本次将直连（作用域=%s 平台=%s 账号=%s%s）",
                       redact_proxy(proxy), scope, platform or "-", account or "-",
                       f" 原因={reason}" if reason else "")
    except Exception:
        pass


def takeover(*, url: str, platform: str, account: str, phase: str,
             requested: str, effective: str, model: str = "", stream: Any = None,
             skipped: Optional[list] = None) -> None:
    """The one line that says who is sending what where.

    ``requested`` is the configured fingerprint, ``effective`` the one that
    actually supplied headers (they differ when the resolver fell back).
    """
    try:
        via = f"{requested}→{effective}" if requested != effective else effective
        fallback = ""
        if requested != effective:
            reason = ", ".join(skipped or []) or "无该平台身份"
            fallback = f"（{requested} 缺少该平台身份，回退原因：{reason}）"
        if stream is None and "streamgeneratecontent" in str(url or "").lower():
            # Gemini/antigravity signal streaming by endpoint, not by body.
            stream = True
        stream_txt = "-" if stream is None else ("是" if stream else "否")
        logger.info(
            "OAuth实现已接管请求，将使用「%s」实现发送到 %s | 平台=%s 账号=%s "
            "指纹=%s 模型=%s 流式=%s 阶段=%s%s",
            scheme_label(effective), server_of(url), platform or "-", account or "-",
            via, model or "-", stream_txt, phase or "-", fallback)
    except Exception:
        pass


def client_ready(*, platform: str, account: str, requested: str, effective: str,
                 proxy: str = "", timeout: Any = None) -> None:
    """INFO once per (platform, account, fingerprint, proxy) client build."""
    key = (platform, account, requested, effective, redact_proxy(proxy))
    if key in _CLIENT_LOGGED:
        return
    _CLIENT_LOGGED.add(key)
    try:
        via = f"{requested}→{effective}" if requested != effective else effective
        logger.info(
            "OAuth客户端就绪 | 平台=%s 账号=%s 指纹=%s 代理=%s 超时=%s",
            platform or "-", account or "-", via,
            redact_proxy(proxy) or "直连", timeout if timeout is not None else "-")
    except Exception:
        pass


def refresh_ok(*, platform: str, account: str, proxy: str = "") -> None:
    try:
        logger.info("OAuth令牌刷新成功（平台=%s 账号=%s%s）", platform or "-", account or "-",
                    f" 经代理={redact_proxy(proxy)}" if proxy else " 直连刷新")
    except Exception:
        pass
