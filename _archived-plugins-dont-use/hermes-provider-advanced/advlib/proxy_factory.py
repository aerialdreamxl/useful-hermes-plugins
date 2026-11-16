"""Proxy httpx client factory (v2 spec §5.2).

Mirrors ``agent/process_bootstrap.py::build_keepalive_http_client``'s PROXY
branch semantics exactly, because those constants are load-bearing:

- ``timeout = Timeout(connect=15.0, read=None, write=15.0, pool=10.0)`` —
  ``read=None`` is what keeps SSE streaming alive; a finite read timeout
  reintroduces the CLOSE-WAIT/hang class of bug.
- ``limits = Limits(max_keepalive_connections=20, max_connections=100,
  keepalive_expiry=20.0)`` — reaps idle connections before a reverse proxy's
  30-60 s timeout drops them.
- A NEW ``httpx.Client`` per call: the core closes the client when the OpenAI
  SDK client is torn down (#10933 — a shared httpx.Client got reused after
  close). The core's proxy branch also builds per-client (no shared transport
  view), so we match that: one proxy client per SDK client, no cross-client
  transport sharing.
- ``verify`` mirrors the entry's own ``ssl_verify`` / ``ssl_ca_cert`` via the
  core resolver, so a provider's custom CA also applies through the proxy.

NO_PROXY handling (R3): reuse the core matcher ``agent.proxy_bypass.
should_bypass_proxy`` so LAN / fake-ip segments behave identically to the
env-proxy path.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("hermes.advanced_providers")

_TIMEOUT_CONNECT = 15.0
_TIMEOUT_WRITE = 15.0
_TIMEOUT_POOL = 10.0
_MAX_KEEPALIVE = 20
_MAX_CONNECTIONS = 100
_KEEPALIVE_EXPIRY = 20.0


def resolve_verify(entry: Any, base_url: str) -> Any:
    """``verify`` for the proxy client: the entry's TLS settings via core resolver.

    Falls back to True when the core resolver is unavailable (fail-open to
    secure default). Never raises.
    """
    ca = getattr(entry, "ssl_ca_cert", None)
    ssl_verify = getattr(entry, "ssl_verify", None)
    try:
        from agent.ssl_verify import resolve_httpx_verify

        return resolve_httpx_verify(
            ca_bundle=ca, ssl_verify=ssl_verify, base_url=str(base_url or "")
        )
    except Exception:
        logger.debug("advanced-providers: core verify resolver unavailable; verify=True", exc_info=True)
        return True


def proxy_decision(entry: Any, base_url: str) -> tuple[str, str]:
    """(proxy_url, reason) for *entry* targeting *base_url*.

    reason is one of:
    - ``"proxy"``   — use the returned proxy
    - ``"bypass"``  — configured proxy suppressed because NO_PROXY matches (R3)
    - ``"none"``    — entry declares no proxy
    Empty string proxy unless reason == "proxy". Never raises.
    """
    try:
        proxy = str(getattr(entry, "proxy", "") or "").strip()
        if not proxy:
            return "", "none"
        url = str(base_url or "").strip()
        if not url:
            return proxy, "proxy"
        if not url.startswith(("http://", "https://", "//")):
            url = f"//{url}"
        try:
            from agent.proxy_bypass import should_bypass_proxy

            if should_bypass_proxy(url):
                return "", "bypass"
        except Exception:
            logger.debug("advanced-providers: NO_PROXY matcher unavailable; using proxy", exc_info=True)
        return proxy, "proxy"
    except Exception:
        logger.debug("advanced-providers: proxy decision failed (fail-open)", exc_info=True)
        return "", "none"


def build_proxy_client(
    proxy: str,
    *,
    verify: Any = True,
    async_mode: bool = False,
) -> Optional[Any]:
    """An ``httpx.Client`` (or ``AsyncClient``) pinned to *proxy*, or None.

    Returns None on ANY failure so the caller falls through to the core's own
    keepalive client (fail-open: a broken proxy never silently becomes a
    direct connection — but it also must not take the session down; the
    core path keeps working, and ``status`` surfaces the probe failure).
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    try:
        import httpx

        limits = httpx.Limits(
            max_keepalive_connections=_MAX_KEEPALIVE,
            max_connections=_MAX_CONNECTIONS,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        )
        # read=None keeps SSE streams open (see module docstring).
        timeout = httpx.Timeout(
            connect=_TIMEOUT_CONNECT, read=None, write=_TIMEOUT_WRITE, pool=_TIMEOUT_POOL
        )
        client_cls = httpx.AsyncClient if async_mode else httpx.Client
        return client_cls(limits=limits, timeout=timeout, proxy=proxy, verify=verify)
    except Exception:
        logger.warning(
            "advanced-providers: failed to build proxy client for %s (falling back to core path)",
            _redact(proxy), exc_info=True,
        )
        return None


def probe_proxy(proxy: str, *, target: str = "", timeout: float = 5.0) -> tuple[bool, str]:
    """Connectivity probe for ``status`` (N3): can we reach the proxy at all?

    TCP-connects to the proxy host:port (not an end-to-end request, so it
    needs no credentials and no target reachability). Returns (ok, detail).
    Detail never contains credentials — only host:port and error class.
    """
    import socket
    from urllib.parse import urlsplit

    proxy = str(proxy or "").strip()
    if not proxy:
        return False, "no proxy configured"
    host, port = "", 0
    try:
        parsed = urlsplit(proxy if "://" in proxy else f"//{proxy}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if (parsed.scheme or "").lower() == "https" else 80)
        if not host:
            return False, "unparseable proxy URL"
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} reachable"
    except Exception as exc:
        where = f"{host}:{port}" if host else _redact(proxy)
        return False, f"{where} — {type(exc).__name__}: {exc}"


def _redact(value: str) -> str:
    """Strip any ``user:pass@`` from a URL for logging (N2)."""
    try:
        from urllib.parse import urlsplit, urlunsplit

        p = urlsplit(str(value or ""))
        if p.username or p.password:
            netloc = p.hostname or ""
            if p.port:
                netloc = f"{netloc}:{p.port}"
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
        return str(value or "")
    except Exception:
        return "<url>"
