"""ProviderTransport for api_mode ``qwen_dashscope``.

The dispatch path is the core's chat-literal route: kwargs arrive already
OpenAI-shaped, the DashScopeNativeClient facade translates them onto the
native wire, and responses come back canonical (the SDK normalizes native
choices itself). So request-side hooks are identity and response-side work
delegates to the core chat_completions transport — same pattern the
advanced_oauth dialect proved in production (advlib/oauth_runtime.py).

Every method the core calls on the registered class per turn is implemented
(validate_response / extract_cache_stats / map_finish_reason /
response_finish_reason) — a missing one is an AttributeError every turn.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.dashscope_native.transport")

API_MODE = "dashscope"  # v0.5 命名统一（PROMPT.md §3.1）：原 qwen_dashscope，config 无旧名引用，零迁移


def _chat_transport():
    from agent.transports import get_transport

    return get_transport("chat_completions")


class DashScopeNativeTransport:
    """Dialect registration so ``qwen_dashscope`` survives every api_mode gate."""

    API_MODE = API_MODE

    @property
    def api_mode(self) -> str:
        return API_MODE

    # -- request side: identity (facade owns wire translation) --------------
    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        return messages

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        return tools

    def build_kwargs(self, **kwargs: Any) -> Dict[str, Any]:
        return kwargs

    def preflight_kwargs(self, api_kwargs: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        return api_kwargs

    # -- response side -----------------------------------------------------
    def normalize_response(self, response: Any, **kwargs: Any) -> Any:
        t = _chat_transport()
        if t is None:
            return response
        return t.normalize_response(response, **kwargs)

    def validate_response(self, response: Any) -> bool:
        t = _chat_transport()
        if t is None:
            return response is not None
        try:
            return bool(t.validate_response(response))
        except Exception:  # fail-open: the model already answered
            logger.debug("qwen_dashscope validate_response delegation failed", exc_info=True)
            return response is not None

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        t = _chat_transport()
        if t is None or not hasattr(t, "extract_cache_stats"):
            return None
        try:
            return t.extract_cache_stats(response)
        except Exception:
            return None

    def map_finish_reason(self, raw_reason: str) -> str:
        return raw_reason  # SDK canonical choices already speak OpenAI vocabulary

    def response_finish_reason(self, response: Any) -> str:
        t = _chat_transport()
        if t is not None and hasattr(t, "response_finish_reason"):
            try:
                return t.response_finish_reason(response)
            except Exception:
                pass
        return "stop"


def register() -> bool:
    """Idempotent registration into the official transport registry."""
    try:
        from agent.transports import register_transport

        register_transport(API_MODE, DashScopeNativeTransport)
        logger.info("dashscope-native: registered transport api_mode %r", API_MODE)
        return True
    except Exception:
        logger.warning("dashscope-native: transport registration failed", exc_info=True)
        return False
