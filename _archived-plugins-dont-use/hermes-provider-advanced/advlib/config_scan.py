"""Config scan + route table for ``providers-advanced:`` (Decoupled spec).

Reads the RAW config (``hermes_cli.config.load_config_readonly()``) to scan the
dedicated top-level section:
- ``providers-advanced:`` mapping of provider_key -> dict

Core insulation contract:
- The core's native ``providers:`` and legacy ``custom_providers:`` sections are
  COMPLETELY HANDS-OFF. Native providers remain under native Hermes governance;
  advanced plugin code never intercepts, wraps, or proxies them.
- Any leftover ``advanced:`` blocks in ``providers:`` are ignored and flagged as
  warnings.
- All entries under ``providers-advanced:`` are registered under the dedicated
  namespace ``advanced:<identifier>`` (e.g. ``advanced:codex``, ``advanced:deepseek-proxy``).
- The registered display name advertised to Hermes Agent is strictly formatted as
  ``[ADVANCED] <name>`` (or ``[ADVANCED] <provider_key>`` if ``name`` is omitted).
- OAuth dialect (``api_mode: advanced_oauth`` and ``platform://account``) is ONLY
  honored within ``providers-advanced``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("hermes.advanced_providers")

# Route-table cache. load_config_readonly() is itself memoized by the core
# (file-signature keyed), so re-reading raw config is cheap; but rebuilding
# the dict + normalize pass on every client construction is not — cache on
# the config file's mtime+size instead (our own signature, no core import).
_CACHE_LOCK = threading.Lock()
_CACHE: Optional["RouteTable"] = None
_CACHE_SIG: tuple = ()


def _config_signature() -> tuple:
    """(home_str, mtime_ns, size) of config.yaml, or () when unresolvable."""
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        cfg = home / "config.yaml"
        st = os.stat(cfg)
        return (str(home), st.st_mtime_ns, st.st_size)
    except Exception:
        return ()


def parse_oauth_ref(entry: "AdvancedEntry") -> tuple[str, str]:
    """(platform, account) for an oauth entry — v3 scheme form first, v1 alias fallback."""
    raw = str(entry.base_url or "").strip()
    if "://" in raw and not raw.lower().startswith(("http://", "https://")):
        scheme, rest = raw.split("://", 1)
        platform = scheme.strip().lower()
        account = _decode_account(rest.split("/", 1)[0])
        if platform:
            return platform, account
    if entry.oauth_platform:
        acct = str(entry.oauth_account or "")
        if ":" in acct and (acct.split(":", 1)[0].strip().lower() == entry.oauth_platform
                            or not acct.split(":", 1)[0].strip()):
            acct = acct.split(":", 1)[1]
        return entry.oauth_platform, acct.strip()
    return "", ""


def _decode_account(value: str) -> str:
    """Percent-decode once, tolerate malformed escapes by returning raw."""
    value = str(value or "").strip()
    try:
        from urllib.parse import unquote

        return unquote(value).strip()
    except Exception:
        return value


def parse_oauth_ref_from_url(base_url: str) -> tuple[str, str]:
    """``<scheme>://<account>`` -> (platform, decoded account)."""
    raw = str(base_url or "").strip()
    if "://" in raw and not raw.lower().startswith(("http://", "https://")):
        scheme, rest = raw.split("://", 1)
        platform = scheme.strip().lower()
        if platform:
            return platform, _decode_account(rest.split("/", 1)[0])
    return "", ""


