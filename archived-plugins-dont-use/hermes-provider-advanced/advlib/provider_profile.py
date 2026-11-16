"""Standalone AdvancedProviderProfile management for hermes-provider-advanced.

Registers:
1. One independent ProviderProfile per entry in `providers-advanced:`
   - name: `advanced:<key>` (e.g. `advanced:openrouter-pxy`)
   - display_name: `[ADVANCED]<entry.name>` (or `[ADVANCED] [OAUTH]...`)
   - aliases: (key, `advanced:<key>`)
2. One fallback profile `name="advanced"` aggregating all entries.
3. Bespoke `_PROVIDER_MODEL_FLOWS` for Hermes CLI `hermes model` selection.
4. Auto-bridge generator under $HERMES_HOME/plugins/model-providers/ to ensure
   discovery during non-agent commands (like `hermes model`).
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from providers.base import ProviderProfile

logger = logging.getLogger("hermes.advanced_providers.profile")

_OPENAI_COMPAT_WIRE_EFFORTS = ("low", "medium", "high", "max")


class AdvancedProviderProfile(ProviderProfile):
    """Standalone provider profile handling an advanced custom route."""

    def __init__(
        self,
        name: str,
        aliases: tuple[str, ...],
        *,
        display_name: str = "",
        description: str = "",
        base_url: str = "",
        entry: Optional[Any] = None,
        **kwargs: Any,
    ):
        super().__init__(
            name=name,
            aliases=aliases,
            env_vars=(),
            base_url=base_url,
            **kwargs,
        )
        self.display_name = display_name
        self.description = description or display_name
        self.entry = entry

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...]:
        return _OPENAI_COMPAT_WIRE_EFFORTS

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        return {"enabled": True, "effort": "medium"}

    def build_client_kwargs_extras(self, **context) -> dict[str, Any]:
        """Inject per-provider proxy http_client if configured."""
        extras: dict[str, Any] = {}
        try:
            from . import client_extras_for_base_url

            base_url = str(context.get("base_url") or self.base_url or "")
            adv = client_extras_for_base_url(base_url)
            for k, v in (adv or {}).items():
                extras.setdefault(k, v)
        except Exception:
            logger.debug("advanced provider extras hook failed (fail-open)", exc_info=True)
        return extras

    def create_client(self, **client_kwargs):
        """Materialize OAuth client for platform://account or api_mode: advanced_oauth."""
        try:
            from . import oauth_client_for_base_url, is_oauth_scheme_ref

            base_url = str(client_kwargs.get("base_url") or self.base_url or "")
            return oauth_client_for_base_url(
                base_url,
                request_timeout=client_kwargs.get("timeout"),
                extra=client_kwargs,
            )
        except Exception:
            from . import is_oauth_scheme_ref

            target_url = str(client_kwargs.get("base_url") or self.base_url or "")
            if is_oauth_scheme_ref(target_url):
                raise
            logger.debug("advanced provider create_client hook error (fail-open)", exc_info=True)
            return None

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """Fetch models from configured entry.models, account catalog if OAuth, or upstream."""
        target_entry = self.entry
        target_url = str(base_url or self.base_url or "")

        if target_entry is None and target_url:
            try:
                from .config_scan import RouteTable

                table = RouteTable.from_config()
                target_entry = table.lookup(target_url)
            except Exception:
                pass

        if target_entry is not None:
            raw = getattr(target_entry, "raw_config", {}) or {}
            cfg_models = raw.get("models")
            if isinstance(cfg_models, dict) and cfg_models:
                return list(cfg_models.keys())
            elif isinstance(cfg_models, list) and cfg_models:
                return [str(m) for m in cfg_models if m]

            discover = raw.get("discover_models", True)
            if isinstance(discover, str):
                discover = discover.lower() not in {"false", "no", "0"}
            saved_m = raw.get("model") or raw.get("default_model")
            if not discover and saved_m:
                return [str(saved_m)]

        try:
            from . import is_oauth_scheme_ref, oauth_models_for_ref

            if is_oauth_scheme_ref(target_url):
                models = oauth_models_for_ref(target_url, force_refresh=False)
                if models is not None:
                    return models
        except Exception:
            pass

        return super().fetch_models(api_key=api_key, base_url=base_url or self.base_url, timeout=timeout)


def _model_flow_advanced(config, selected_provider: str, current_model: str = "", args: Any = None) -> None:
    """Bespoke CLI model selection flow for an advanced provider row."""
    from hermes_cli.config import load_config, save_config
    from hermes_cli.model_setup_flows import _pick_model_or_prompt, _finish_model
    from providers import get_provider_profile
    from .config_scan import RouteTable

    table = RouteTable.from_config()
    entry = None
    for e in table.all_entries:
        if (
            e.slug == selected_provider
            or e.provider_key == selected_provider
            or f"advanced:{e.provider_key}" == selected_provider
        ):
            entry = e
            break

    profile = get_provider_profile(selected_provider)
    display_title = entry.display_name if entry else (profile.display_name if profile else selected_provider)
    base_url = entry.base_url if entry else (profile.base_url if profile else "")

    print(f"\n  Provider: {display_title}")
    if base_url:
        print(f"  URL:      {base_url}")

    models = None
    if profile:
        try:
            models = profile.fetch_models()
        except Exception:
            pass
    if not models and entry:
        raw = entry.raw_config or {}
        if raw.get("models"):
            models = list(raw["models"].keys())
        elif raw.get("model") or raw.get("default_model"):
            models = [str(raw.get("model") or raw.get("default_model"))]

    saved_model = (
        (entry.raw_config.get("model") or entry.raw_config.get("default_model") or "") if entry else ""
    )
    selected = _pick_model_or_prompt(
        models or [],
        f"Model name [{saved_model}]: " if saved_model else "Model name: ",
        current_model=saved_model or current_model,
        confirm_provider=selected_provider,
        confirm_base_url=base_url,
    )
    if not selected and saved_model:
        selected = saved_model

    if not selected:
        print("No model selected. Cancelled.")
        return

    _finish_model(
        selected,
        selected_provider,
        f"\n✅ Default model set to: {selected} (via {display_title})",
        base_url=base_url or None,
        api_mode=(entry.api_mode_cfg if entry else None) or None,
    )

    if entry and entry.provider_key:
        try:
            cfg = load_config()
            pa = cfg.get("providers-advanced")
            if isinstance(pa, dict) and entry.provider_key in pa:
                pa[entry.provider_key]["default_model"] = selected
                save_config(cfg)
        except Exception:
            pass


