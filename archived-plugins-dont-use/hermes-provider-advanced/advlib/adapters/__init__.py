"""Platform adapter registry — one .py per OAuth platform, independently removable.

Adapters self-register at import of THIS package. Delisting a platform is a
one-liner the owner controls without touching code:

    ADVANCED_ADAPTERS=antigravity          # disable one (comma-separated)
    ADVANCED_ADAPTERS=""                   # disable all OAuth behaviour

Adapter contract (v3, module-level names — what the runtime actually calls):
    NAME / ALIASES: str/tuple          platform keys ("codex"; scheme = NAME)
    def normalize(imported: dict, platform_hint: str = "") -> dict | None
                                      any recognized credential shape ->
                                      store account dict (None = not ours)
    def login_interactive(account_name: str, store) -> dict | None
                                      run the platform's own login (or None +
                                      INFO pointer when web login lands later)
    def build_openai_client(entry, account_name, store, *, fingerprint,
                            proxy, async_mode=False)   [OpenAI-SDK platforms]
    def build_anthropic_client(entry, account_name, store, *, fingerprint,
                            proxy)                    [anthropic-wire platforms]
    def resolve_inference_base_url(account, entry) -> str
Optional:
    def refresh(acct: dict, rt: str) -> dict          custom refresher when no
                                      core refresher exists (antigravity)
"""

from __future__ import annotations

import os
from typing import Callable, Optional

_REGISTRY: "dict[str, Adapter]" = {}


class AccountError(ValueError):
    """Payload is not (or not only) this platform's account shape."""


class AdapterUnavailable(RuntimeError):
    """Platform disabled, uninstalled, or its flow is not implemented."""


class Adapter:
    def __init__(self, name: str, display: str, module: object) -> None:
        self.name = name
        self.display = display
        self._m = module

    def __getattr__(self, item: str):  # contract passthrough
        return getattr(self._m, item)

    def is_disabled(self) -> tuple[bool, str]:
        if _is_env_disabled(self.name):
            return True, "disabled via ADVANCED_ADAPTERS"
        try:
            from ..config_scan import load_plugin_settings

            disabled = load_plugin_settings().get("disabled_adapters") or []
            if self.name in {str(x).strip().lower() for x in disabled}:
                return True, "disabled via plugin settings"
        except Exception:
            pass
        return False, ""

    def normalize(self, imported: dict, platform_hint: str = "") -> "Optional[dict]":
        """v3 contract: shape -> store account dict, or None (not ours)."""
        fn = getattr(self._m, "normalize", None)
        if fn is None:
            return None
        try:
            return fn(imported, platform_hint)
        except Exception:
            return None

    def login_interactive(self, account_name: str, store,
                          *, proxy: str = "") -> "Optional[dict]":
        fn = getattr(self._m, "login_interactive", None)
        if fn is None:
            raise AdapterUnavailable(
                f"{self.display}: web login not implemented — use JSON import (sub2api/cpa)")
        # Adapters that speak to the network take the per-login proxy; ones that
        # reuse a core flow (codex device-code) don't need it.
        try:
            import inspect

            if "proxy" in inspect.signature(fn).parameters:
                return fn(account_name, store, proxy=proxy)
        except (TypeError, ValueError):
            pass
        return fn(account_name, store)

    def build_client(self, entry: dict, account_name: str, store, *, fingerprint: str,
                     proxy: str, async_mode: bool = False):
        """Dispatch to whichever client builder this platform's wire uses."""
        openai_build = getattr(self._m, "build_openai_client", None)
        if openai_build is not None:
            return openai_build(entry, account_name, store, fingerprint=fingerprint,
                                proxy=proxy, async_mode=async_mode)
        anth_build = getattr(self._m, "build_anthropic_client", None)
        if anth_build is not None:
            return anth_build(entry, account_name, store, fingerprint=fingerprint, proxy=proxy)
        raise AdapterUnavailable(
            f"{self.display}: adapter builds no client (import-only platform?)")


def _is_env_disabled(name: str) -> bool:
    raw = os.environ.get("ADVANCED_ADAPTERS")
    if raw is None:
        return False
    if raw.strip() == "":
        return True  # all off
    disabled = {p.strip().lower() for p in raw.split(",") if p.strip()}
    # the env var lists ENABLED when prefixed '+=' semantics are absent; keep it
    # literal and boring: comma list = platforms to DISABLE
    return name.lower() in disabled


def register(module: object) -> None:
    name = str(getattr(module, "NAME", "") or "").strip().lower()
    if not name:
        return
    _REGISTRY[name] = Adapter(name, str(getattr(module, "DISPLAY", name)), module)


def get(name: str) -> Optional[Adapter]:
    adapter = _REGISTRY.get(str(name or "").strip().lower())
    if adapter is None:
        return None
    disabled, reason = adapter.is_disabled()
    if disabled:
        return None
    return adapter


def get_any(name: str) -> Optional[Adapter]:
    """Registered adapter even if disabled — for status/diagnostics messages."""
    return _REGISTRY.get(str(name or "").strip().lower())


def names() -> list[str]:
    out = []
    for key, adapter in _REGISTRY.items():
        disabled, _ = adapter.is_disabled()
        if not disabled:
            out.append(key)
    return out


def all_adapters() -> list["Adapter"]:
    """Every registered adapter, enabled or not — importers need shape
    matching across platforms that may be delisted for RUNTIME but must
    still recognize credentials for storage."""
    return list(_REGISTRY.values())


# ---------------------------------------------------------------- discovery
# Import every platform module in this directory. A missing/unreadable module
# simply means that platform is not available (its entries surface as
# AdapterUnavailable with a pointer) — which is exactly the delisting path.
import importlib as _importlib
import pkgutil as _pkgutil

for _info in _pkgutil.iter_modules(__path__):
    if _info.name.startswith("_"):
        continue
    try:
        _mod = _importlib.import_module(f"{__name__}.{_info.name}")
    except Exception as _exc:  # adapter file broken/removed: platform absent
        continue
    register(_mod)
