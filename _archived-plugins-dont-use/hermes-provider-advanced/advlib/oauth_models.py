"""Account-scoped model catalog (v3 P3.6) — list what THIS account can serve.

Mirrors CPA's ``/v1/models`` semantics: the catalog is derived from the
loaded OAuth credential against the credential's own upstream, never a
public/vendor API. Each adapter declares how its upstream exposes the
account's model list:

- codex: GET ``<base>/models`` with the bearer + ChatGPT-Account-ID — the
  core's own catalog prober does exactly this for the ChatGPT backend
  (``fetch_codex_catalog_entries``); reused, so a relay/gateway base_url
  stored on the account lists THAT gateway (CPA behavior).
- claude: GET ``<base>/v1/models`` with the OAuth bearer + anthropic-version
  (the account's model entitlement).
- antigravity: POST ``<cloudcode-pa>/v1internal:loadCodeAssist`` (and the
  ``:listAvailableModels`` sibling) with the account bearer — the same
  account-scoped endpoints CPA's antigravity executor talks to; response
  ``availableModels[].modelNames`` is the entitlement list.

Auth is live (ctx.token() refreshes on skew and persists rotation) and the
fingerprint layer rides along (identity of the listing call = identity of
inference, matching how both relays behave). Results cache briefly so
picker-open storms don't hammer upstream; failures return None → the caller
falls through to whatever curated list exists (never an empty crash).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("hermes.advanced_providers")

_CACHE_TTL = 900.0  # 15 min: entitlement changes are rare; force_refresh bypasses
_CACHE: "dict[str, tuple[float, list[str]]]" = {}
_CACHE_LOCK = threading.Lock()

LISTING_TIMEOUT = 10.0


def list_models_for_ref(ref: str, *, force_refresh: bool = False) -> Optional[list[str]]:
    """Model ids the account behind ``platform://account`` is entitled to.

    None = not our ref / platform has no listing / upstream failed (caller
    degrades). Never raises.
    """
    try:
        from . import oauth_gate

        if oauth_gate.oauth_disabled():
            return None
        return _list(ref, force_refresh=force_refresh)
    except Exception as exc:
        logger.debug("advanced-providers: model listing failed for %r", ref, exc_info=True)
        return None


def _list(ref: str, *, force_refresh: bool) -> Optional[list[str]]:
    from .adapters import get as get_adapter
    from .config_scan import RouteTable, parse_oauth_ref_from_url

    platform, account_name = parse_oauth_ref_from_url(ref)
    if not platform:
        return None
    adapter = get_adapter(platform)
    if adapter is None:
        return None
    lister = getattr(adapter, "list_models", None)
    if lister is None:
        return None

    entry = RouteTable.from_config().lookup_oauth_ref(ref) or {}
    from .account_store import AccountStore
    from .oauth_client import OAuthTokenContext

    def _f(name: str, default=None):
        if isinstance(entry, dict):
            return entry.get(name, default)
        return getattr(entry, name, default)

    fingerprint = _f("fingerprint") or "hermes"
    proxy = _f("proxy") or ""
    key = f"{platform}::{account_name}::{fingerprint}::{proxy}"
    if not force_refresh:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and hit[0] > time.time():
                return hit[1]

    store = AccountStore()
    ctx = OAuthTokenContext(platform, account_name, store, proxy=proxy)
    account = ctx.peek_account()

    models = lister(ctx, account, fingerprint=fingerprint, proxy=proxy,
                    timeout=LISTING_TIMEOUT)
    models = _clean(models)
    if models:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time() + _CACHE_TTL, models)
        logger.info("advanced-providers: %s://%s — %d model(s) from upstream catalog",
                    platform, account_name, len(models))
        return models
    return None


def _clean(models: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in models or []:
        s = str(m or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ------------------------------------------------------------- wire helpers

def http_get_json(url: str, *, headers: dict, proxy: str = "", timeout: float = 10.0) -> Any:
    """GET a JSON doc with the account identity via the oauth http hook.

    ``headers`` are baseline (auth/fingerprint still decide per request —
    ctx hook rewrites Authorization last). Returns parsed JSON or None.
    """
    import httpx

    kw: dict[str, Any] = {"timeout": timeout}
    if proxy:
        kw["proxy"] = proxy
    from . import oplog

    oplog.proxy_injected(proxy, scope="模型目录GET")
    try:
        with httpx.Client(headers=headers, follow_redirects=True, **kw) as c:
            r = c.get(url)
            if r.status_code != 200:
                logger.debug("advanced-providers: listing GET %s -> %s", url, r.status_code)
                return None
            return r.json()
    except Exception as exc:
        logger.debug("advanced-providers: listing GET %s failed: %s", url, exc)
        return None


def http_post_json(url: str, *, body: Any, headers: dict, proxy: str = "",
                   timeout: float = 10.0) -> Any:
    import httpx

    kw: dict[str, Any] = {"timeout": timeout}
    if proxy:
        kw["proxy"] = proxy
    from . import oplog

    oplog.proxy_injected(proxy, scope="模型目录POST")
    try:
        with httpx.Client(headers=headers, follow_redirects=True, **kw) as c:
            r = c.post(url, json=body)
            if r.status_code != 200:
                logger.debug("advanced-providers: listing POST %s -> %s", url, r.status_code)
                return None
            return r.json()
    except Exception as exc:
        logger.debug("advanced-providers: listing POST %s failed: %s", url, exc)
        return None


def openai_style_models(ctx: OAuthCtxT, account: dict, *, fingerprint: str,
                        proxy: str, timeout: float, base: str,
                        path: str = "/models",
                        extra_headers: "dict | None" = None,
                        query: "dict | None" = None,
                        exclude_hidden: bool = False) -> list[str]:
    """Standard ``GET <base><path>`` (OpenAI + Anthropic shapes accepted).

    Auth header is pre-set from the LIVE token (the shared hook would too —
    both apply, hook wins); fingerprint identity layers on it. Anthropic
    surfaces pass ``path="/v1/models"`` plus the version header.

    ``query`` appends URL query params (codex's catalog endpoint REQUIRES
    ``client_version`` — omitting it is a hard 400, so the param lives here
    instead of polluting ``path``). ``exclude_hidden`` drops entries whose
    ``visibility`` is not ``list`` (codex serves reserve/auto-review models
    the picker must never show).
    """
    token = ctx.token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    headers.update(extra_headers or {})
    _apply_fingerprint_static(headers, platform=ctx.platform, account=account,
                              phase="inference", name=fingerprint, access_token=token)
    url = base.rstrip("/") + path
    if query:
        from urllib.parse import urlencode

        clean = {k: v for k, v in query.items() if v not in (None, "")}
        if clean:
            url += ("&" if "?" in url else "?") + urlencode(clean)
    data = http_get_json(url, headers=headers, proxy=proxy, timeout=timeout)
    return extract_model_ids(data, exclude_hidden=exclude_hidden)


def extract_model_ids(data: Any, *, exclude_hidden: bool = False) -> list[str]:
    """Id lists from the shapes upstreams actually serve:
    ``{"data":[{id}...]}`` / ``{"models":[{slug|id|name}...]}`` / bare list.

    ``exclude_hidden`` skips entries carrying ``visibility`` other than
    ``"list"`` (codex: ``gpt-reserve`` / ``codex-auto-review`` ship as
    ``hide`` and are not selectable by the account).
    """
    if data is None:
        return []
    items: Any = None
    if isinstance(data, dict):
        for k in ("data", "models", "availableModels"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    elif isinstance(data, list):
        items = data
    out: list[str] = []
    for item in items or []:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            if exclude_hidden:
                vis = item.get("visibility")
                if isinstance(vis, str) and vis.strip().lower() not in ("", "list"):
                    continue
            for k in ("id", "slug", "name", "model", "modelId"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
    return out


def _apply_fingerprint_static(headers: dict, *, platform: str, account: dict,
                              phase: str, name: str, access_token: str) -> None:
    """Fingerprint onto a plain dict header set (mirrors httpx.Request apply)."""
    try:
        from . import fingerprints

        req = _RequestWithHeaders(headers)
        fingerprints.apply(req, platform=platform, account=account, phase=phase,
                           name=name, access_token=access_token)
    except Exception:
        pass


class _RequestWithHeaders:
    """Minimal httpx.Request stand-in so fingerprints.apply() works on a dict."""

    def __init__(self, headers: dict) -> None:
        self.headers = _HeaderDict(headers)


class _HeaderDict:
    """Case-insensitive dict PROXY (mutates the underlying dict in place)."""

    def __init__(self, d: dict) -> None:
        self._d = d

    def get(self, k, default=None):
        if k in self._d:
            return self._d[k]
        lk = str(k).lower()
        for key in self._d:
            if str(key).lower() == lk:
                return self._d[key]
        return default

    def __contains__(self, k) -> bool:
        if k in self._d:
            return True
        lk = str(k).lower()
        return any(str(key).lower() == lk for key in self._d)

    def __setitem__(self, k, v):
        lk = str(k).lower()
        for key in list(self._d):
            if str(key).lower() == lk:
                del self._d[key]
                break
        self._d[k] = v

    def __delitem__(self, k):
        lk = str(k).lower()
        for key in list(self._d):
            if str(key).lower() == lk:
                del self._d[key]
                return


# Type alias for editor clarity only (avoid circular import at runtime).
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .oauth_client import OAuthTokenContext as OAuthCtxT
else:
    OAuthCtxT = Any