@dataclass
class AdvancedEntry:
    """One custom provider entry managed under ``providers-advanced:``."""

    provider_key: str          # providers-advanced.<key>
    base_url: str              # raw as configured (http(s) URL, or scheme://account for oauth)
    route_url: str             # normalized (api mode) — "" for oauth entries
    auth_mode: str             # "api" | "oauth"
    proxy: str                 # normalized proxy URL or ""
    model: str = ""            # entry's model slug
    oauth_platform: str = ""   # platform key (e.g. codex, claude, antigravity, qwen)
    oauth_account: str = ""    # account portion
    api_mode_cfg: str = ""     # declared api_mode (e.g. advanced_oauth)
    fingerprint: str = "hermes"  # hermes|sub2api|cpa (oauth entries)
    ssl_verify: Any = None     # entry-level, for proxy client verify
    ssl_ca_cert: Optional[str] = None
    slug: str = ""            # internal slug: advanced:<provider_key_norm>
    declared_name: str = ""   # original name field in config
    display_name: str = ""    # formatted name: "[ADVANCED] " + (declared_name or provider_key)
    raw_config: dict = field(default_factory=dict)
    _warned_bypass: set = field(default_factory=set)
    _warned_proxy: set = field(default_factory=set)

    def note_bypass_once(self, base_url: str) -> None:
        if base_url not in self._warned_bypass:
            self._warned_bypass.add(base_url)
            logger.info(
                "advanced-providers: %s proxy skipped — %s matches NO_PROXY (direct)",
                self.provider_key, base_url,
            )

    def note_proxy_once(self, base_url: str, proxy: str) -> None:
        key = (base_url, proxy)
        if key not in self._warned_proxy:
            self._warned_proxy.add(key)
            logger.info(
                "advanced-providers: %s requests routed via proxy %s",
                self.provider_key, proxy,
            )


