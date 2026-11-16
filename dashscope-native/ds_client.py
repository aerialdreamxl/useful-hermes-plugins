"""Thin forward-only wrapper around the official dashscope.Client (native wire).

Contract (PROMPT.md §3.1/1b + S0 v3):
- The object handed to Hermes IS a dashscope.Client at ``protocol="native"``
  (the SDK default) wrapped ONLY for kwargs translation. ``protocol`` is never
  assigned anywhere in this package; A8 wire proof guards that.
- Hermes' chat-literal dispatch builds OpenAI-wire kwargs. The openai SDK
  tolerates unknown keys (extra_body); dashscope 2.0.4 ``create()`` is a strict
  keyword-only signature → unknown keys TypeError. This wrapper maps the known
  keys, lifts native-only extras into named args, and forwards everything else
  through the SDK's own ``parameters=`` dict (the native full-parameter escape
  hatch documented in PROMPT.md §5.1).
- Response usage is normalized to OpenAI vocabulary (prompt_tokens/
  completion_tokens/total_tokens) while every native counter is preserved
  (input_tokens/output_tokens/details stay readable) — Hermes' normalize_usage
  reads the chat shape for unknown modes, so the mapping must happen here.
- Upstream error text is untrusted input: every exception message leaves this
  module through ``sanitize_upstream_error_body`` (status code and error type
  kept, body truncated to 800 chars, control/ANSI and template markers broken).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("hermes.dashscope_native")

# dashscope 2.0.4 Completions.create() named parameters (besides model/messages/
# stream/timeout which are handled explicitly). Verified against the wheel.
_SDK_NAMED_ARGS = frozenset({
    "tools", "temperature", "top_p", "max_tokens", "stop", "seed",
    "repetition_penalty", "presence_penalty", "enable_thinking",
    "response_format", "top_k", "tool_choice", "n", "logprobs",
    "top_logprobs", "thinking_budget", "enable_search", "search_options",
    "parallel_tool_calls", "incremental_output",
})

# OpenAI-wire-only keys that have no native meaning; dropped, never forwarded
# into ``parameters`` (native would 400 on unknown keys).
_OPENAI_ONLY_KEYS = frozenset({
    "stream_options",  # native streams always carry usage on the last chunk
    "frequency_penalty", "user", "metadata", "modalities", "audio",
    "logit_bias", "service_tier", "store", "prediction", "web_search_options",
})


# ------------------------------------------------- 上游错误体净化（F21）
# 上游 HTTP 错误体是第三方输入：逐字拼进异常会顺着 工具输出 → 模型上下文 变成
# 二次注入面（模板标记/伪指令）。统一先净化再拼，状态码与错误类型一律保留。
_UPSTREAM_UNTRUSTED_TAG = "[上游错误·不可信]"
ERROR_BODY_LIMIT = 800

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][0-2]"
)
# 控制字符：保留 \t 与 \n，其余（含 NUL/ESC 残留/BEL）全剔
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# 聊天模板/工具协议标记：打断成非标记形态（`[[` → `[ [`），可读性不变
_TEMPLATE_MARKERS = (
    ("[[", "[ ["), ("]]", "] ]"),
    ("{{", "{ {"), ("}}", "} }"),
    ("<|", "< |"), ("|>", "| >"),
)


def sanitize_upstream_error_body(body: Any, limit: int = ERROR_BODY_LIMIT) -> str:
    """净化上游错误体：去 ANSI/控制符、打断模板标记、截断到 limit 字符。

    只做"不可信文本"该做的事，不解析、不改写语义——状态码/错误类型由调用方
    另行拼进文案，不从这里消失。
    """
    if body is None:
        return ""
    text = body if isinstance(body, str) else str(body)
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_CHARS_RE.sub("", text)
    for marker, broken in _TEMPLATE_MARKERS:
        text = text.replace(marker, broken)
    try:
        limit = max(0, int(limit or 0))
    except (TypeError, ValueError):
        limit = ERROR_BODY_LIMIT
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "…(已截断)"
    return text


def upstream_error_message(
    status: Any = None, body: Any = "", kind: str = ""
) -> str:
    """拼上游错误文案：`[上游错误·不可信] HTTP <code> (<type>): <净化后的体>`。"""
    head = _UPSTREAM_UNTRUSTED_TAG
    if status not in (None, ""):
        head = f"{head} HTTP {status}"
    if kind:
        head = f"{head} ({kind})"
    detail = sanitize_upstream_error_body(body)
    return f"{head}: {detail}" if detail else f"{head}: (空错误体)"


def sanitize_exception_text(exc: BaseException) -> str:
    """把异常文案净化成可安全放进工具输出的文本（保留状态码/异常类型）。"""
    try:
        raw = str(exc)
    except Exception:
        raw = ""
    if _UPSTREAM_UNTRUSTED_TAG and _UPSTREAM_UNTRUSTED_TAG in raw[:40]:
        return raw  # 已净化过：幂等，避免前缀叠两层
    status = getattr(exc, "status_code", None)
    if status in (None, ""):
        status = getattr(exc, "status", None)
    if status in (None, ""):
        status = getattr(exc, "code", None)
    return upstream_error_message(status, raw, type(exc).__name__)


def scrub_exception_message(exc: BaseException) -> None:
    """就地改写异常文案：类型与属性不动，宿主仍能按类型/状态码分类重试。"""
    try:
        cleaned = sanitize_exception_text(exc)
        if not cleaned or cleaned == str(exc):
            return
        if isinstance(getattr(exc, "message", None), str):
            try:
                exc.message = cleaned  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            exc.args = (cleaned,)  # type: ignore[assignment]
        except Exception:
            pass
    except Exception:  # 净化本身绝不许改变原异常
        logger.debug("dashscope-native: exception scrub failed", exc_info=True)


# --------------------------------------------------------------- usage vocab

def normalize_native_usage(usage: Any) -> Any:
    """Add OpenAI aliases to a native usage mapping, preserving every counter.

    Native: input_tokens/output_tokens/total_tokens (+ *_details bags).
    Aliases added: prompt_tokens/completion_tokens (+ total fallback sum).
    """
    if usage is None:
        return None
    if isinstance(usage, dict):
        merged = dict(usage)
    else:  # pydantic Usage / SimpleNamespace — read attrs, keep extras
        merged = dict(getattr(usage, "__dict__", {}) or {})
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if key not in merged:
                merged[key] = getattr(usage, key, 0) or 0
        extra = getattr(usage, "model_extra", None)
        if isinstance(extra, dict):
            merged.update(extra)
    input_tokens = merged.get("input_tokens") or 0
    output_tokens = merged.get("output_tokens") or 0
    merged.setdefault("prompt_tokens", input_tokens)
    merged.setdefault("completion_tokens", output_tokens)
    merged["total_tokens"] = merged.get("total_tokens") or (input_tokens + output_tokens)
    return merged


def _with_normalized_usage(response: Any) -> Any:
    """model_copy so choices/message objects stay the SDK's own models."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return response
    try:
        return response.model_copy(update={"usage": normalize_native_usage(usage)})
    except Exception:
        logger.debug("dashscope-native: usage copy failed; returning raw response", exc_info=True)
        return response


