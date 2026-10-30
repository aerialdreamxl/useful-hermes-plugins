"""Platform adapter: Qwen Portal accounts (``qwen://<email>``) — experimental.

Wire: the portal's OpenAI-compatible endpoint (api_mode
``openai-completions``, plain chat wire — no facade needed). Login is NOT
implemented (unknown device flow; the portal has no stable public
authorize endpoint we verified); users export tokens from a browser session
and import them. Refresh delegates to the bundled qwen-portal plugin's own
refresher when present.

Marked experimental: entries work only for imported accounts that still hold
valid tokens; expect re-import when they lapse.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..oauth_client import AccountNotFound, OAuthTokenContext

logger = logging.getLogger("hermes.advanced_providers")

NAME = "qwen"
ALIASES = ("qwen-oauth", "qwen-portal")
DELEGATE_API_MODE = "openai-completions"
INFERRED_API_MODE = "openai-completions"

API_BASE = "https://portal.qwen.ai/v1"

_CLIENT_CACHE: "dict[tuple[str, str, str], Any]" = {}


def normalize(imported: dict, platform_hint: str = "") -> Optional[dict]:
    if not isinstance(imported, dict):
        return None
    at = str(imported.get("access_token") or imported.get("accessToken") or "").strip()
    rt = str(imported.get("refresh_token") or imported.get("refreshToken") or "").strip()
    if not (at or rt):
        return None
    if not (platform_hint in ("qwen", "qwen-oauth", "qwen-portal")
            or "qwen.ai" in str(imported.get("base_url", ""))
            or str(imported.get("type", "")).lower() in ("qwen-oauth", "qwen")):
        return None
    return {
        "access_token": at,
        "refresh_token": rt,
        "email": str(imported.get("email") or ""),
        "expired": str(imported.get("expired") or imported.get("expires_at") or ""),
        "base_url": str(imported.get("base_url") or ""),
        "type": "qwen-oauth",
        "_imported_from": str(imported.get("_import_source") or "manual"),
    }


def login_interactive(account_name: str, store) -> Optional[dict]:
    logger.info("advanced-providers: qwen login flow not implemented (experimental platform) — "
                "import a token pair exported from the portal session instead.")
    return None


def resolve_inference_base_url(account: dict, entry: dict) -> str:
    return (str(account.get("base_url") or "").strip().rstrip("/")
            or str(entry.get("inference_base_url") or "").strip().rstrip("/")
            or API_BASE)


def list_models(ctx, account: dict, *, fingerprint: str = "hermes",
                proxy: str = "", timeout: float = 10.0) -> list[str]:
    """The account's OWN upstream ``<base>/models`` with its live bearer —
    a relay/gateway base_url stored on the account lists that gateway
    (CPA behavior), never the public vendor catalog."""
    from ..oauth_models import openai_style_models

    base = str(account.get("base_url") or "").strip().rstrip("/") or API_BASE
    return openai_style_models(ctx, account, fingerprint=fingerprint,
                               proxy=proxy, timeout=timeout, base=base)


def build_openai_client(entry: dict, account_name: str, store, *,
                        fingerprint: str, proxy: str, async_mode: bool = False) -> Any:
    """Plain OpenAI client over the live-bearer http hook (chat wire: no facade)."""
    import httpx
    from openai import OpenAI

    acct = store.get(NAME, account_name)
    if acct is None:
        raise AccountNotFound(f"qwen account '{account_name}' not found in the store")
    ctx = OAuthTokenContext(NAME, account_name, store)
    # store path in the key: a cached client's ctx carries its store binding
    key = ("sync", str(store.path), account_name, entry.get("model"), proxy, fingerprint)
    if not async_mode and key in _CLIENT_CACHE:
        cached = _CLIENT_CACHE[key]
        # a per-request close must not let the cache hand out a dead client
        try:
            dead = bool(cached.is_closed())
        except Exception:
            dead = False
        if not dead:
            return cached
        _CLIENT_CACHE.pop(key, None)

    from ..oauth_client import build_oauth_http_client

    http = build_oauth_http_client(ctx, platform=NAME, fingerprint=fingerprint,
                                   proxy=proxy, async_mode=async_mode)
    client = OpenAI(api_key="advanced-oauth-placeholder",
                    base_url=resolve_inference_base_url(acct, entry),
                    http_client=http, max_retries=0,
                    timeout=httpx.Timeout(float(entry.get("request_timeout") or 300.0)))
    if not async_mode:
        _CLIENT_CACHE[key] = client
    return client