@dataclass
class RouteTable:
    """Parsed declarations from ``providers-advanced:``, split by auth_mode."""

    by_route: dict[str, AdvancedEntry] = field(default_factory=dict)      # api-mode: route_url -> entry
    by_slug: dict[str, AdvancedEntry] = field(default_factory=dict)       # slug (e.g. advanced:codex) -> entry
    by_oauth: dict[tuple[str, str], AdvancedEntry] = field(default_factory=dict)  # (platform, account) -> entry
    by_oauth_ref: dict[str, AdvancedEntry] = field(default_factory=dict)  # "scheme://account" -> entry
    by_oauth_key: dict[str, AdvancedEntry] = field(default_factory=dict)  # provider_key -> oauth entry
    all_entries: list[AdvancedEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lookup_slug(self, slug_or_name: str) -> Optional[AdvancedEntry]:
        """Find entry by its slug (e.g. 'advanced:my-model') or bare key."""
        key = str(slug_or_name or "").strip().lower()
        if not key:
            return None
        if key in self.by_slug:
            return self.by_slug[key]
        if not key.startswith("advanced:"):
            return self.by_slug.get(f"advanced:{key}")
        return None

    def lookup_oauth_ref(self, base_url: str) -> Optional[AdvancedEntry]:
        """OAuth entry whose base_url IS *base_url* (scheme://account form)."""
        key = str(base_url or "").strip().rstrip("/").lower()
        return self.by_oauth_ref.get(key) if key else None

    def lookup(self, base_url: str) -> Optional[AdvancedEntry]:
        """API-mode entry whose route identity equals *base_url*, or None."""
        try:
            from hermes_cli.route_identity import normalize_route_base_url

            target = normalize_route_base_url(base_url)
        except Exception:
            target = str(base_url or "").strip().rstrip("/").lower()
        if not target:
            return None
        entry = self.by_route.get(target)
        if entry is not None:
            return entry
        stripped = str(base_url or "").strip().rstrip("/").lower()
        for cand in self.by_route.values():
            if cand.base_url.strip().rstrip("/").lower() == stripped:
                return cand
        return None

    @classmethod
    def from_config(cls, *, config: Optional[dict] = None) -> "RouteTable":
        """Build (or return cached) route table from raw config."""
        global _CACHE, _CACHE_SIG
        if config is None:
            sig = _config_signature()
            with _CACHE_LOCK:
                if _CACHE is not None and sig and sig == _CACHE_SIG:
                    return _CACHE
            table = cls._build(_load_raw_config())
            with _CACHE_LOCK:
                _CACHE, _CACHE_SIG = table, sig
            return table
        return cls._build(config)

    @staticmethod
    def _build(config: dict) -> "RouteTable":
        table = RouteTable()
        try:
            from hermes_cli.route_identity import normalize_route_base_url
        except Exception:
            def normalize_route_base_url(u: Any) -> str:  # type: ignore
                return str(u or "").strip().rstrip("/").lower()

        def add_entry(provider_key: str, raw: dict) -> None:
            adv = raw.get("advanced")
            if not isinstance(adv, dict):
                adv = {}
            api_mode_cfg = str(raw.get("api_mode") or raw.get("transport") or "").strip().lower()
            alias_oauth = str(adv.get("auth_mode") or "").strip().lower() == "oauth"
            base = str(raw.get("base_url") or raw.get("url") or raw.get("api") or "").strip()

            # In providers-advanced, oauth is recognized via api_mode, base_url scheme, or alias
            scheme_oauth = bool(base) and "://" in base and not base.lower().startswith(
                ("http://", "https://"))
            is_oauth = api_mode_cfg == "advanced_oauth" or alias_oauth or scheme_oauth

            if is_oauth and alias_oauth and api_mode_cfg != "advanced_oauth":
                table.warnings.append(
                    f"{provider_key}: advanced.auth_mode=oauth is the v1/v2 alias — prefer "
                    f"api_mode: advanced_oauth (translated automatically)")

            fingerprint = str(adv.get("oauth_fingerprint") or "hermes").strip().lower()
            norm_key = provider_key.strip().lower().replace(" ", "-")
            slug = f"advanced:{norm_key}"

            declared_name = str(raw.get("name") or "").strip()
            if declared_name:
                display_name = f"[ADVANCED] {declared_name}"
            else:
                display_name = f"[ADVANCED] {provider_key.strip()}"

            entry = AdvancedEntry(
                provider_key=provider_key,
                base_url=base,
                route_url="",
                auth_mode="oauth" if is_oauth else "api",
                proxy=_normalize_proxy(adv.get("proxy")),
                model=str(raw.get("model") or "").strip(),
                oauth_platform=str(adv.get("oauth_platform") or "").strip().lower(),
                oauth_account=str(adv.get("oauth_account") or "").strip(),
                api_mode_cfg=api_mode_cfg,
                fingerprint=fingerprint,
                ssl_verify=raw.get("ssl_verify"),
                ssl_ca_cert=raw.get("ssl_ca_cert") if isinstance(raw.get("ssl_ca_cert"), str) else None,
                slug=slug,
                declared_name=declared_name,
                display_name=display_name,
                raw_config=raw,
            )

            if entry.proxy and adv.get("proxy") and not isinstance(adv.get("proxy"), str):
                table.warnings.append(f"{provider_key}: advanced.proxy must be a string — ignored")
                entry.proxy = ""

            if is_oauth:
                platform, account = parse_oauth_ref(entry)
                if not platform:
                    table.warnings.append(
                        f"{provider_key}: oauth entry needs base_url 'platform://account' "
                        f"(or v1 oauth_platform) — entry disabled")
                    return
                entry.oauth_platform, entry.oauth_account = platform, account
                ref = f"{platform}://{account}".rstrip("/").lower()
                if ref in table.by_oauth_ref:
                    table.warnings.append(
                        f"{provider_key}: duplicate oauth reference {ref} — first declaration wins (R4)")
                    return
                table.by_oauth_ref[ref] = entry
                table.by_oauth[(platform, account)] = entry
                table.by_oauth_key[entry.provider_key.strip().lower()] = entry
            else:
                if not entry.base_url:
                    table.warnings.append(f"{provider_key}: auth_mode=api with no base_url — entry cannot route")
                    return
                route = normalize_route_base_url(entry.base_url)
                if not route:
                    table.warnings.append(f"{provider_key}: unparseable base_url — entry skipped")
                    return
                if route in table.by_route:
                    table.warnings.append(
                        f"{provider_key}: duplicate route {route} — first declaration wins (R4)")
                else:
                    entry.route_url = route
                    table.by_route[route] = entry

            # Register slugs
            table.by_slug[slug] = entry
            table.by_slug[norm_key] = entry
            table.all_entries.append(entry)

        # ── ONLY scan providers-advanced: ──────────────────────────────
        adv_providers = config.get("providers-advanced")
        if isinstance(adv_providers, dict):
            for name, raw in adv_providers.items():
                if isinstance(raw, dict):
                    add_entry(str(name), raw)
                else:
                    table.warnings.append(
                        f"providers-advanced.{name}: expected a dict, got {type(raw).__name__} — skipped")
        elif adv_providers is not None:
            table.warnings.append(
                f"providers-advanced: expected a mapping, got {type(adv_providers).__name__} — skipped")

        # ── Zero-touch check for native providers: (deprecation warnings only) ──
        native_providers = config.get("providers")
        if isinstance(native_providers, dict):
            for name, raw in native_providers.items():
                if isinstance(raw, dict):
                    if raw.get("advanced") is not None:
                        table.warnings.append(
                            f"providers.{name}.advanced is deprecated and ignored — move to providers-advanced.{name}.advanced"
                        )
                    if str(raw.get("api_mode") or "").strip().lower() == "advanced_oauth":
                        table.warnings.append(
                            f"providers.{name}.api_mode=advanced_oauth is ignored in native providers — move to providers-advanced.{name}"
                        )

        # Legacy custom_providers check (deprecation warning only)
        legacy = config.get("custom_providers")
        if isinstance(legacy, list):
            for raw in legacy:
                if isinstance(raw, dict) and raw.get("advanced") is not None:
                    k = str(raw.get("name") or raw.get("provider") or "<unnamed>").strip()
                    table.warnings.append(
                        f"custom_providers[{k}].advanced is deprecated and ignored — move to providers-advanced"
                    )

        for w in table.warnings:
            logger.warning("advanced-providers: %s", w)
        return table


def _normalize_proxy(value: Any) -> str:
    """Normalized proxy URL string, or '' (uses core utils when available)."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        from utils import normalize_proxy_url

        return normalize_proxy_url(raw) or ""
    except Exception:
        return raw


def _load_raw_config() -> dict:
    """Raw config dict (never mutate!). Empty dict on any failure (fail-open)."""
    try:
        from hermes_constants import get_hermes_home
        import yaml

        cfg_path = get_hermes_home() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, encoding="utf-8-sig") as f:
                res = yaml.safe_load(f)
                if isinstance(res, dict):
                    return res
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("advanced-providers: raw config unreadable (fail-open)", exc_info=True)
        return {}


_SETTINGS_KEY = "hermes-provider-advanced"


def load_plugin_settings() -> dict:
    """``plugins.entries.hermes-provider-advanced.settings`` (dict, {})."""
    cfg = _load_raw_config()
    entries = ((cfg.get("plugins") or {}).get("entries") or {})
    for key in (_SETTINGS_KEY, "hermes_provider_advanced"):
        entry = entries.get(key)
        if isinstance(entry, dict):
            settings = entry.get("settings")
            if isinstance(settings, dict):
                return settings
    return {}


def invalidate_cache() -> None:
    """Drop the cached route table (tests / status command)."""
    global _CACHE, _CACHE_SIG
    with _CACHE_LOCK:
        _CACHE, _CACHE_SIG = None, ()
    try:
        import sys
        for mod_name in ("advlib.config_scan", "hermes_advlib.config_scan"):
            m = sys.modules.get(mod_name)
            if m is not None:
                m._CACHE = None
                m._CACHE_SIG = ()
    except Exception:
        pass
