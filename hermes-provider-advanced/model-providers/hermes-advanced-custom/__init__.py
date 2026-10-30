"""User-layer overlay of the bundled ``custom`` model-provider profile.

Deployed to ``$HERMES_HOME/plugins/model-providers/hermes-advanced-custom/``.
The provider scan (``providers/__init__.py::_scan_home_layer``) imports this
package; we re-register an EXTENDED copy of the bundled CustomProfile under
the same name — user layer wins over bundled (last-writer-wins lookup), so
every ``provider: custom`` / ``custom:<name>`` route now consults our
``build_client_kwargs_extras`` before the core's ``http_client`` gate.

Why extend (not reimplement): the bundled profile carries behaviour we must
not lose (reasoning-effort vocabulary, medium default, fetch_models guard,
Ollama think handling). We subclass the ALREADY-REGISTERED instance's class
and copy its dataclass fields, so a core upgrade to CustomProfile flows
through automatically.

Fail-open contract: every step is wrapped — if advlib is missing, the bundled
module vanished, or registration blows up, we simply do not register (or
register a no-op extension), and the core keeps working exactly as before.
"""

from __future__ import annotations

import dataclasses
import logging
import sys

logger = logging.getLogger("hermes.advanced_providers.overlay")

REGISTERED = False  # set True when our profile replaced the bundled one


def _get_advlib():
    """Load the shared advlib under its fixed module name (or None)."""
    from pathlib import Path

    here = Path(__file__).resolve().parent
    # advlib ships inside the deployed plugin dir; the overlay may live in a
    # sibling tree, so probe: env override, $HERMES_HOME plugin dir, dev-tree
    # relative location (../../hermes-provider-advanced/advlib).
    candidates = []
    import os

    env = os.environ.get("HERMES_ADVANCED_LIB_DIR", "").strip()
    if env:
        candidates.append(Path(env))
    try:
        from hermes_constants import get_hermes_home

        candidates.append(Path(get_hermes_home()) / "plugins" / "hermes-provider-advanced" / "advlib")
        # installed via a clone of the custom-plugins collection repo
        candidates.append(Path(get_hermes_home()) / "plugins" / "custom-plugins"
                          / "hermes-provider-advanced" / "advlib")
    except Exception:
        pass
    # deployed: <plugins>/hermes-provider-advanced/advlib ; dev tree:
    # <workdir>/hermes-provider-advanced/{model-providers/hermes-advanced-custom, advlib}
    candidates.append(here.parent.parent / "hermes-provider-advanced" / "advlib")
    candidates.append(here.parent.parent / "advlib")

    import importlib.util

    existing = sys.modules.get("hermes_advlib")
    if existing is not None:
        return existing
    for cand in candidates:
        init_file = cand / "__init__.py"
        if not init_file.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            "hermes_advlib", init_file, submodule_search_locations=[str(cand)]
        )
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_advlib"] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop("hermes_advlib", None)
            logger.warning("advanced-providers overlay: advlib failed to load (fail-open)", exc_info=True)
            return None
        return module
    return None


def _bundled_custom_profile():
    """The bundled CustomProfile instance to extend.

    Import-order race: the home-layer scan can run DURING
    ``providers._discover_providers()`` (an early bundled/pip plugin calls a
    lookup), i.e. BEFORE the bundled custom module registered. Resolution
    ladder:
    1. ``sys.modules['plugins.model_providers.custom']`` (post-discovery: the
       stable name the scan assigned it)
    2. the process-wide ``_REGISTRY`` / current home layer
    3. private fresh import of the bundled file under OUR OWN module name —
       executing it registers a throwaway instance (into the scan's
       registration target), from which we clone; the later bundled
       discovery step still imports under the stable name normally.
    """
    mod = sys.modules.get("plugins.model_providers.custom")
    profile = getattr(mod, "custom", None) if mod is not None else None
    if profile is not None:
        return profile
    try:
        from providers import _REGISTRY

        profile = _REGISTRY.get("custom")
        if profile is not None:
            return profile
    except Exception:
        pass
    return _private_import_bundled_custom()


