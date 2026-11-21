"""ProviderProfile overlay for qwen_dashscope — one profile serves every
dashscope:// URI (workspace/region ride inside the URL, so unlike the
advanced plugin there is no per-entry registration table).

Discovery: register_provider() by name/aliases, plus base_url prefix matching
(``_profile_for_base_url``) so a URL-only runtime resolves us too. The
profile's base_url is the scheme root ``dashscope://`` purely as the prefix
sentinel — the real gateway is built by the SDK from region+workspace.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from providers.base import ProviderProfile

from .ds_uri import SCHEME, DashScopeUriError, parse_dashscope_uri

logger = logging.getLogger("hermes.dashscope_native.profile")

PROFILE_NAME = "dashscope"  # v0.5 命名统一（PROMPT.md 裁定②/§6.4）：旧名与旧 aliases 作废


class DashScopeNativeProfile(ProviderProfile):
    api_mode = "dashscope"

    def __init__(self):
        # ProviderProfile is a dataclass: un-annotated class attrs do NOT become
        # field defaults — pass everything explicitly (same as advanced plugin).
        super().__init__(
            name=PROFILE_NAME,
            aliases=(PROFILE_NAME,),  # v0.5：旧 aliases 不保留（§6.4）
            display_name="[NATIVE] Alibaba Bailian — DashScope native (metered key)",
            description=(
                "Official dashscope SDK, native wire. base_url = "
                "dashscope://<region>[@<workspace-id>] (metered/pay-as-you-go key; "
                "Token Plan × native is undocumented — out of scope per O1)."
            ),
            api_mode="dashscope",
            env_vars=("DASHSCOPE_API_KEY",),
            base_url=SCHEME,  # prefix sentinel for _profile_for_base_url
            supports_streaming=True,
            supports_model_listing=False,  # workspace-gateway catalog parity is O3
            supports_vision=True,
        )

    def create_client(self, **client_kwargs: Any) -> Optional[Any]:
        base_url = str(client_kwargs.get("base_url") or "").strip()
        if not base_url.lower().startswith(SCHEME):
            return None  # not ours — standard construction path proceeds

        ref = parse_dashscope_uri(base_url)  # malformed → DashScopeUriError (loud, A5)
        assert ref is not None
        api_key = str(client_kwargs.get("api_key") or os.environ.get("DASHSCOPE_API_KEY") or "")
        if not api_key:
            raise RuntimeError(
                "dashscope-native: no API key for a dashscope:// route. "
                "Set DASHSCOPE_API_KEY to your METERED (pay-as-you-go) Bailian key "
                "(Token Plan keys are out of scope per PROMPT.md O1), or use `hermes auth add`."
            )
        from .ds_client import DashScopeNativeClient

        logger.info(
            "dashscope-native: building native SDK client region=%s workspace=%s",
            ref.region, ref.workspace or "<legacy shared gateway>",
        )
        return DashScopeNativeClient(
            api_key=api_key,
            region=ref.region,
            workspace=ref.workspace,
            timeout=client_kwargs.get("timeout"),
        )

    def fetch_models(self, *, api_key: Optional[str] = None,
                     base_url: Optional[str] = None, timeout: float = 8.0):
        if base_url is not None:
            base_url = str(base_url).strip()
            if base_url.lower().startswith(SCHEME):
                parse_dashscope_uri(base_url)
        # No catalog probe: native workspace gateway model listing is O3.
        # Keep the public hook bounded even when a future SDK implementation uses it.
        try:
            timeout_value = max(0.1, float(timeout))
        except (TypeError, ValueError):
            timeout_value = 8.0
        del api_key, base_url, timeout_value
        return None


def register() -> Optional[DashScopeNativeProfile]:
    """Register the profile by name+aliases (idempotent replace semantics)."""
    try:
        from providers import register_provider

        profile = DashScopeNativeProfile()
        register_provider(profile)
        # Keep the bound home layer's alias map in sync (same as advanced plugin).
        try:
            from providers import _bound_home_layer

            layer, _, _ = _bound_home_layer()
            if layer is not None:
                layer.registry[profile.name] = profile
                for alias in profile.aliases:
                    layer.aliases[alias] = profile.name
        except Exception:
            pass
        logger.info("dashscope-native: provider profile %r registered", PROFILE_NAME)
        return profile
    except Exception:
        logger.warning("dashscope-native: profile registration failed", exc_info=True)
        return None
