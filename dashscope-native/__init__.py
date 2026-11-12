"""dashscope-native — Hermes provider plugin (api_mode: qwen_dashscope).

Zero core patches. Two official seams (S0 v3, PROMPT.md):
  1. register_transport("qwen_dashscope", ...) — dialect survives api_mode gates.
  2. ProviderProfile.create_client() — materializes the official dashscope
     Client on the native wire for dashscope://<region>[@<workspace>] URLs.

Scope: chat plane = METERED (pay-as-you-go) Bailian keys only; Token Plan ×
native chat stays undocumented upstream and out of scope (PROMPT.md O1).
The VOICE plane additionally carries a documented Token Plan lane
(PROMPT.md §11, provider name "dashscope-tp", official best-practices docs).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("hermes.dashscope_native")

PLUGIN_NAME = "dashscope-native"


def register(ctx) -> None:
    from . import ds_profile, ds_transport

    ds_transport.register()
    ds_profile.register()
    logger.info("%s: qwen_dashscope registered (native wire, zero core patches)", PLUGIN_NAME)

    # Voice plane (PROMPT.md §10): isolated — a voice failure must never
    # take down the chat seams registered above.
    try:
        from . import ds_voice

        ds_voice.register(ctx)
    except Exception:
        logger.warning("%s: voice plane registration failed (chat unaffected)",
                       PLUGIN_NAME, exc_info=True)

    # Token Plan voice lane (PROMPT.md §11): independent switches, independent
    # failure domain.
    try:
        from . import ds_voice_tp

        ds_voice_tp.register(ctx)
    except Exception:
        logger.warning("%s: token-plan voice lane registration failed (rest unaffected)",
                       PLUGIN_NAME, exc_info=True)
