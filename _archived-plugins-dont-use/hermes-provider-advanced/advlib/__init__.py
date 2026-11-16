"""hermes-provider-advanced shared library.

Loaded under the fixed module name ``hermes_advlib`` by BOTH halves of the
plugin (the agent-side CLI package and the user-layer ``custom`` provider
overlay) via :func:`load_advlib`, so one process shares one instance.

Design rules (v2 spec §5/§6):
- stdlib-only at import time; every core-module import is lazy + try/except
  (the overlay executes inside ``providers._scan_home_layer`` where heavy or
  circular imports would poison provider discovery).
- fail-open everywhere: any error degrades to "no advanced behaviour", never
  to a broken client or a dead session.
- never log token values or account-store contents (N2).
"""

from __future__ import annotations

__version__ = "0.1.0"

_MODULE_NAME = "hermes_advlib"


def load_advlib():
    """Import (or fetch) the shared library under the fixed module name.

    Resolution order:
    1. ``HERMES_ADVANCED_LIB_DIR`` (tests / dev override)
    2. ``$HERMES_HOME/plugins/hermes-provider-advanced/advlib``
    3. ``<this file's grandparent>/advlib`` (overlay kept inside the dev tree)

    Returns the module, or None when it cannot be loaded (caller must then
    behave as if no advanced config existed — fail-open).
    """
    import importlib.util
    import os
    import sys
    from pathlib import Path

    existing = sys.modules.get(_MODULE_NAME)
    if existing is not None:
        return existing

    candidates: list[Path] = []
    env = os.environ.get("HERMES_ADVANCED_LIB_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    try:
        from hermes_constants import get_hermes_home

        candidates.append(Path(get_hermes_home()) / "plugins" / "hermes-provider-advanced" / "advlib")
        # installed via a clone of the custom-plugins collection repo: the
        # plugin (and the copied-over overlay) resolve advlib one level deeper
        candidates.append(Path(get_hermes_home()) / "plugins" / "custom-plugins"
                          / "hermes-provider-advanced" / "advlib")
    except Exception:
        pass
    here = Path(__file__).resolve().parent
    candidates.append(here)  # already inside advlib/
    candidates.append(here.parent.parent / "advlib")

    for cand in candidates:
        init_file = cand / "__init__.py"
        if not init_file.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            _MODULE_NAME, init_file, submodule_search_locations=[str(cand)]
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_MODULE_NAME, None)
            raise
        return module
    return None


def client_extras_for_base_url(base_url: str) -> dict:
    """The P1 entry point consumed by the custom-profile overlay hook.

    Mirrors the contract of ``ProviderProfile.build_client_kwargs_extras``:
    returns a dict of OpenAI-client kwargs to ``setdefault`` into the core's
    client_kwargs BEFORE the ``if "http_client" not in client_kwargs`` gate
    (agent_runtime_helpers.py). Empty dict = zero behaviour change (R5).

    Never raises: the overlay wraps us too, but belt-and-braces per fail-open.
    """
    try:
        from .config_scan import RouteTable
        from .proxy_factory import build_proxy_client, proxy_decision, resolve_verify

        base_url = str(base_url or "").strip()
        if not base_url:
            return {}
        table = RouteTable.from_config()
        entry = table.lookup(base_url)
        if entry is None:
            return {}
        if entry.auth_mode != "api":
            # P3: oauth-mode entries are materialized via create_client
            # (oauth_client_for_base_url); the extras hook keeps hands off.
            return {}
        proxy, reason = proxy_decision(entry, base_url)
        if not proxy:
            if reason == "bypass":
                entry.note_bypass_once(base_url)
            return {}
        verify = resolve_verify(entry, base_url)
        client = build_proxy_client(proxy, verify=verify)
        if client is None:
            return {}
        entry.note_proxy_once(base_url, proxy)
        return {"http_client": client}
    except Exception:
        try:
            from .config_scan import logger

            logger.debug("advanced-providers: client extras failed (fail-open)", exc_info=True)
        except Exception:
            pass
        return {}


def is_oauth_scheme_ref(base_url: str) -> bool:
    """True when *base_url* looks like an advanced_oauth reference
    (``<scheme>://<account>``, non-http scheme)."""
    try:
        from .config_scan import parse_oauth_ref_from_url

        platform, _ = parse_oauth_ref_from_url(str(base_url or ""))
        return bool(platform)
    except Exception:
        return False


def oauth_client_for_base_url(base_url: str, *, request_timeout=None,
                              extra: "dict | None" = None):
    """The P3 entry point consumed by the overlay's ``create_client`` hook.

    Returns a live-token client for an advanced_oauth reference, or None for
    non-ref base_urls (standard path proceeds). Raises with actionable
    pointers ONLY for our own refs (missing account / disabled platform) —
    the contract-tolerant caller re-raises those into visible errors instead
    of silently connecting a placeholder URL somewhere wrong.
    """
    base_url = str(base_url or "").strip()
    if not is_oauth_scheme_ref(base_url):
        return None
    from . import oauth_gate

    if oauth_gate.oauth_disabled():
        # Kill switch: refuse OUR refs loudly. Returning None would hand the
        # placeholder scheme URL to the generic client — a stranger error.
        raise RuntimeError(f"advanced-providers: cannot serve {base_url!r}: "
                           f"{oauth_gate.DISABLED_MSG}")
    from .oauth_runtime import ensure_registered

    ensure_registered()
    from .oauth_runtime import build_client_for_ref

    return build_client_for_ref(base_url, request_timeout=request_timeout, extra=extra)


def oauth_models_for_ref(base_url: str, *, force_refresh: bool = False):
    """The P3.6 entry point consumed by the overlay's ``fetch_models`` hook:
    the model list the ACCOUNT's own upstream catalog serves (CPA-style),
    or None (caller degrades to its normal behaviour). Never raises.
    """
    base_url = str(base_url or "").strip()
    if not is_oauth_scheme_ref(base_url):
        return None
    try:
        from . import oauth_gate

        if oauth_gate.oauth_disabled():
            return None
    except Exception:
        pass
    try:
        from .oauth_models import list_models_for_ref

        return list_models_for_ref(base_url, force_refresh=force_refresh)
    except Exception:
        try:
            from .config_scan import logger

            logger.debug("advanced-providers: oauth listing failed (fail-open)", exc_info=True)
        except Exception:
            pass
        return None