def build_and_register_advanced_profile() -> list[AdvancedProviderProfile]:
    """Scan providers-advanced: and register independent provider profiles for all entries."""
    registered: list[AdvancedProviderProfile] = []
    try:
        from providers import register_provider, _bound_home_layer
        from .config_scan import RouteTable

        table = RouteTable.from_config()
        layer = None
        try:
            layer, _, _ = _bound_home_layer()
        except Exception:
            pass

        all_slugs: list[str] = []
        for entry in table.all_entries:
            slug = entry.slug or f"advanced:{entry.provider_key}"
            all_slugs.append(slug)
            aliases = (entry.provider_key, slug)
            p = AdvancedProviderProfile(
                name=slug,
                aliases=aliases,
                display_name=entry.display_name,
                description=entry.display_name,
                base_url=entry.base_url,
                entry=entry,
            )
            register_provider(p)
            if layer is not None:
                layer.registry[p.name] = p
                for a in p.aliases:
                    layer.aliases[a] = p.name
            registered.append(p)

            try:
                import hermes_cli.main as hmain

                if hasattr(hmain, "_PROVIDER_MODEL_FLOWS"):
                    hmain._PROVIDER_MODEL_FLOWS[slug] = (
                        lambda c, m, a, s=slug: _model_flow_advanced(c, s, m, a)
                    )
                    hmain._PROVIDER_MODEL_FLOWS[entry.provider_key] = (
                        lambda c, m, a, s=slug: _model_flow_advanced(c, s, m, a)
                    )
            except Exception:
                pass

        base_aliases = ["advanced", "advanced-custom"] + all_slugs
        base_p = AdvancedProviderProfile(
            name="advanced",
            aliases=tuple(dict.fromkeys(base_aliases)),
            display_name="[ADVANCED] Advanced Custom Providers",
            description="[ADVANCED] Advanced Custom Providers",
            base_url="",
        )
        register_provider(base_p)
        if layer is not None:
            layer.registry[base_p.name] = base_p
            for a in base_p.aliases:
                layer.aliases[a] = base_p.name
        registered.append(base_p)

        try:
            from hermes_cli import models_catalog_static

            models_catalog_static.sync_plugin_provider_catalog()
        except Exception:
            pass

        logger.info(
            "advanced-providers: registered %d standalone advanced profiles (entries: %s)",
            len(registered),
            [p.name for p in registered],
        )
        return registered
    except Exception:
        logger.warning("advanced-providers: failed to register advanced profiles", exc_info=True)
        return registered


def ensure_model_provider_bridge() -> bool:
    """Ensure $HERMES_HOME/plugins/model-providers/hermes-provider-advanced exists.

    This ensures `hermes model` discovers advanced provider profiles even when
    the plugin is installed under custom-plugins/ or another sub-folder.
    """
    try:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
        mp_dir = home / "plugins" / "model-providers" / "hermes-provider-advanced"
        mp_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = mp_dir / "plugin.yaml"
        manifest_content = (
            "name: hermes-provider-advanced\n"
            "kind: model-provider\n"
            "version: 0.3.0\n"
            "description: Standalone advanced custom model provider discovery bridge\n"
            "author: Feather (翎羽)\n"
        )
        if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != manifest_content:
            manifest_path.write_text(manifest_content, encoding="utf-8")

        init_path = mp_dir / "__init__.py"
        init_content = (
            '"""Bridge loader for hermes-provider-advanced under model-providers."""\n'
            'from __future__ import annotations\n'
            'import logging\n'
            'import sys\n'
            'from pathlib import Path\n'
            'logger = logging.getLogger("hermes.advanced_providers.bridge")\n'
            'try:\n'
            '    from hermes_advlib.provider_profile import build_and_register_advanced_profile\n'
            '    build_and_register_advanced_profile()\n'
            'except Exception:\n'
            '    try:\n'
            '        here = Path(__file__).resolve()\n'
            '        cands = [\n'
            '            here.parent.parent / "custom-plugins" / "hermes-provider-advanced",\n'
            '            here.parent.parent / "hermes-provider-advanced",\n'
            '            here.parent.parent.parent / "hermes-provider-advanced",\n'
            '        ]\n'
            '        for cand in cands:\n'
            '            if (cand / "advlib").is_dir():\n'
            '                if str(cand) not in sys.path:\n'
            '                    sys.path.insert(0, str(cand))\n'
            '                break\n'
            '        from advlib.provider_profile import build_and_register_advanced_profile\n'
            '        build_and_register_advanced_profile()\n'
            '    except Exception as exc:\n'
            '        logger.debug("advanced provider bridge failed to load: %s", exc)\n'
        )
        if not init_path.is_file() or init_path.read_text(encoding="utf-8") != init_content:
            init_path.write_text(init_content, encoding="utf-8")

        return True
    except Exception:
        logger.debug("advanced-providers: ensure_model_provider_bridge failed (fail-open)", exc_info=True)
        return False
