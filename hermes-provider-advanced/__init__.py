"""hermes-provider-advanced — standalone advanced custom model provider.

Registers:
1. Standalone provider profile `name="advanced:<key>"` with display names
   `[ADVANCED]<providers-advanced.<key>.name>` for all entries in `providers-advanced:`.
2. CLI commands `hermes advanced-providers` (status / selfcheck / oauth).
3. P2 seams and P3 OAuth transport dialect for advanced_oauth routes.
4. Auto-bridge generator to ensure discovery by `hermes model`.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("hermes.advanced_providers")

PLUGIN_NAME = "hermes-provider-advanced"
CLI_NAME = "advanced-providers"


def register(ctx) -> None:
    from .cli import build_parser, run_command

    ctx.register_cli_command(
        CLI_NAME,
        "Inspect/manage per-provider advanced: settings (proxy routing, OAuth accounts).",
        build_parser,
        run_command,
        description="advanced: blocks on custom providers — proxy (P1), transport "
                    "patches (P2), oauth account store (P3).",
    )
    logger.info("%s: registered CLI command `%s`", PLUGIN_NAME, CLI_NAME)

    # Standalone Model-Provider Profile registration & bridge
    try:
        from .advlib.provider_profile import build_and_register_advanced_profile, ensure_model_provider_bridge

        ensure_model_provider_bridge()
        build_and_register_advanced_profile()
    except Exception:
        logger.warning("%s: standalone advanced profile registration failed", PLUGIN_NAME, exc_info=True)

    # OpenRouter 隐式兜底闸门
    try:
        from .advlib import fallback_guard

        fallback_guard.configure(ctx)
    except Exception:
        logger.debug("%s: fallback guard ctx binding failed (fail-open)", PLUGIN_NAME,
                     exc_info=True)

    # P2 seams
    try:
        from .advlib import transport_patches

        results = transport_patches.install()
        logger.info("%s: P2 seams %s", PLUGIN_NAME, results)
    except Exception:
        logger.warning("%s: P2 seam install failed (core behaviour kept)",
                       PLUGIN_NAME, exc_info=True)

    # P3 dialect registration
    try:
        from .advlib import oauth_runtime

        oauth_runtime.ensure_registered()
    except Exception:
        logger.warning("%s: advanced_oauth transport registration failed "
                       "(oauth entries will surface as errors)", PLUGIN_NAME,
                       exc_info=True)


# Auto-register profile and bridge on import if possible (fail-open)
try:
    from .advlib.provider_profile import build_and_register_advanced_profile, ensure_model_provider_bridge

    ensure_model_provider_bridge()
    build_and_register_advanced_profile()
except Exception:
    pass