class _ChunkStream:
    """Iterate SDK chunks, normalizing usage on any chunk that carries it."""

    def __init__(self, stream: Any):
        self._stream = stream
        self._iter = iter(stream)  # ChatStream is iterable, not itself an iterator

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        return _with_normalized_usage(next(self._iter))

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------- message parts

def _convert_part(part: Any) -> Any:
    """OpenAI content part → the SDK's native intermediate form.

    dashscope native._translate_part understands {type:text/image/audio/video}
    with url/data/frames; Hermes sends image_url/input_audio/video_url shapes.
    Hint keys (min_pixels/fps/...) pass through untouched.
    """
    if not isinstance(part, dict):
        return part
    ptype = part.get("type")
    if ptype == "image_url":
        url = (part.get("image_url") or {}).get("url")
        out = {k: v for k, v in part.items() if k not in ("type", "image_url")}
        out["type"] = "image"
        out["url"] = url
        return out
    if ptype == "input_audio":
        audio = part.get("input_audio") or {}
        out = {k: v for k, v in part.items() if k not in ("type", "input_audio")}
        out["type"] = "audio"
        if audio.get("data"):
            out["data"] = audio["data"]
            if audio.get("format"):
                out["format"] = audio["format"]
        else:
            out["url"] = audio.get("url")
        return out
    if ptype == "video_url":
        url = (part.get("video_url") or {}).get("url")
        out = {k: v for k, v in part.items() if k not in ("type", "video_url")}
        out["type"] = "video"
        out["url"] = url
        return out
    if ptype == "video" and isinstance(part.get("video"), list):
        # OpenAI "image list as video" → SDK frames form.
        out = {k: v for k, v in part.items() if k not in ("video",)}
        out["frames"] = part["video"]
        out.setdefault("type", "video")
        return out
    return part


