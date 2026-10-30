"""hermes-provider-advanced — agent-side half.

Registers ``hermes advanced-providers`` (status / selfcheck; ``oauth`` verbs
arrive in P3). The transport half is the sibling user-layer model-provider
overlay (``model-providers/hermes-advanced-custom/``); both share ``advlib``.
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

    # P2 seams (identity-checked wrappers — core drift shows as a WARNING,
    # never a breakage). Installed lazily-safe: any failure keeps core
    # behaviour untouched.
    try:
        from .advlib import transport_patches

        results = transport_patches.install()
        logger.info("%s: P2 seams %s", PLUGIN_NAME, results)
    except Exception:
        logger.warning("%s: P2 seam install failed (core behaviour kept)",
                       PLUGIN_NAME, exc_info=True)

    # P3 dialect registration: cheap and side-effect-free (one registry
    # entry). Client materialization itself stays lazy in the overlay's
    # create_client hook — nothing here touches accounts or the network.
    try:
        from .advlib import oauth_runtime

        oauth_runtime.ensure_registered()
    except Exception:
        logger.warning("%s: advanced_oauth transport registration failed "
                       "(oauth entries will surface as errors)", PLUGIN_NAME,
                       exc_info=True)
