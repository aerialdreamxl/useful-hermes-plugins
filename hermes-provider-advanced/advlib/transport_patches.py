"""P2 transport patches — anthropic wire + auxiliary chain (v2 spec §5.2/§6).

Two core chokepoints get identity-checked wrappers, installed once from the
agent-side half's ``register(ctx)``:

1. ``agent.anthropic_adapter._new_sdk_client`` — the SINGLE funnel every
   Anthropic-SDK client passes through (static-key path line ~520 and the
   Entra bearer-hook path line ~431; no other module imports it). We
   ``setdefault`` an ``http_client`` (per-provider proxy) into ``kwargs``
   before the SDK constructor. The bearer path already sets its own
   http_client — setdefault respects it (core semantics win).

2. ``agent.auxiliary_client._openai_http_client_kwargs`` — builds the aux
   chain's ``{"http_client": ...}`` (compression/title/vision… paths that
   never see the main-loop profile hook). We return OUR proxied client when
   the aux base_url routes to a proxy-declaring entry, else the original's.

Wrapping rules (per the agent-side plugin contracts):
- identity-check: only swap when the attribute is the expected callable.
- patch EVERY consumer binding: verified both call sites resolve the module
  global at call time, so one attribute swap covers all.
- fail-open: any error inside our logic → delegate to the original.
- idempotent: a second install() is a no-op (marker attribute).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("hermes.advanced_providers")

_LOCK = threading.Lock()
_MARKER = "_hermes_advanced_providers_wrapped"


def _route_http_client(base_url: str, *, async_mode: bool = False) -> Any | None:
    """Proxy httpx client for *base_url* when it routes to a proxy-declaring
    api-mode entry, else None. Same decision ladder as the P1 profile hook."""
    from .config_scan import RouteTable
    from .proxy_factory import build_proxy_client, proxy_decision, resolve_verify

    base_url = str(base_url or "").strip()
    if not base_url:
        return None
    table = RouteTable.from_config()
    entry = table.lookup(base_url)
    if entry is None or entry.auth_mode != "api":
        return None
    proxy, reason = proxy_decision(entry, base_url)
    if reason != "proxy" or not proxy:
        if reason == "bypass":
            entry.note_bypass_once(base_url)
        return None
    client = build_proxy_client(proxy, verify=resolve_verify(entry, base_url), async_mode=async_mode)
    if client is not None:
        entry.note_proxy_once(base_url, proxy)
    return client


# ---------------------------------------------------------------- anthropic

def _wrap_new_sdk_client(original: Callable) -> Callable:
    def _new_sdk_client(sdk, kwargs: dict, headers: dict, route: str | None = None) -> Any:
        try:
            if "http_client" not in kwargs:
                target = str(route or (kwargs or {}).get("base_url") or "")
                client = _route_http_client(target)
                if client is not None:
                    kwargs = {**kwargs, "http_client": client}
        except Exception:
            logger.debug("advanced-providers: anthropic http_client injection failed (fail-open)",
                         exc_info=True)
        return original(sdk, kwargs, headers, route=route)

    _new_sdk_client.__name__ = original.__name__
    _new_sdk_client.__qualname__ = getattr(original, "__qualname__", original.__name__)
    _new_sdk_client.__doc__ = (original.__doc__ or "") + \
        "\n[hermes-provider-advanced] per-provider proxy http_client injected via route table."
    setattr(_new_sdk_client, _MARKER, True)
    return _new_sdk_client


# ---------------------------------------------------------------- auxiliary

def _wrap_aux_http_kwargs(original: Callable) -> Callable:
    def _openai_http_client_kwargs(base_url, *, async_mode: bool = False) -> dict:
        try:
            client = _route_http_client(str(base_url or ""), async_mode=async_mode)
            if client is not None:
                return {"http_client": client}
        except Exception:
            logger.debug("advanced-providers: aux http_client injection failed (fail-open)",
                         exc_info=True)
        return original(base_url, async_mode=async_mode)

    _openai_http_client_kwargs.__name__ = original.__name__
    _openai_http_client_kwargs.__qualname__ = getattr(original, "__qualname__", original.__name__)
    setattr(_openai_http_client_kwargs, _MARKER, True)
    return _openai_http_client_kwargs


# ---------------------------------------------------------------- catalog (P3.6)

def _oauth_ref_models(url: str, *, force: bool):
    """advanced_oauth model list for a scheme reference URL, or None.

    None for every non-ref URL (caller proceeds untouched) and on any
    internal failure (fail-open) — a catalog probe must never crash a
    picker, it degrades to the curated/static path.
    """
    try:
        from . import oauth_models

        return oauth_models.list_models_for_ref(url, force_refresh=force)
    except Exception:
        logger.debug("advanced-providers: catalog seam failed for %r (fail-open)", url,
                     exc_info=True)
        return None


def _wrap_profile_live_catalog(original: Callable) -> Callable:
    """``models._profile_live_catalog``: intercept named custom providers whose
    route table entry is an advanced_oauth reference. The core's generic path
    reads the PROFILE's base_url (empty for custom) and the pool credentials
    (empty for oauth) — so a scheme://account entry returns None and the
    picker shows nothing. We ask the ACCOUNT's own upstream instead (CPA
    semantics), never the public vendor catalog.
    """
    def _profile_live_catalog(normalized):
        try:
            from .config_scan import RouteTable

            entry = RouteTable.from_config().by_oauth_key.get(
                str(normalized or "").strip().lower())
            if entry is not None and entry.base_url:
                models = _oauth_ref_models(entry.base_url, force=False)
                if models:
                    return models
        except Exception:
            logger.debug("advanced-providers: profile_live_catalog seam failed",
                         exc_info=True)
        return original(normalized)

    _profile_live_catalog.__name__ = original.__name__
    _profile_live_catalog.__qualname__ = getattr(original, "__qualname__", original.__name__)
    setattr(_profile_live_catalog, _MARKER, True)
    return _profile_live_catalog


def _wrap_discover_endpoint_models(original: Callable) -> Callable:
    """``model_switch_providers._discover_endpoint_models``: the picker's
    section-4 endpoint row passes the entry's base_url straight here; for an
    advanced_oauth scheme ref the core would hand ``antigravity://…`` to
    urllib and get a scheme error -> empty row. Route it to the account
    catalog instead. Signature: (api_key, api_url, native_provider,
    has_explicit_models, *, headers, api_mode, probe_live, discovery_allowed,
    fast_custom_probe) -> (models, native_catalog_empty)."""
    def _discover_endpoint_models(api_key, api_url, *rest, **kw):
        try:
            from . import is_oauth_scheme_ref

            if is_oauth_scheme_ref(str(api_url or "")):
                # probe_live (Refresh / current row) bypasses the catalog
                # cache; a cache_only read of a non-current row still
                # fetches on a cold cache, so the account list appears the
                # first time without re-probing every picker open.
                models = _oauth_ref_models(str(api_url), force=bool(kw.get("probe_live")))
                if models is not None:
                    return models, False
        except Exception:
            logger.debug("advanced-providers: discover_endpoint_models seam failed",
                         exc_info=True)
        return original(api_key, api_url, *rest, **kw)

    _discover_endpoint_models.__name__ = original.__name__
    _discover_endpoint_models.__qualname__ = getattr(
        original, "__qualname__", original.__name__)
    setattr(_discover_endpoint_models, _MARKER, True)
    return _discover_endpoint_models


def _wrap_get_provider_profile(original: Callable) -> Callable:
    """Fallback 'advanced:<id>' to 'advanced' profile (identical to core 'custom:<id>')."""
    def _get_provider_profile(name: str):
        if isinstance(name, str) and name.lower().startswith("advanced:"):
            prof = original(name)
            if prof is not None:
                return prof
            return original("advanced")
        return original(name)

    _get_provider_profile.__name__ = original.__name__
    _get_provider_profile.__qualname__ = getattr(original, "__qualname__", original.__name__)
    setattr(_get_provider_profile, _MARKER, True)
    return _get_provider_profile


# ---------------------------------------------------------------- install

def install() -> dict[str, str]:
    """Install both wrappers. Returns {site: status} for selfcheck/reporting.

    Statuses: ``wrapped`` / ``already`` / ``missing`` (module/attr not found —
    core drifted or partial install) / ``shape-changed`` (attribute exists but
    is not the callable we expect).
    """
    results: dict[str, str] = {}
    with _LOCK:
        results["anthropic"] = _install_one(
            "agent.anthropic_adapter", "_new_sdk_client", _wrap_new_sdk_client,
            expect_name="_new_sdk_client")
        results["auxiliary"] = _install_one(
            "agent.auxiliary_client", "_openai_http_client_kwargs", _wrap_aux_http_kwargs,
            expect_name="_openai_http_client_kwargs")
        results["catalog_profile"] = _install_one(
            "hermes_cli.models", "_profile_live_catalog", _wrap_profile_live_catalog,
            expect_name="_profile_live_catalog")
        results["catalog_picker"] = _install_one(
            "hermes_cli.model_switch_providers", "_discover_endpoint_models",
            _wrap_discover_endpoint_models, expect_name="_discover_endpoint_models")
        # advanced: prefix routing fallback: mirrors core custom: fallback
        results["advanced_routing"] = _install_one(
            "providers", "get_provider_profile", _wrap_get_provider_profile,
            expect_name="get_provider_profile")

        # OpenRouter 隐式兜底闸门（block-custom-openrouter-fallback）：包装的是
        # runtime_provider 的模块级名字——阶梯末级用该全局名调用，一处替换即覆盖
        # resolve_runtime_provider / resolve_runtime_with_fallback 全部调用点。
        try:
            from . import fallback_guard

            results["openrouter_fallback"] = fallback_guard.install()
        except Exception:
            logger.debug("advanced-providers: fallback guard install failed", exc_info=True)
            results["openrouter_fallback"] = "missing"
    for site, status in results.items():
        if status in ("missing", "shape-changed"):
            logger.warning(
                "advanced-providers P2: %s seam %s — that transport keeps core behaviour "
                "(no per-provider proxy there). Run `hermes advanced-providers selfcheck`.",
                site, status)
        else:
            logger.info("advanced-providers P2: %s seam %s", site, status)
    return results


def _install_one(module_name: str, attr: str, wrapper: Callable, *, expect_name: str) -> str:
    import importlib
    import inspect

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        logger.debug("advanced-providers P2: %s unavailable (%s)", module_name, exc)
        return "missing"
    original = getattr(module, attr, None)
    if original is None:
        return "missing"
    if getattr(original, _MARKER, False):
        return "already"
    if not callable(original) or getattr(original, "__name__", attr) != expect_name:
        # identity-check: attribute exists but is not the function we mapped —
        # core drifted; refuse to wrap a stranger.
        return "shape-changed"
    try:
        setattr(module, attr, wrapper(original))
        return "wrapped"
    except Exception:
        logger.debug("advanced-providers P2: setattr failed on %s.%s", module_name, attr, exc_info=True)
        return "missing"


def probe() -> dict[str, str]:
    """Current state of all wrapped seams (for selfcheck, read-only)."""
    import importlib

    out: dict[str, str] = {}
    for label, module_name, attr in (
        ("anthropic", "agent.anthropic_adapter", "_new_sdk_client"),
        ("auxiliary", "agent.auxiliary_client", "_openai_http_client_kwargs"),
        ("catalog_profile", "hermes_cli.models", "_profile_live_catalog"),
        ("catalog_picker", "hermes_cli.model_switch_providers", "_discover_endpoint_models"),
        ("advanced_routing", "providers", "get_provider_profile"),
        ("openrouter_fallback", "hermes_cli.runtime_provider", "_resolve_openrouter_runtime"),
    ):
        try:
            if label == "openrouter_fallback":
                # 闸门用的是自己的 marker（与 transport 包装不同的属性名）。
                from . import fallback_guard

                marker = fallback_guard._MARKER
            else:
                marker = _MARKER
            module = importlib.import_module(module_name)
            fn = getattr(module, attr, None)
            if fn is None:
                out[label] = "missing"
            elif getattr(fn, marker, False):
                out[label] = "wrapped"
            else:
                out[label] = "original"
        except Exception:
            out[label] = "unimportable"
    return out
