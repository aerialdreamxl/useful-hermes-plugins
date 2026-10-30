"""Platform adapter: Anthropic Claude accounts (``claude://<email>``).

Wire: Anthropic Messages API at ``api.anthropic.com`` via the core's own
``build_anthropic_client`` — which natively supports a CALLABLE api_key
(Entra bearer-hook pattern, anthropic_adapter.py:400) and stamps the Claude
Code OAuth identity headers on oauth routes itself. The adapter therefore
only supplies: normalized account shape, the live token provider, and the
base_url. api_mode stays ``anthropic_messages`` (no facade needed — the
client is genuinely anthropic-shaped).

Import note: the subscription OAuth path uses ``sk-ant-oat01-`` access
tokens (core's `_is_oauth_token` predicate recognizes the family); refresh
is single-use rotation with peer-adoption, owned by oauth_tokens.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..oauth_client import (AccountNotFound, OAuthTokenContext,
                            fingerprint_effective)

logger = logging.getLogger("hermes.advanced_providers")

NAME = "claude"
ALIASES = ("anthropic", "claude-oauth")
DELEGATE_API_MODE = "anthropic_messages"
INFERRED_API_MODE = "anthropic_messages"

API_BASE = "https://api.anthropic.com"

_CLIENT_CACHE: "dict[tuple[str, str, str], Any]" = {}


def normalize(imported: dict, platform_hint: str = "") -> Optional[dict]:
    if not isinstance(imported, dict):
        return None
    at = str(imported.get("access_token") or imported.get("accessToken") or "").strip()
    rt = str(imported.get("refresh_token") or imported.get("refreshToken") or "").strip()
    if not (at or rt):
        return None
    looks = at.startswith("sk-ant-") or "claude" in str(imported.get("type", "")).lower() \
        or "anthropic" in platform_hint or "claude" in platform_hint \
        or imported.get("account_uuid") is not None or imported.get("organization_uuid") is not None
    if not looks:
        return None
    acct = {
        "access_token": at,
        "refresh_token": rt,
        "email": str(imported.get("email") or ""),
        "account_uuid": str(imported.get("account_uuid") or imported.get("accountUUID") or ""),
        "organization_uuid": str(imported.get("organization_uuid") or ""),
        "organization_name": str(imported.get("organization_name") or ""),
        "expired": str(imported.get("expired") or imported.get("expiresAt") or ""),
        "type": str(imported.get("type") or "oauth-token"),
    }
    ids = imported.get("claude_device_ids")
    if ids:
        acct["device_ids"] = ids
    src = imported.get("_import_source")
    if src:
        acct["_imported_from"] = str(src)
    return acct


def login_interactive(account_name: str, store, *, proxy: str = "") -> Optional[dict]:
    """Claude Code OAuth login (port of CPA's flow; see advlib/oauth_login.py).

    Prints the authorize URL, captures the loopback callback on :54545, then
    exchanges it JSON-encoded in the native key order and stores the credential
    (access/refresh, profile identity, one 32-byte device id).
    """
    from ..oauth_login import LoginError, claude_login, slot_name

    try:
        raw = claude_login(proxy=proxy)
    except LoginError as exc:
        logger.error("advanced-providers: claude login failed: %s", exc)
        print(f"[login] {exc}")
        return None
    acct = normalize(raw) or raw
    acct["device_ids"] = raw.get("device_ids") or []
    store.put(NAME, slot_name(account_name, acct.get("email") or "", NAME), acct)
    return acct


def refresh(acct: dict, rt: str, proxy: str = "") -> dict:
    """Native Claude OAuth refresh — CPA's exact shape, ported (offline audit P0).

    The core's ``refresh_anthropic_oauth_pure`` posts a FORM to
    ``api.anthropic.com/v1/oauth/tokens``. Claude Code 2.1.220+ and CPA both
    send a JSON body to ``platform.claude.com/v1/oauth/token`` with the login's
    axios headers. On a single-use grant the wrong endpoint/content-type is how
    a refresh token gets flagged and the whole grant revoked — so we do NOT use
    the core refresher here. This path also takes ``proxy=`` (the core one
    cannot), which is what keeps a token expiring on a proxy-only network from
    bricking the provider mid-session.
    """
    import time

    import httpx

    from .. import oplog
    from ..oauth_login import (CLAUDE_AXIOS_HEADERS, CLAUDE_CLIENT_ID,
                               CLAUDE_SCOPE, CLAUDE_TOKEN_URL)

    headers = dict(CLAUDE_AXIOS_HEADERS)
    headers["Content-Type"] = "application/json"
    body = {
        "client_id": str(acct.get("client_id") or CLAUDE_CLIENT_ID),
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "scope": str(acct.get("scope") or CLAUDE_SCOPE),
    }
    kw: dict = {"timeout": 20}
    if proxy:
        kw["proxy"] = proxy
    oplog.proxy_injected(proxy, platform=NAME, scope="令牌刷新")
    with httpx.Client(**kw) as c:
        r = c.post(CLAUDE_TOKEN_URL, content=json.dumps(body).encode(), headers=headers)
        if r.status_code != 200:
            raise ValueError(f"claude token refresh failed with status {r.status_code}: "
                             f"{(r.text or '')[:200]}")
        data = r.json() or {}
    upd = {"access_token": str(data.get("access_token") or "")}
    if data.get("refresh_token"):
        upd["refresh_token"] = str(data["refresh_token"])
    # Anthropic returns expires_in (seconds); store the absolute expiry like
    # the antigravity refresher so oauth_tokens' expiry check reads one shape.
    exp_in = data.get("expires_in")
    if exp_in:
        upd["expired"] = str(int(time.time()) + int(exp_in))
    elif data.get("expires_at"):  # some responses carry an absolute ms stamp
        try:
            upd["expired"] = str(int(int(data["expires_at"]) / 1000))
        except (TypeError, ValueError):
            pass
    if not upd["access_token"]:
        raise ValueError("Claude refresh response missing access_token")
    return upd


def resolve_inference_base_url(account: dict, entry: dict) -> str:
    return (str(account.get("base_url") or "").strip().rstrip("/")
            or str(entry.get("inference_base_url") or "").strip().rstrip("/")
            or API_BASE)


def build_anthropic_client(entry: dict, account_name: str, store, *,
                           fingerprint: str, proxy: str) -> Any:
    """Live-bearer anthropic client via the CORE's callable-api_key route.

    ``build_anthropic_client(callable)`` → bearer http-hook path; the core
    adds the Claude Code identity headers itself when the route qualifies as
    oauth. The fingerprint layer sits on top of the resulting transport for
    non-hermes profiles (sub2api/cpa override the UA identity).
    """
    acct = store.get(NAME, account_name)
    if acct is None:
        raise AccountNotFound(f"claude account '{account_name}' not found in the store")
    ctx = OAuthTokenContext(NAME, account_name, store, proxy=proxy)
    # store path in the key: a cached client's ctx carries its store binding
    key = (str(store.path), account_name, entry.get("model"), proxy, fingerprint)
    if key in _CLIENT_CACHE:
        cached = _CLIENT_CACHE[key]
        # a per-request close must not let the cache hand out a dead client
        try:
            dead = bool(cached.is_closed())
        except Exception:
            dead = False
        if not dead:
            return cached
        _CLIENT_CACHE.pop(key, None)

    from agent.anthropic_adapter import build_anthropic_client as _core_build

    base_url = resolve_inference_base_url(acct, entry)
    client = _core_build(lambda: ctx.token(), base_url=base_url,
                         timeout=float(entry.get("request_timeout") or 900.0))

    # Attached for EVERY profile: "hermes" is not a no-op here, it resolves
    # through the fingerprint chain (native core identity when its version is
    # current, CPA when it is not) and it is the hook that produces the
    # per-request takeover log line.
    _attach_fingerprint(client, ctx, fingerprint or "hermes", proxy=proxy)
    _CLIENT_CACHE[key] = client
    return client


def _attach_fingerprint(client: Any, ctx: OAuthTokenContext, fingerprint: str,
                        *, proxy: str = "") -> None:
    """Layer the resolved fingerprint onto the core-built client's transport.

    The core bearer hook owns Authorization; we append a second request hook
    that applies the impersonation profile AFTER it (hooks run in order), so
    the profile can rewrite identity headers on the final request.
    """
    from .. import oplog

    if proxy:
        # Core's anthropic client owns its transport and takes no proxy
        # argument; a configured proxy silently would not be used here.
        oplog.proxy_skipped(proxy, platform=NAME, scope="推理链路",
                            reason="核心 anthropic 客户端不接受 proxy 参数")
    try:
        transport = client._client  # anthropic SDK -> httpx.Client
        transport.event_hooks["request"].append(
            _make_hook(ctx, fingerprint)
        )
        oplog.client_ready(platform=NAME, account=ctx.account_name,
                           requested=fingerprint,
                           effective=fingerprint_effective(NAME, fingerprint),
                           proxy=proxy)
    except Exception:
        logger.warning("advanced-providers: claude fingerprint '%s' could not attach to "
                       "the core transport — the request keeps the hermes identity",
                       fingerprint)


def _make_hook(ctx: OAuthTokenContext, fingerprint: str):
    from .. import fingerprints
    from .. import oplog

    def _hook(request: Any) -> None:
        token = ""
        try:
            token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        except Exception:
            pass
        effective, skipped = fingerprints.apply(
            request, platform=NAME, account=ctx.peek_account(),
            phase="inference", name=fingerprint, access_token=token)
        body = _peek_body(request)
        oplog.takeover(url=str(request.url), platform=NAME,
                       account=ctx.account_name, phase="inference",
                       requested=str(fingerprint or "hermes").strip().lower(),
                       effective=effective or str(fingerprint or "hermes"),
                       model=str(body.get("model") or ""),
                       stream=body.get("stream"), skipped=skipped)
    return _hook


def _peek_body(request: Any) -> dict:
    import json

    try:
        raw = request.content
        if not raw or len(raw) > 4 * 1024 * 1024:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_models(ctx, account: dict, *, fingerprint: str = "hermes",
                proxy: str = "", timeout: float = 10.0) -> list[str]:
    """The account's OWN upstream ``<base>/v1/models`` with its live OAuth
    bearer + anthropic-version (the Claude Code entitlement list) — a
    relay/gateway base_url stored on the account lists that gateway
    (CPA behavior), never the public vendor catalog."""
    from ..oauth_models import openai_style_models

    base = str(account.get("base_url") or "").strip().rstrip("/") or API_BASE
    return openai_style_models(ctx, account, fingerprint=fingerprint,
                               proxy=proxy, timeout=timeout, base=base,
                               path="/v1/models",
                               extra_headers={"anthropic-version": "2023-06-01"})
