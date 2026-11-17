"""dashscope-native v0.5 — one plugin, every Bailian (DashScope) plane.

Merged from t2i-bailian (image) and web-dashscope (web MCP), per PROMPT.md
v0.5 (owner's exp.yaml settings tree). Zero core patches. Planes:
  chat  : register_transport("dashscope") + ProviderProfile.create_client()
          (S0 v3 anti-shell seam — official dashscope SDK Client body)
  voice : metered SDK lane (provider "dashscope") + Token Plan REST lane
          (provider "dashscope-tp"), tp_ prefixed per-lane param overrides
  t2i   : one provider class, two variants ("dashscope" / "dashscope-tp")
  web   : EnhancedSearch/WebFetch MCP ("dashscope")

Settings: shared top-level base_url/key_env + tp_base_url/tp_key_env; per
block enabled = metered variant, enabled_tp = TP variant. Seeding happens
ONCE here (ds_settings.seed) before any plane registers. Each plane gets an
isolated try block — one plane's failure must never take down the others
(PROMPT.md §1; V2: SDK missing must not cascade to t2i/web).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("hermes.dashscope_native")

PLUGIN_NAME = "dashscope-native"


def register(ctx) -> None:
    from . import ds_settings

    ds_settings.seed(ctx)

    # Chat plane: transport + profile seams (v0.2 isolation mode kept).
    if bool(ctx.get_config("model_api_mode.enabled", True)):
        try:
            from . import ds_profile, ds_transport

            ds_transport.register()
            ds_profile.register()
            logger.info("%s: chat plane registered (api_mode/profile 'dashscope')", PLUGIN_NAME)
        except Exception:
            logger.warning("%s: chat plane registration failed (rest unaffected)",
                           PLUGIN_NAME, exc_info=True)
    else:
        logger.debug("%s: chat plane disabled (model_api_mode.enabled=false)", PLUGIN_NAME)

    # Voice plane, metered lane (PROMPT.md §4/§10).
    try:
        from . import ds_voice

        ds_voice.register(ctx)
    except Exception:
        logger.warning("%s: voice plane registration failed (rest unaffected)",
                       PLUGIN_NAME, exc_info=True)

    # Voice plane, Token Plan lane (PROMPT.md §4/§11): independent switches.
    try:
        from . import ds_voice_tp

        ds_voice_tp.register(ctx)
    except Exception:
        logger.warning("%s: token-plan voice lane registration failed (rest unaffected)",
                       PLUGIN_NAME, exc_info=True)

    # t2i plane (merged from t2i-bailian, §6.1): stdlib-only, must survive
    # a missing dashscope SDK — that independence is acceptance item V2.
    try:
        from . import ds_image

        ds_image.register(ctx)
    except Exception:
        logger.warning("%s: t2i plane registration failed (rest unaffected)",
                       PLUGIN_NAME, exc_info=True)

    # Web plane (merged from web-dashscope, §6.2): stdlib-only, same rule.
    try:
        from . import ds_web

        ds_web.register(ctx)
    except Exception:
        logger.warning("%s: web plane registration failed (rest unaffected)",
                       PLUGIN_NAME, exc_info=True)
