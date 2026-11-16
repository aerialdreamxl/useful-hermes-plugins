"""Platform adapter: Google Antigravity accounts (``antigravity://<email>``)
— experimental / placeholder.

Wire: Antigravity's own endpoint shape (not plain OpenAI-compatible; CPA's
translator rewrites bodies for it). Real inference wiring lands in P3.5+
and only if demand exists — this module currently provides IMPORT and
refresh plumbing so accounts stop rotting mid-experiment, but
``build_*_client`` raises AdapterUnavailable with an honest pointer.

Refresh: standard Google OAuth (oauth2.googleapis.com/token) with the
PUBLIC desktop client of the official Antigravity app — the same constant
CPA's open-source tree ships (internal/auth/antigravity/constants.go); an
imported account carrying its own client_id/secret wins.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..oauth_client import AccountNotFound  # noqa: F401  (surface parity)

logger = logging.getLogger("hermes.advanced_providers")

NAME = "antigravity"
ALIASES = ("gemini-cli", "google-antigravity")
DELEGATE_API_MODE = ""          # own wire (cloudcode-pa v1internal), no delegate
INFERRED_API_MODE = "openai-completions"  # entry stays resolvable

# Facade cache keyed by (mode, store path, account, proxy, fingerprint) —
# same discipline as codex.py: a materialized client binds its OAuthTokenContext.
_CLIENT_CACHE: "dict[tuple[str, str], Any]" = {}

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
# Public client of the official desktop app (open-source everywhere; not a
# confidential secret). Assembled in one place for the whole package — see
# advlib/public_client.py for why it is not written as a contiguous literal.
from ..public_client import (GOOGLE_INSTALLED_CLIENT_ID,
                             GOOGLE_INSTALLED_CLIENT_SECRET)

DEFAULT_CLIENT_ID = GOOGLE_INSTALLED_CLIENT_ID
DEFAULT_CLIENT_SECRET = GOOGLE_INSTALLED_CLIENT_SECRET


def normalize(imported: dict, platform_hint: str = "") -> Optional[dict]:
    if not isinstance(imported, dict):
        return None
    at = str(imported.get("access_token") or imported.get("accessToken") or "").strip()
    rt = str(imported.get("refresh_token") or imported.get("refreshToken") or "").strip()
    if not (at or rt):
        return None
    if not (platform_hint in ("antigravity", "gemini-cli", "google")
            or str(imported.get("type", "")).lower() in ("antigravity", "google")
            or (at.startswith("ya29.") or rt.startswith("1//"))):
        return None
    return {
        "access_token": at,
        "refresh_token": rt,
        "email": str(imported.get("email") or ""),
        "expired": str(imported.get("expires_in") and "" or imported.get("expired") or ""),
        "token_type": str(imported.get("token_type") or "Bearer"),
        "client_id": str(imported.get("client_id") or DEFAULT_CLIENT_ID),
        "client_secret": str(imported.get("client_secret") or DEFAULT_CLIENT_SECRET),
        "type": "antigravity",
        "_imported_from": str(imported.get("_import_source") or "manual"),
    }


def refresh(acct: dict, rt: str, proxy: str = "") -> dict:
    """Google token-endpoint refresh (standard grant) — through the proxy.

    ``urllib.request`` has no per-call proxy and ignores ``advanced.proxy``,
    so the previous version went straight out and timed out on a proxied
    network (live case 2026-10-29: the desktop logged "refresh failed ...
    urlopen error timed out" every minute and the provider looked dead, which
    forced the agent's fallback). httpx takes the proxy per client.
    """
    import httpx

    form = {
        "client_id": acct.get("client_id") or DEFAULT_CLIENT_ID,
        "client_secret": acct.get("client_secret") or DEFAULT_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": rt,
    }
    kw: dict = {"timeout": 20}
    if proxy:
        kw["proxy"] = proxy
    from .. import oplog

    oplog.proxy_injected(proxy, platform="antigravity", scope="令牌刷新")
    with httpx.Client(**kw) as c:
        r = c.post(TOKEN_ENDPOINT, data=form,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise ValueError(f"google token refresh failed with status {r.status_code}: "
                             f"{(r.text or '')[:200]}")
        data = r.json() or {}
    upd = {"access_token": str(data.get("access_token") or "")}
    if data.get("refresh_token"):
        upd["refresh_token"] = str(data["refresh_token"])
    import time

    exp_in = data.get("expires_in")
    if exp_in:
        upd["expired"] = str(int(time.time()) + int(exp_in))
    if not upd["access_token"]:
        raise ValueError("Google refresh response missing access_token")
    return upd


def login_interactive(account_name: str, store, *, proxy: str = "") -> Optional[dict]:
    """Google OAuth login (port of CPA's antigravity auth.go).

    Authorize URL (5 scopes, access_type=offline, prompt=consent) -> loopback
    callback on :51121/oauth-callback -> form exchange -> userinfo email ->
    loadCodeAssist/onboardUser for the cloudaicompanionProject. Stores the raw
    account dict (project_id/tier_id/client pair included) — normalize() would
    drop those fields.
    """
    from ..oauth_login import LoginError, antigravity_login, slot_name

    try:
        acct = antigravity_login(proxy=proxy)
    except LoginError as exc:
        logger.error("advanced-providers: antigravity login failed: %s", exc)
        print(f"[login] {exc}")
        return None
    store.put(NAME, slot_name(account_name, acct.get("email") or "", NAME), acct)
    return acct


def build_openai_client(entry: dict, account_name: str, store, *,
                        fingerprint: str, proxy: str, async_mode: bool = False) -> Any:
    """Materialize the antigravity facade for ``antigravity://<acct>``.

    The facade speaks OpenAI chat.completions and translates to the cloudcode-pa
    v1internal wire (port of CPA's translator; see advlib/antigravity_infer.py).
    Cache key includes the STORE path (codex precedent): a materialized facade
    binds its OAuthTokenContext to one store file.
    """
    if async_mode:
        from . import AdapterUnavailable

        raise AdapterUnavailable("antigravity facade is sync-only for now")
    key = ("sync", str(store.path), account_name, proxy, fingerprint)
    cached = _CLIENT_CACHE.get(key)
    if cached is not None:
        closed = False
        try:
            closed = bool(cached.is_closed())
        except Exception:
            closed = False
        if not closed:
            return cached
        # The core closes per-request clients after every request; our cached
        # facade shares that identity, so a closed pool means "rebuild", not
        # "hand out a dead client" (live case 2026-10-29).
        _CLIENT_CACHE.pop(key, None)
    from ..antigravity_client import AntigravityFacade
    from ..oauth_client import OAuthTokenContext

    acct = store.get(NAME, account_name)
    if acct is None:
        raise AccountNotFound(f"antigravity account '{account_name}' not found in the store")
    ctx = OAuthTokenContext(NAME, account_name, store, proxy=proxy)
    timeout = float(entry.get("request_timeout") or 300.0)
    facade = AntigravityFacade(platform=NAME, account_name=account_name, store=store,
                               ctx=ctx, fingerprint=fingerprint, proxy=proxy,
                               timeout=timeout, entry=entry)
    _CLIENT_CACHE[key] = facade
    return facade


# ------------------------------------------------------------------ listing
#
# Account-scoped catalog, CPA-style: the credential's OWN upstream answers
# what that account may use — never a public vendor API.

DAILY_BASE = "https://daily-cloudcode-pa.googleapis.com"
PROD_BASE = "https://cloudcode-pa.googleapis.com"


def _candidate_bases(account: dict) -> list[str]:
    out: list[str] = []
    for v in (account.get("base_url"), account.get("antigravity_base_url")):
        s = str(v or "").strip().rstrip("/")
        if s and s not in out:
            out.append(s)
    for s in (DAILY_BASE, PROD_BASE):
        if s not in out:
            out.append(s)
    return out


def _harvest_models(data) -> list[str]:
    """Model ids from any loadCodeAssist-ish payload shape (CPA reads these
    loosely; so do we — entitlement lists drift)."""
    from ..oauth_models import extract_model_ids

    if not isinstance(data, dict):
        return extract_model_ids(data)

    def _ids(items):
        out: list[str] = []
        for item in items or []:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                # antigravity shape: {"key": {"modelId": ...}, "displayName": ...}
                nested = item.get("key")
                if isinstance(nested, dict):
                    v = nested.get("modelId") or nested.get("name")
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
                        continue
                for k in ("id", "slug", "name", "model", "modelId"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
                        break
        return out

    # NB: tier fields (allowedTiers/currentTier) are NOT harvested here —
    # their ids are tiers, not model slugs; _read_tiers owns them.
    for key in ("availableModels", "models", "cloudaicompanionModels"):
        v = data.get(key)
        if isinstance(v, list):
            vals = _ids(v)
            if vals:
                return vals
    # nested one level: {"tiers":[{"supportedModelIds":[...]}]} style
    for tier in [data.get("currentTier")] + list(data.get("availableTierIds") or []):
        if isinstance(tier, dict):
            for key in ("supportedModelIds", "modelIds", "models"):
                v = tier.get(key)
                if isinstance(v, list):
                    ids = _ids(v)
                    if ids:
                        return ids
    return []


def list_models(ctx, account: dict, *, fingerprint: str = "hermes",
                proxy: str = "", timeout: float = 10.0) -> list[str]:
    """Account-scoped catalog, exactly how CPA does it for antigravity.

    Google's cloudcode-pa upstream has NO dynamic /models endpoint (verified:
    ``listModels``/``listAvailableModels`` 404; ``loadCodeAssist`` returns only
    tier eligibility). CPA mirrors this: it calls ``loadCodeAssist`` to learn
    the ACCOUNT's tiers, then serves the model list from its own registry
    (models.json), filtered by what the tier allows. We do the same — the live
    bearer + ideType metadata (CPA auth.go:79-83) hits loadCodeAssist; the
    entitlement decides WHICH curated catalog we hand back. Never a public
    vendor catalog API, and never a guessed dynamic endpoint.
    """
    from ..oauth_models import _apply_fingerprint_static, http_post_json

    token = ctx.token()
    body = {"metadata": {"ideType": "ANTIGRAVITY"}}
    tiers: "list[str]" = []
    project = ""
    for base in _candidate_bases(account):
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        _apply_fingerprint_static(headers, platform=NAME, account=account,
                                  phase="inference", name=fingerprint,
                                  access_token=token)
        data = http_post_json(f"{base}/v1internal:loadCodeAssist", body=body,
                              headers=headers, proxy=proxy, timeout=timeout)
        if isinstance(data, dict):
            # forward-compat: if the response ever carries a real model list,
            # trust it over the curated tier table (harvest is a superset probe).
            direct = _harvest_models(data)
            if direct:
                return direct
            tiers = _read_tiers(data)
            project = str(_dig(data, "cloudaicompanionProject",
                                  "cloudaicompanionProjectId", "project") or "")
            if tiers or project:
                break
    # loadCodeAssist answered but the account is entitled to nothing usable
    # (all tiers ineligible): an empty catalog is the honest answer.
    if tiers == []:
        # An account with a project but no tier list still gets the standard
        # catalog — Google omits tiers for some onboarded personal accounts.
        if project:
            tiers = ["standard-tier"]
        else:
            return []
    catalog = antigravity_catalog(tiers, project=project)
    if catalog:
        # remember the project for the (future) inference wiring; harmless if
        # the account record already carries one.
        if project and not account.get("project_id"):
            account["project_id"] = project
    return catalog


def _read_tiers(data: dict) -> "list[str]":
    """Allowed tier ids from a loadCodeAssist response."""
    out: "list[str]" = []
    for tier in data.get("allowedTiers") or []:
        if isinstance(tier, dict):
            tid = str(tier.get("id") or tier.get("tierId") or "").strip()
            if tid:
                out.append(tid)
    return out


def _dig(data: dict, *keys: str):
    for k in keys:
        if k in data and data[k]:
            return data[k]
    return ""


# --- entitlement-filtered catalog (mirrors CPA's registry models.json
#     "antigravity" section, recon 2026-10-29; refresh by editing this table —
#     intentionally local, no runtime vendor catalog call, matching CPA).
#
# premium: the -high / opus / argon tier (needs a paid/default tier);
# base: what any onboarded antigravity personal account can reach.
_ANTI_PREMIUM = [
    "claude-opus-4-6-thinking", "claude-opus-5-5-high",
    "gemini-4-argon-high", "gemini-3.8-flash-high",
]
_ANTI_BASE = [
    "claude-sonnet-4-6", "claude-sonnet-5-5-high",
    "gemini-3-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
    "gemini-3.1-flash-image", "gpt-oss-120b-medium",
]
# A default/paid tier unlocks the premium set on top of the base.
_PAID_TIERS = {"standard-tier", "premium-tier", "enterprise-tier",
               "ultra-tier", "pro-tier"}


def antigravity_catalog(tiers: "list[str]", *, project: str = "") -> "list[str]":
    """Model ids this account's tiers unlock (CPA registry parity).

    Premium first when a paid/default tier is present, then base. A purely
    ineligible account still sees the base set — that is what the personal
    client exposes once onboarded (project id present).
    """
    paid = any(str(t) in _PAID_TIERS for t in tiers)
    out = ([m for m in _ANTI_PREMIUM] + _ANTI_BASE) if paid else list(_ANTI_BASE)
    seen: "set[str]" = set()
    return [m for m in out if not (m in seen or seen.add(m))]
