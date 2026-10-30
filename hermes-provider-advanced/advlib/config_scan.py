"""Config scan + route table for ``advanced:`` blocks (v2 spec §2/§5.1).

Reads the RAW config (``hermes_cli.config.load_config_readonly()``) because
the core's ``_normalize_custom_provider_entry`` whitelist-rebuild drops the
``advanced`` key from the runtime view (``get_compatible_custom_providers()``).

Both config shapes are consumed (N4):
- legacy ``custom_providers:`` list of dicts
- v12+ ``providers:`` mapping of name -> dict

Routing (v3 R2'''/R4''):
- oauth track, three accepted spellings (first match wins):
    * ``api_mode: advanced_oauth``          (v3 primary — plugin's registered dialect)
    * ``base_url: <platform>://<account>``  (v3 reference; non-http scheme)
    * ``advanced.auth_mode: oauth`` + ``oauth_platform``/``oauth_account``
      (v1/v2 alias, translated once with an INFO)
  oauth entries NEVER join the base_url route table and are keyed by the
  verbatim reference string ``platform://account``.
- everything else is the api track: joins the base_url route table keyed by
  ``normalize_route_base_url`` (core rules).
- First declarer wins on duplicate routes, WARN once per duplicate (R4).
- ``advanced.oauth_fingerprint: hermes|sub2api|cpa`` (default hermes) rides
  on the oauth entry (v3 R6).
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
    """(mtime_ns, size) of config.yaml, or () when unresolvable."""
    try:
        from hermes_constants import get_hermes_home

        cfg = get_hermes_home() / "config.yaml"
        st = os.stat(cfg)
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return ()


def parse_oauth_ref(entry: "AdvancedEntry") -> tuple[str, str]:
    """(platform, account) for an oauth entry — v3 scheme form first, v1 alias fallback.

    v3: ``base_url: <scheme>://<account-name>`` — scheme is the platform key,
        the netloc (everything before any '/') is the account name; '%' is
        URL-decoded once so ``me%40x.com`` and a literal ``me@x.com`` address
        the same slot.
    v1: ``advanced.oauth_platform`` + ``advanced.oauth_account`` ("platform:name"
        or bare name); the entry's base_url is an inert placeholder.
    Returns ("", "") when neither shape resolves.
    """
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
    """``<scheme>://<account>`` -> (platform, decoded account) — URL-level
    mirror of parse_oauth_ref's v3 branch (no AdvancedEntry needed).
    Returns ("", "") for http(s) or malformed refs."""
    raw = str(base_url or "").strip()
    if "://" in raw and not raw.lower().startswith(("http://", "https://")):
        scheme, rest = raw.split("://", 1)
        platform = scheme.strip().lower()
        if platform:
            return platform, _decode_account(rest.split("/", 1)[0])
    return "", ""


@dataclass
class AdvancedEntry:
    """One provider entry that declared an ``advanced:`` block."""

    provider_key: str          # providers.<name> key or list entry name
    base_url: str              # raw as configured (http(s) URL, or scheme://account for oauth)
    route_url: str             # normalized (api mode) — "" for oauth entries
    auth_mode: str             # "api" | "oauth"  (R2': absent -> "api")
    proxy: str                 # normalized proxy URL or ""
    model: str = ""            # entry's model slug (cache key / diagnostics only)
    oauth_platform: str = ""   # v1 alias (v2: derived from base_url scheme)
    oauth_account: str = ""    # v1 alias / v2 account portion
    api_mode_cfg: str = ""     # entry's declared api_mode (for the oauth cross-check)
    fingerprint: str = "hermes"  # v3 R6: hermes|sub2api|cpa (oauth entries)
    ssl_verify: Any = None     # entry-level, for proxy client verify
    ssl_ca_cert: Optional[str] = None
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
    """Parsed ``advanced:`` declarations, split by auth_mode (R4')."""

    by_route: dict[str, AdvancedEntry] = field(default_factory=dict)      # api-mode: route_url -> entry
    by_oauth: dict[tuple[str, str], AdvancedEntry] = field(default_factory=dict)  # (platform, account) -> entry
    by_oauth_ref: dict[str, AdvancedEntry] = field(default_factory=dict)  # v2 "scheme://account" -> entry
    by_oauth_key: dict[str, AdvancedEntry] = field(default_factory=dict)  # provider_key -> oauth entry (named-route catalog seam)
    all_entries: list[AdvancedEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lookup_oauth_ref(self, base_url: str) -> Optional[AdvancedEntry]:
        """OAuth entry whose base_url IS *base_url* (v2 scheme://account form).

        Identity is exact-string (lowercased, trailing '/' trimmed): the
        placeholder is inert config text, never rewritten by core URL helpers
        (verified: _to_openai_base_url leaves scheme:// untouched except
        rstrip('/')).
        """
        key = str(base_url or "").strip().rstrip("/").lower()
        return self.by_oauth_ref.get(key) if key else None

    def lookup(self, base_url: str) -> Optional[AdvancedEntry]:
        """API-mode entry whose route identity equals *base_url*, or None.

        ``base_url`` here arrives ALREADY rewritten by the core's
        ``_to_openai_base_url`` (dual-surface anthropic -> openai sibling) —
        so we normalize both sides and also fall back to a raw-string match
        against entries' as-configured URL for hosts the rewrite changed.
        """
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
        # Second pass: rewrite-tolerant raw comparison (cheap, table is tiny).
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
            # v3 R2''': three spellings of "this entry is advanced_oauth"
            scheme_oauth = bool(base) and "://" in base and not base.lower().startswith(
                ("http://", "https://"))
            is_oauth = api_mode_cfg == "advanced_oauth" or alias_oauth or scheme_oauth
            if not is_oauth and not adv:
                return  # plain entry, no advanced: block — zero behaviour change (R5)
            if is_oauth and alias_oauth and api_mode_cfg != "advanced_oauth":
                table.warnings.append(
                    f"{provider_key}: advanced.auth_mode=oauth is the v1/v2 alias — prefer "
                    f"api_mode: advanced_oauth (translated automatically)")
            fingerprint = str(adv.get("oauth_fingerprint") or "hermes").strip().lower()
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
                    table.warnings.append(f"{provider_key}: auth_mode=api with no base_url — proxy cannot route")
                    return
                route = normalize_route_base_url(entry.base_url)
                if not route:
                    table.warnings.append(f"{provider_key}: unparseable base_url — entry skipped")
                    return
                if route in table.by_route:
                    table.warnings.append(
                        f"{provider_key}: duplicate route {route} — first declaration wins (R4)")
                    return
                entry.route_url = route
                table.by_route[route] = entry
            table.all_entries.append(entry)

        # v12+ providers: mapping
        providers = config.get("providers")
        if isinstance(providers, dict):
            for name, raw in providers.items():
                if isinstance(raw, dict):
                    add_entry(str(name), raw)
        elif providers is not None:
            table.warnings.append(f"providers: expected a mapping, got {type(providers).__name__} — skipped")

        # legacy custom_providers: list
        legacy = config.get("custom_providers")
        if isinstance(legacy, list):
            for raw in legacy:
                if isinstance(raw, dict):
                    key = str(raw.get("name") or raw.get("provider") or "").strip() or "<unnamed>"
                    add_entry(key, raw)
        elif legacy is not None:
            table.warnings.append(
                f"custom_providers: expected a list, got {type(legacy).__name__} — skipped")

        # Friendly hint for the sample-config trap: top-level `provider:`
        # (singular) is the ACTIVE-provider string in core semantics; a dict
        # under it means the user meant `providers:`.
        singular = config.get("provider")
        if isinstance(singular, dict):
            has_adv = any(isinstance(v, dict) and isinstance(v.get("advanced"), dict)
                          for v in singular.values())
            if has_adv:
                table.warnings.append(
                    "top-level `provider:` (singular) holds advanced: blocks, but the core reads "
                    "`providers:` (plural) / `custom_providers:` — rename the section or the "
                    "entries will never route")
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
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("advanced-providers: raw config unreadable (fail-open)", exc_info=True)
        return {}


_SETTINGS_KEY = "hermes-provider-advanced"


def load_plugin_settings() -> dict:
    """``plugins.entries.hermes-provider-advanced.settings`` (dict, {}).

    Bare-name fallback per house rule (a subdir-deployed plugin resolves
    entries under a path-derived key only; the settings reader must not
    depend on where the plugin was installed).
    """
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