def _private_import_bundled_custom():
    """Load the bundled custom profile file under a private module name."""
    try:
        import importlib.util

        from providers import _BUNDLED_PLUGINS_DIR

        init_file = _BUNDLED_PLUGINS_DIR / "custom" / "__init__.py"
        if not init_file.is_file():
            return None
        module_name = "_hermes_adv_overlay_bundled_custom"
        cached = sys.modules.get(module_name)
        if cached is not None:
            return getattr(cached, "custom", None)
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(init_file.parent)]
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return getattr(module, "custom", None)
    except Exception:
        logger.warning(
            "advanced-providers overlay: private bundled-custom import failed", exc_info=True)
        return None


def _build_overlay(base_profile, advlib):
    """Subclass-of-instance's-class with advanced extras folded into the hook."""

    class AdvancedCustomProfile(type(base_profile)):
        def build_client_kwargs_extras(self, **context):
            extras = dict(super().build_client_kwargs_extras(**context))
            try:
                adv = advlib.client_extras_for_base_url(str(context.get("base_url") or ""))
            except Exception:
                logger.debug("advanced-providers overlay: extras hook failed (fail-open)", exc_info=True)
                adv = {}
            for key, value in (adv or {}).items():
                extras.setdefault(key, value)
            return extras

        def create_client(self, **client_kwargs):
            """P3 seam: materialize advanced_oauth accounts for scheme refs.

            The core calls this on BOTH the main-agent and auxiliary client
            construction paths (agent_runtime_helpers._provider_supplied_client
            / auxiliary_client._api_key_profile_supplied_client). A non-http
            scheme base_url (``codex://…``) is ours; anything else returns
            None so the standard path proceeds untouched. Raising is
            discouraged by the contract but tolerated (caller logs + falls
            back); we raise ONLY for our own refs with clear pointers —
            silently returning None for a broken oauth ref would connect the
            placeholder scheme URL somewhere else and fail more confusingly.
            """
            try:
                return advlib.oauth_client_for_base_url(
                    str(client_kwargs.get("base_url") or ""),
                    request_timeout=client_kwargs.get("timeout"),
                    extra=client_kwargs)
            except Exception:
                if advlib.is_oauth_scheme_ref(str(client_kwargs.get("base_url") or "")):
                    raise
                logger.debug("advanced-providers overlay: create_client hook error on a "
                             "non-advanced base_url (fail-open)", exc_info=True)
                return None

    AdvancedCustomProfile.__name__ = "AdvancedCustomProfile"
    AdvancedCustomProfile.__qualname__ = "AdvancedCustomProfile"
    try:
        fields = {f.name: getattr(base_profile, f.name) for f in dataclasses.fields(base_profile)}
        return AdvancedCustomProfile(**fields)
    except Exception:
        logger.warning("advanced-providers overlay: cannot clone profile fields (fail-open)", exc_info=True)
        return None


def _register(profile) -> bool:
    try:
        from providers import register_provider

        register_provider(profile)
        return True
    except Exception:
        logger.warning("advanced-providers overlay: registration failed (fail-open)", exc_info=True)
        return False


def install() -> bool:
    """Build + register the overlay. Idempotent-ish; safe to call at import."""
    global REGISTERED
    if REGISTERED:
        return True
    advlib = _get_advlib()
    if advlib is None:
        logger.warning(
            "advanced-providers overlay: advlib not found — overlay NOT installed; "
            "advanced: blocks will be inert. Check $HERMES_HOME/plugins/hermes-provider-advanced/")
        return False
    base_profile = _bundled_custom_profile()
    if base_profile is None:
        logger.warning(
            "advanced-providers overlay: bundled 'custom' profile not found — overlay NOT installed")
        return False
    overlay = _build_overlay(base_profile, advlib)
    if overlay is None:
        return False
    ok = _register(overlay)
    if ok:
        REGISTERED = True
        logger.info(
            "advanced-providers overlay: 'custom' profile extended (name=%s, aliases=%s)",
            overlay.name, tuple(overlay.aliases))
    return ok


# Module-level side effect: the provider scan imports this file expecting
# registration to happen at import (same contract as the bundled profiles).
install()
