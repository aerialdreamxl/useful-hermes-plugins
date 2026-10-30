"""Platform adapter: OpenAI Codex accounts (``codex://<email>``).

Wire: Responses API against ``chatgpt.com/backend-api/codex`` (the official
app-server endpoint), via the core's OWN ``CodexTransport`` (P3 §2 R5 — no
re-implementation). Because core HTTP call sites branch on literal
``api_mode`` strings, the profile's ``create_client`` hands the chat-literal
call sites a facade (``OAuthResponsesClient``) that translates
``.chat.completions.create()`` onto the Responses wire — the same bridging
direction ``CodexAuxiliaryClient`` already proves in core.

Login: reuses the core's device-code flow verbatim (``_codex_device_code_login``)
— it is a pure print+return function with no pool side effects (the docstring
guarantees "never writes to ~/.codex/"), so the CLI can call it and route the
tokens into OUR store instead.

Single-use refresh rotation is the core refresher's discipline (ported via
oauth_tokens.refresh_and_persist).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..oauth_client import (AccountNotFound, OAuthResponsesClient,
                            OAuthTokenContext, build_oauth_http_client)

logger = logging.getLogger("hermes.advanced_providers")

NAME = "codex"
ALIASES = ("openai-codex",)
DELEGATE_API_MODE = "codex_responses"
INFERRED_API_MODE = "openai-completions"

# Official endpoint (recon: core auth_codex._codex_base_url at the time of
# writing). An account's stored ``base_url`` (from sub2api/CPA imports) wins.
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"

# The ChatGPT backend catalog endpoint HARD-REQUIRES ``?client_version=`` —
# without it the upstream answers 400 "missing query client_version" (probed
# live 2026-10-29). The value also gates the list: newer models carry
# ``minimal_client_version`` (gpt-6-sol/luna need 0.155.0), so an old version
# silently hides them (0.1.0 -> zero models). 0.285.0 was the version that
# returned the fullest list live; CPA's own fetcher defaults to 0.159.0.
CODEX_CLIENT_VERSION = "0.285.0"

# Curated fallback, mirrors CPA's shipped catalog
# (``internal/registry/models/codex_client_models.json`` in the CLIProxyAPI
# source), keeping only ``visibility: list`` entries in upstream priority
# order. Used solely when the official upstream listing fails — never for
# relay base_urls.
CURATED_MODELS = (
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6.1-terra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
)

_CLIENT_CACHE: "dict[tuple[str, str], Any]" = {}


def normalize(imported: dict, platform_hint: str = "") -> Optional[dict]:
    """Any recognized codex credential shape -> stored account dict (None if
    not codex). Keeps the official field set (id_token/access/refresh/
    account_id/email/expired) + provenance."""
    if not isinstance(imported, dict):
        return None
    at = str(imported.get("access_token") or imported.get("accessToken") or "").strip()
    rt = str(imported.get("refresh_token") or imported.get("refreshToken") or "").strip()
    idt = str(imported.get("id_token") or "").strip()
    if not idt and isinstance(imported.get("tokens"), dict):
        idt = str(imported["tokens"].get("id_token") or "").strip()
    if not (at or rt or idt):
        return None
    # Shape must look codex-ish: JWT access token OR chatgpt markers.
    blob = " ".join(str(k) for k in imported) + " " + at[:12]
    if not (idt or "chatgpt" in blob.lower() or "codex" in blob.lower()
            or platform_hint in ("codex", "openai-codex") or at.startswith("eyJ")):
        return None
    acct = {
        "access_token": at,
        "refresh_token": rt,
        "id_token": idt,
        "account_id": str(imported.get("account_id") or imported.get("chatgpt_account_id")
                          or imported.get("chatgptAccountID") or imported.get("openai_account_id") or ""),
        "email": str(imported.get("email") or ""),
        "expired": str(imported.get("expired") or imported.get("expires_at") or ""),
        "auth_mode": str(imported.get("auth_mode") or "chatgpt"),
        "base_url": str(imported.get("base_url") or ""),
        "type": str(imported.get("type") or "oauth"),
    }
    src = imported.get("_import_source")
    if src:
        acct["_imported_from"] = str(src)
    if acct.get("plan_type"):
        acct["plan_type"] = str(imported.get("plan_type"))
    return acct


def refresh(acct: dict, rt: str, proxy: str = "") -> dict:
    """Native Codex OAuth refresh with per-call proxy (offline audit P0).

    Same story as claude: the core's ``refresh_codex_oauth_pure`` builds its
    own httpx client and only ever sees the ambient environment — a token
    expiring on a proxy-only network dies on a direct connection. Request
    shape stays identical to the core's (form, auth.openai.com, client_id),
    so this is purely the transport fix. The core constants are reused when
    importable; the CPA-pinned fallbacks keep the refresher working if the
    core module moves.
    """
    import time

    import httpx

    from .. import oplog

    try:
        from hermes_cli.auth_constants import (CODEX_OAUTH_CLIENT_ID,
                                               CODEX_OAUTH_TOKEN_URL,
                                               CODEX_OAUTH_USER_AGENT)
    except Exception:
        CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
        CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
        CODEX_OAUTH_USER_AGENT = "hermes-cli/0.0.0"

    kw: dict = {"timeout": 20,
                "headers": {"Accept": "application/json",
                            "User-Agent": CODEX_OAUTH_USER_AGENT}}
    if proxy:
        kw["proxy"] = proxy
    oplog.proxy_injected(proxy, platform=NAME, scope="令牌刷新")
    with httpx.Client(**kw) as c:
        r = c.post(CODEX_OAUTH_TOKEN_URL,
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   data={"grant_type": "refresh_token", "refresh_token": rt,
                         "client_id": str(acct.get("client_id") or CODEX_OAUTH_CLIENT_ID)})
        if r.status_code != 200:
            raise ValueError(f"codex token refresh failed with status {r.status_code}: "
                             f"{(r.text or '')[:200]}")
        data = r.json() or {}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
    at = str(tokens.get("access_token") or "")
    upd = {"access_token": at, "refresh_token": str(tokens.get("refresh_token") or rt)}
    from ..oauth_tokens import _jwt_expiry

    exp = _jwt_expiry(at)
    if exp:
        upd["expired"] = str(int(exp))
    elif tokens.get("expires_in"):
        upd["expired"] = str(int(time.time()) + int(tokens["expires_in"]))
    if not at:
        raise ValueError("Codex refresh response missing access_token")
    return upd


def login_interactive(account_name: str, store) -> Optional[dict]:
    """Run the CORE's device-code login and persist into our store under
    *account_name*. Returns the stored account dict or None (failures print
    via the core flow itself)."""
    try:
        from hermes_cli.auth_codex import _codex_device_code_login

        result = _codex_device_code_login()
    except Exception as exc:  # AuthError etc. — core flow already made it actionable
        logger.error("advanced-providers: codex device login failed: %s", exc)
        return None
    tokens = (result or {}).get("tokens") or {}
    if not tokens.get("access_token") and not tokens.get("refresh_token"):
        return None
    from ..oauth_tokens import _jwt_expiry

    _exp = _jwt_expiry(tokens.get("access_token") or "")
    acct = normalize({**tokens, "base_url": DEFAULT_BASE_URL,
                      "type": "oauth", "expired": str(int(_exp)) if _exp else ""})
    if acct is None:
        return None
    email = acct.get("email") or ""
    if not email:
        try:
            import base64
            import json

            parts = str(tokens.get("access_token") or "").split(".")
            if len(parts) >= 2:
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload))
                email = str(((data.get("https://api.openai.com/auth") or {}).get("chatgpt_user_id"))
                            or data.get("email") or "")
                acct["account_id"] = acct["account_id"] or str(
                    ((data.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")) or "")
                if email:
                    acct["email"] = email
        except Exception:
            pass
    store.put(NAME, _slot(account_name, acct.get("email") or ""), acct)
    return acct


def _slot(account_name: str, email: str) -> str:
    """Never store under the CLI's ``__login__`` placeholder: two logins in
    that shape would collide on one slot."""
    from ..oauth_login import slot_name

    return slot_name(account_name, email, NAME)


def resolve_inference_base_url(account: dict, entry: dict) -> str:
    return (str(account.get("base_url") or "").strip().rstrip("/")
            or str(entry.get("inference_base_url") or "").strip().rstrip("/")
            or DEFAULT_BASE_URL)


def is_official_codex_base(base: str) -> bool:
    """True for the ChatGPT backend itself (its catalog needs the query)."""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(str(base or ""))
    except Exception:
        return False
    host = (parts.hostname or "").lower()
    return host.endswith("chatgpt.com") and "/backend-api/codex" in (parts.path or "")


def list_models(ctx, account: dict, *, fingerprint: str = "hermes",
                proxy: str = "", timeout: float = 10.0) -> list[str]:
    """The account's OWN upstream ``<base>/models`` with its live bearer —
    the relay/gateway base_url stored on the account lists that gateway
    (CPA behavior), never the public vendor catalog.

    Two branches, because the upstreams differ:
    - relay/gateway base (a stored base_url that is not chatgpt.com): plain
      ``<base>/models``, no query, no visibility concept — untouched.
    - official ChatGPT backend: ``<base>/models?client_version=...`` (the
      query is mandatory, 400 without it) + ``visibility == list`` filter;
      on total upstream failure fall back to the curated table so the
      picker never goes empty.
    """
    from ..oauth_models import openai_style_models

    base = str(account.get("base_url") or "").strip().rstrip("/") or DEFAULT_BASE_URL
    if not is_official_codex_base(base):
        return openai_style_models(ctx, account, fingerprint=fingerprint,
                                   proxy=proxy, timeout=timeout, base=base)
    models = openai_style_models(ctx, account, fingerprint=fingerprint,
                                 proxy=proxy, timeout=timeout, base=base,
                                 query={"client_version": CODEX_CLIENT_VERSION},
                                 exclude_hidden=True)
    if models:
        return models
    logger.info("advanced-providers: codex catalog unreachable — serving the "
                "curated fallback list (%d models)", len(CURATED_MODELS))
    return list(CURATED_MODELS)


def build_openai_client(entry: dict, account_name: str, store, *,
                        fingerprint: str, proxy: str, async_mode: bool = False) -> Any:
    """Materialize the OpenAI client for ``codex://<acct>``.

    Uses core ``provider_client`` so base_url/model params stay core-shaped;
    the http_client carries live Bearer + fingerprint per request; wrapped in
    the chat→Responses facade for the literal chat call sites.

    Cache key includes the STORE PATH: two configs bound to different stores
    (dev vs deployed HERMES_HOME, or tests) must never share a materialized
    client — its OAuthTokenContext carries the store binding.
    """
    key = ("sync", str(store.path), account_name, entry.get("model"), proxy, fingerprint)
    if not async_mode and key in _CLIENT_CACHE:
        cached = _CLIENT_CACHE[key]
        # The core closes per-request clients after every request; the cache
        # hands the SAME facade object to shared and request-scoped slots, so
        # a closed facade must be evicted, not handed out again (antigravity
        # live case 2026-10-29: retries died on "client has been closed").
        try:
            dead = bool(cached.is_closed())
        except Exception:
            dead = False
        if not dead:
            return cached
        _CLIENT_CACHE.pop(key, None)

    import httpx

    acct = store.get(NAME, account_name)
    if acct is None:
        raise AccountNotFound(f"codex account '{account_name}' not found in the store")
    ctx = OAuthTokenContext(NAME, account_name, store, proxy=proxy)

    # Construct against the real endpoint; a placeholder key (the SDK demands
    # one) is overwritten per-request by the oauth hook — never sent as-is.
    # max_retries=0 mirrors core _create_openai_client's own policy note:
    # Hermes owns retry/fallback; SDK retries would multiply wall time.
    base_url = resolve_inference_base_url(acct, entry)
    http = build_oauth_http_client(ctx, platform=NAME, fingerprint=fingerprint,
                                   proxy=proxy, async_mode=async_mode)
    from openai import OpenAI

    client = OpenAI(api_key="advanced-oauth-placeholder", base_url=base_url,
                    http_client=http, max_retries=0,
                    timeout=httpx.Timeout(float(entry.get("request_timeout") or 300.0)))
    facade = OAuthResponsesClient(client, ctx, NAME,
                                  force_stream=is_official_codex_base(base_url))
    if not async_mode:
        _CLIENT_CACHE[key] = facade
    return facade
