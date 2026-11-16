"""advanced_oauth runtime — transport registration + entry materialization.

Two seams, both official plugin surfaces (zero core patches):

1. ``register_transport("advanced_oauth", AdvancedOAuthTransport)`` makes
   the api_mode a REGISTERED dialect — every core gate that validates
   api_mode strings consults ``registered_api_modes()`` (transports/__init__
   docstring says this seam exists exactly for out-of-tree dialects), so the
   mode survives instead of being rewritten. Dispatch then takes the
   chat-literal route (the mode is not a known literal), and the platform
   facade in ``oauth_client`` converts chat->Responses where the platform
   speaks Responses — normalization here sniffs the raw shape.

2. Client materialization happens in the model-provider overlay's
   ``create_client`` (main agent + aux share the seam) and, for the
   anthropic route, in the P2 ``_new_sdk_client`` identity-checked wrapper.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("hermes.advanced_providers")

API_MODE = "advanced_oauth"

_registered = False


def ensure_registered() -> bool:
    """Register the dialect transport (idempotent; safe at import or lazy).

    Respects ADVANCED_DISABLE_OAUTH: the mode is not registered at all, so a
    stale ``api_mode: advanced_oauth`` config entry behaves like any unknown
    mode instead of half-working through a live transport.
    """
    global _registered
    if _registered:
        return True
    try:
        from . import oauth_gate

        if oauth_gate.oauth_disabled():
            logger.warning("advanced-providers: transport NOT registered — %s",
                           oauth_gate.DISABLED_MSG)
            return False
    except Exception:
        pass
    try:
        from agent.transports import register_transport

        register_transport(API_MODE, AdvancedOAuthTransport)
        _registered = True
        logger.info("advanced-providers: registered transport api_mode %r", API_MODE)
        return True
    except Exception as exc:
        logger.warning("advanced-providers: transport registration failed: %s", exc)
        return False


class AdvancedOAuthTransport:
    """Shape-sniffing dispatcher for advanced_oauth entries.

    ``build_kwargs``: identity — the agent already assembled chat-completions
    kwargs (advanced_oauth is not a literal-gated mode) and the platform
    facade performs any wire conversion inside create().

    ``normalize_response``: a Responses object (has ``output``, lacks
    ``choices``) is delegated to the core Codex transport; a chat object to
    the chat_completions transport; anything else passes through as a text
    completion so a wrong-shape surprise never crashes a turn.
    """

    API_MODE = API_MODE

    def build_kwargs(self, **kwargs: Any) -> dict:
        # Passthrough: kwargs are already wire-ready for the facade/chat path.
        return kwargs

    # -- response-side surface the core calls on EVERY turn ------------------
    # The core does not go through ProviderTransport's defaults here: it holds
    # the registered CLASS and calls the methods directly
    # (turn_recovery.validate_response_shape -> agent._get_transport()
    # .validate_response(response)). A missing method is an AttributeError on
    # every single turn — that was the live "smoke test fails" bug of
    # 2026-10-30 (retried 3x, then the provider looked broken).

    def _delegate_for(self, response: Any):
        """The core transport that understands this response's shape."""
        from agent.transports import get_transport

        return get_transport("codex_responses" if _looks_like_responses(response)
                             else "chat_completions")

    def validate_response(self, response: Any) -> bool:
        """Structural validity via the shape's own transport.

        Fail-open by policy: if delegation itself breaks (core internals moved),
        report valid rather than block a turn the model already answered.
        """
        t = self._delegate_for(response)
        if t is None:
            return response is not None
        try:
            return bool(t.validate_response(response))
        except Exception:
            logger.debug("advanced-providers: validate_response delegation failed",
                         exc_info=True)
            return response is not None

    def extract_cache_stats(self, response: Any) -> Optional[dict]:
        t = self._delegate_for(response)
        if t is None or not hasattr(t, "extract_cache_stats"):
            return None
        try:
            return t.extract_cache_stats(response)
        except Exception:
            return None

    def map_finish_reason(self, raw_reason: str) -> str:
        """Passthrough: the platform facade already speaks OpenAI vocabulary
        (chat merge / responses translation happen inside create())."""
        return raw_reason

    def response_finish_reason(self, response: Any) -> str:
        """Only the anthropic route asks for this; delegate when possible."""
        t = self._delegate_for(response)
        if t is None or not hasattr(t, "response_finish_reason"):
            return "stop"
        try:
            return t.response_finish_reason(response)
        except Exception:
            return "stop"

    def preflight_kwargs(self, api_kwargs: Any, **kwargs: Any) -> Any:
        """Identity. The core only calls this for literal ``codex_responses``;
        advanced_oauth has no preflight of its own — the facade already built
        wire-ready kwargs."""
        return api_kwargs

    def normalize_response(self, response: Any, **kwargs: Any):
        from agent.transports import get_transport

        obj = response
        # SDK stream wrappers and dict-ish surprises: hand back something the
        # chat normalizer tolerates rather than raising.
        if _looks_like_responses(obj):
            t = get_transport("codex_responses")
        else:
            t = get_transport("chat_completions")
        if t is None:  # core internals moved; degrade to raw pass
            return obj
        try:
            return t.normalize_response(obj, **kwargs)
        except Exception:
            logger.debug("advanced-providers: normalize delegation failed", exc_info=True)
            return obj


def _looks_like_responses(obj: Any) -> bool:
    return (hasattr(obj, "output") and not hasattr(obj, "choices")
            and not isinstance(obj, dict)) or (
            isinstance(obj, dict) and "output" in obj and "choices" not in obj)


# ---------------------------------------------------------------- materialize

def resolve_route(base_url: str) -> tuple[str, str]:
    """``platform://account`` -> (platform, decoded account name)."""
    from .config_scan import parse_oauth_ref_from_url

    return parse_oauth_ref_from_url(base_url)


def build_client_for_ref(base_url: str, *, api_key: str = "",
                         request_timeout: Optional[float] = None,
                         extra: Optional[dict] = None) -> Any:
    """Return a live-token client for an advanced_oauth base_url, or None
    when this base_url is not ours (caller falls through to normal path).

    ``extra`` may carry the entry dict (model/provider fields from config) so
    adapters can read inference_base_url/request_timeout without re-reading
    config (cheap re-read happens when absent).
    """
    from .adapters import get as get_adapter
    from .config_scan import RouteTable
    from .account_store import AccountStore

    platform, account_name = resolve_route(base_url)
    if not platform:
        return None
    adapter = get_adapter(platform)
    if adapter is None:
        from .adapters import AdapterUnavailable

        raise AdapterUnavailable(
            f"advanced_oauth: platform '{platform}' is not enabled — re-enable via "
            f"ADVANCED_ADAPTERS/plugin settings or remove the provider entry")
    table = RouteTable.from_config()
    entry = table.lookup_oauth_ref(base_url)
    if entry is None:
        # create_client seam reached with an oauth ref that config no longer
        # declares (mid-session config edit): treat as not-ours, None.
        return None
    merged_entry = dict(extra or {})
    merged_entry.setdefault("model", entry.model)
    merged_entry.setdefault("inference_base_url", "")
    merged_entry.setdefault("request_timeout", request_timeout)

    store = AccountStore()
    fingerprint = entry.fingerprint or "hermes"
    proxy = entry.proxy or ""

    return adapter.build_client(merged_entry, account_name, store,
                                fingerprint=fingerprint, proxy=proxy)