def _convert_messages(messages: Any) -> Any:
    if not isinstance(messages, list):
        return messages
    converted = []
    for msg in messages:
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            msg = dict(msg)
            msg["content"] = [_convert_part(p) for p in msg["content"]]
        converted.append(msg)
    return converted


# ---------------------------------------------------------------- the client

class DashScopeNativeClient:
    """OpenAI-client-shaped *handle* whose create() is dashscope native, always.

    Only three OpenAI-surface attributes are emulated for the host's client
    contract (api_key/base_url for display+rebuild, close/is_closed for the
    ownership probes). Everything else is the real SDK object.
    """

    def __init__(self, *, api_key: str, region: str, workspace: Optional[str],
                 timeout: Optional[float] = None, http_client: Any = None):
        try:
            import dashscope  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "dashscope-native: official SDK missing — install with "
                "`pip install dashscope>=2.0.4` (Hermes venv), then restart."
            ) from exc
        from importlib.metadata import version as pkg_version
        from .ds_uri import VALID_REGIONS

        ver = pkg_version("dashscope")
        if tuple(int(x) for x in ver.split(".")[:3]) < (2, 0, 4):
            raise RuntimeError(
                f"dashscope-native: SDK {ver} is the legacy 1.x shape; "
                "this mode requires dashscope>=2.0.4 (pip install -U dashscope)."
            )
        if region not in VALID_REGIONS:  # defense in depth; ds_uri already gates
            raise RuntimeError(f"dashscope-native: invalid region {region!r}")

        import dashscope

        client_kwargs: Dict[str, Any] = {"api_key": api_key, "region": region}
        if workspace:
            client_kwargs["workspace"] = workspace
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        if http_client is not None:  # test seam (MockTransport wire capture)
            client_kwargs["http_client"] = http_client
        # protocol deliberately NOT passed → SDK default "native".
        self._sdk = dashscope.Client(**client_kwargs)
        self.api_key = api_key
        self.base_url = f"dashscope://{region}" + (f"@{workspace}" if workspace else "")
        self.region = region
        self.workspace = workspace

    # -- Hermes client-ownership contract ---------------------------------
    def close(self) -> None:
        try:
            self._sdk.close()
        except Exception:
            logger.debug("dashscope-native: sdk close failed", exc_info=True)

    def is_closed(self) -> bool:
        return False  # SDK clients are recreated per request; no cached-dead state

    # -- the one endpoint the host calls ----------------------------------
    def create(self, **kwargs: Any) -> Any:
        return self.chat.completions.create(**kwargs)

    @property
    def chat(self):
        return _ChatView(self)

    # -- kwargs translation ------------------------------------------------
    def _translate_kwargs(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        kw = dict(kwargs)
        call: Dict[str, Any] = {
            "model": kw.pop("model"),
            "messages": _convert_messages(kw.pop("messages")),
        }
        if kw.get("stream"):
            call["stream"] = True
        kw.pop("stream", None)
        timeout = kw.pop("timeout", None)
        if timeout is not None:
            call["timeout"] = timeout

        parameters: Dict[str, Any] = {}
        extra_body = kw.pop("extra_body", None) or {}
        # extra_body carries native-only keys on the OpenAI-wire path.
        for key, value in {**kw, **extra_body}.items():
            if value is None:
                continue
            if key in _OPENAI_ONLY_KEYS:
                logger.debug("dashscope-native: dropped openai-only kwarg %r", key)
                continue
            if key in _SDK_NAMED_ARGS:
                call[key] = value
            else:
                parameters[key] = value  # native full-parameter escape hatch
        if parameters:
            call["parameters"] = parameters
        return call

    def _create(self, **kwargs: Any) -> Any:
        call = self._translate_kwargs(kwargs)
        try:
            response = self._sdk.chat.completions.create(**call)
        except Exception as exc:  # 上游错误体逐字进异常 = 二次注入面（F21）
            scrub_exception_message(exc)  # 类型/状态码不变，只换文案
            raise
        if call.get("stream"):
            return _ChunkStream(response)
        return _with_normalized_usage(response)


class _ChatView:
    def __init__(self, owner: DashScopeNativeClient):
        self.completions = _CompletionsView(owner)


class _CompletionsView:
    def __init__(self, owner: DashScopeNativeClient):
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        return self._owner._create(**kwargs)
