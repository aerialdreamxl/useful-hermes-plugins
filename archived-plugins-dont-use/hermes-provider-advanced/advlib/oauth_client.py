"""OAuth client facade — the v3 §3.2 materialization layer.

For ``advanced_oauth`` entries the OpenAI client is built against the
ADAPTER's real upstream endpoint (never the ``platform://account``
placeholder), with:

- a fresh Bearer per request (token provider: account store + refresh on
  skew), injected via an httpx request hook (azure bearer-hook precedent:
  the SDK snapshots api_key at construction; the hook rewrites
  Authorization per request, which also beats client-level default headers
  because hooks run last) — risk 9's mitigation is structural here;
- fingerprint application per request (hermes/sub2api/cpa, §R6);
- wire-shape translation so the core's literal dispatch works unchanged:
  ``.chat.completions.create()`` on a Responses-wire platform (codex) is
  adapted; the result object is shaped like a chat completion so the core's
  chat-path response normalization keeps working.

Claude: the Anthropic-compatible surface of a claude account is served by
the anthropic SDK path (P3 wires build_anthropic_client through the same
token provider); against the OpenAI SDK, the platform's /v1 endpoint is
used directly when the adapter declares ``openai_surface``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from . import oauth_tokens

logger = logging.getLogger("hermes.advanced_providers")


class AccountNotFound(RuntimeError):
    """The referenced (platform, account) slot is absent — surface a pointer,
    never a silent direct connection or bare 401 (v3 §2 'explicit error')."""


class OAuthTokenContext:
    """Per-entry live-token provider: cache + skew refresh + store persist."""

    def __init__(self, platform: str, account_name: str, store,
                 proxy: str = "") -> None:
        self.platform = platform
        self.account_name = account_name
        self._store = store
        # Per-provider proxy (v3 advanced.proxy). Needed HERE because token
        # refresh is a NETWORK call of its own: on a proxied network the
        # oauth2.googleapis.com refresh fails outright without it, which
        # looked like "provider broken" and made the agent fall back.
        self.proxy = str(proxy or "").strip()
        self._lock = threading.Lock()
        self._cached: Optional[str] = None
        self._cache_exp: float = 0.0

    def token(self) -> str:
        """Current valid access token; refresh+persist when missing/near-expiry.

        Raises AccountNotFound when the slot is absent; RuntimeError when a
        refresh is required and fails (caller surfaces a pointer to login).
        """
        now = time.time()
        with self._lock:
            if self._cached and now < self._cache_exp - oauth_tokens.REFRESH_SKEW_SECONDS:
                return self._cached
            acct = self._store.get(self.platform, self.account_name)
            if acct is None:
                raise AccountNotFound(
                    f"advanced_oauth: account '{self.platform}://{self.account_name}' not found in "
                    f"the store — run: hermes advanced-providers oauth login")
            if oauth_tokens.needs_refresh(acct, now=now):
                refreshed = oauth_tokens.refresh_and_persist(
                    self.platform, self.account_name, self._store,
                    proxy_hint=self.proxy)
                if refreshed is None:
                    raise RuntimeError(
                        f"advanced_oauth: token refresh failed for "
                        f"'{self.platform}://{self.account_name}' — re-run: "
                        f"hermes advanced-providers oauth login")
                acct = refreshed
            token = str(acct.get("access_token") or "").strip()
            if not token:
                raise AccountNotFound(
                    f"advanced_oauth: account '{self.platform}://{self.account_name}' carries no "
                    f"access material — re-login")
            self._cached = token
            exp = oauth_tokens.parse_expiry(
                acct.get("expired") or acct.get("expires_at") or acct.get("expires_at_ms"))
            if exp is None:
                exp = oauth_tokens._jwt_expiry(token) or (now + 1500)
            self._cache_exp = float(exp)
            return token

    def peek_account(self) -> dict:
        acct = self._store.get(self.platform, self.account_name)
        return acct if isinstance(acct, dict) else {}

    def force_refresh(self) -> dict:
        """Rotate at/rt NOW regardless of expiry (CLI ``oauth refresh``).

        Drops the cached bearer, runs the refresher + persist path, returns
        the fresh account dict (redaction is the caller's job — never print
        tokens). Raises the same errors token() raises for missing material.
        """
        with self._lock:
            self._cached = None
            self._cache_exp = 0.0
            acct = self._store.get(self.platform, self.account_name)
            if acct is None:
                raise AccountNotFound(
                    f"advanced_oauth: account '{self.platform}://{self.account_name}' not found "
                    f"in the store — run: hermes advanced-providers oauth login")
            refreshed = oauth_tokens.refresh_and_persist(
                self.platform, self.account_name, self._store,
                proxy_hint=self.proxy)
            if refreshed is None:
                raise RuntimeError(
                    f"advanced_oauth: refresh failed for "
                    f"'{self.platform}://{self.account_name}' (no refresher, spent grant, or "
                    f"network error — see log)")
            return refreshed


def build_oauth_http_client(ctx: OAuthTokenContext, *, platform: str, fingerprint: str,
                            proxy: str = "", verify: Any = True,
                            async_mode: bool = False,
                            base_client_factory: Optional[Callable[..., Any]] = None) -> Any:
    """httpx client that stamps live auth + fingerprint identity per request.

    Transport construction rides the P1-validated ladder
    (``proxy_factory.build_proxy_client``: httpx 0.28 ``proxy=`` vs older
    ``proxies=``, keepalive limits, NO_PROXY reuse — never re-implemented
    here). The auth/fingerprint request hook is appended on top; with no
    proxy declared we still build through the factory with an empty proxy so
    timeout/limits stay identical to the P1 semantics.
    """
    def _on_request(request: Any) -> None:
        try:
            token = ctx.token()
        except Exception as exc:
            # Fail LOUD before the request leaves: a missing account must not
            # become an upstream 401 with a foreign identity.
            raise exc
        _strip_auth(request)
        request.headers["Authorization"] = f"Bearer {token}"
        from . import fingerprints
        from . import oplog

        phase = _phase_of(request)
        effective, skipped = fingerprints.apply(
            request, platform=platform, account=ctx.peek_account(),
            phase=phase, name=fingerprint, access_token=token)
        body = _peek_json_body(request)
        oplog.takeover(url=str(request.url), platform=platform,
                       account=ctx.account_name, phase=phase,
                       requested=str(fingerprint or "hermes").strip().lower(),
                       effective=effective or str(fingerprint or "hermes"),
                       model=str(body.get("model") or ""),
                       stream=body.get("stream"), skipped=skipped)

    if base_client_factory is None:
        from .proxy_factory import build_proxy_client

        base_client_factory = build_proxy_client
    client = base_client_factory(proxy=proxy or "", verify=verify, async_mode=async_mode)
    from . import oplog

    if client is not None:
        try:
            client.event_hooks["request"].append(_on_request)
            oplog.proxy_injected(proxy, platform=platform, account=ctx.account_name,
                                 scope="推理链路")
            oplog.client_ready(platform=platform, account=ctx.account_name,
                               requested=str(fingerprint or "hermes"),
                               effective=fingerprint_effective(platform, fingerprint),
                               proxy=proxy)
            return client
        except Exception:
            if not async_mode:
                try:
                    client.close()
                except Exception:
                    pass
    else:
        # A configured proxy that produced no client means this request goes
        # out DIRECT — the failure mode that looks like "the provider broke".
        oplog.proxy_skipped(proxy, platform=platform, account=ctx.account_name,
                            scope="推理链路", reason="代理客户端构建失败")
    # factory returned None (empty proxy, or httpx missing/incompatible):
    # build a DIRECT client here with the hook — same timeout/limits
    # constants as the P1 ladder (imported, not re-hardcoded) so proxy and
    # no-proxy oauth entries keep identical transport semantics.
    import httpx

    from .proxy_factory import (
        _KEEPALIVE_EXPIRY, _MAX_CONNECTIONS, _MAX_KEEPALIVE, _TIMEOUT_CONNECT,
        _TIMEOUT_POOL, _TIMEOUT_WRITE,
    )

    cls = httpx.AsyncClient if async_mode else httpx.Client
    return cls(
        event_hooks={"request": [_on_request]},
        limits=httpx.Limits(max_keepalive_connections=_MAX_KEEPALIVE,
                            max_connections=_MAX_CONNECTIONS,
                            keepalive_expiry=_KEEPALIVE_EXPIRY),
        timeout=httpx.Timeout(connect=_TIMEOUT_CONNECT, read=None,
                              write=_TIMEOUT_WRITE, pool=_TIMEOUT_POOL),
        verify=verify)


def _strip_auth(request: Any) -> None:
    for name in list(request.headers):
        if name.lower() in ("authorization", "x-api-key", "api-key"):
            del request.headers[name]


def _phase_of(request: Any) -> str:
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    if ("backend-api/codex" in path or path.endswith("/responses")
            or path.endswith("/chat/completions") or "v1internal:" in path
            or "loadCodeAssist" in path):
        return "inference"
    if "oauth" in path or "token" in path or "deviceauth" in path:
        return "auth"
    return "other"


def _peek_json_body(request: Any) -> dict:
    """Best-effort JSON body peek for the takeover log (model / stream).

    Never raises and never consumes anything the request needs: ``.content``
    is the already-serialized body, so this is a read, not a drain. Oversized
    bodies (image payloads) are skipped rather than parsed.
    """
    import json

    try:
        raw = request.content
        if not raw or len(raw) > 4 * 1024 * 1024:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def fingerprint_effective(platform: str, fingerprint: str) -> str:
    """Which scheme will really supply headers (resolver chain, no logging)."""
    try:
        from . import fingerprints

        _mod, effective, _skipped = fingerprints.resolve(platform, fingerprint)
        return effective or str(fingerprint or "hermes").strip().lower()
    except Exception:
        return str(fingerprint or "hermes").strip().lower()


# ------------------------------------------------------------------ facade

class _CompletionsFacade:
    """``.chat.completions.create(...)`` over a Responses-wire client."""

    def __init__(self, real_client: Any, ctx: OAuthTokenContext, platform: str,
                 force_stream: bool = False) -> None:
        self._real = real_client
        self._ctx = ctx
        self._platform = platform
        # None = undecided (try plain first, learn from a 400), True = this
        # upstream requires SSE (official ChatGPT codex backend), False = plain.
        self._requires_stream: "bool | None" = True if force_stream else None

    def _drain(self, resp_kwargs: dict, model: str) -> Any:
        events = list(self._real.responses.create(**{**resp_kwargs, "stream": True}))
        return _response_to_chat_object(_drain_responses_stream(events, model=model),
                                        model=model)

    def create(self, **kwargs: Any) -> Any:
        stream = bool(kwargs.pop("stream", False))
        resp_kwargs = _chat_kwargs_to_responses(kwargs)
        model = str(kwargs.get("model") or "")
        if stream:
            events = self._real.responses.create(**{**resp_kwargs, "stream": True})
            return _stream_events_to_chunks(events, model=model)
        if self._requires_stream:
            return self._drain(resp_kwargs, model)
        # Relays accept a plain Responses call; the official ChatGPT Codex
        # backend REJECTS it with 400 "Stream must be set to true" (live
        # 2026-10-30). Learn once, then stop asking.
        try:
            resp = self._real.responses.create(**resp_kwargs)
        except Exception as exc:
            if not _requires_stream_error(exc):
                raise
            logger.info("advanced-providers: %s requires stream=true upstream — "
                        "switching this client to SSE assembly", self._platform)
            self._requires_stream = True
            return self._drain(resp_kwargs, model)
        self._requires_stream = False
        return _response_to_chat_object(resp, model=model)


class _ChatShim:
    def __init__(self, completions: _CompletionsFacade) -> None:
        self.completions = completions


class OAuthResponsesClient:
    """OpenAI-client-shaped facade over Responses wire for advanced_oauth.

    The core's literal chat dispatch calls ``.chat.completions.create()``;
    this routes to ``.responses.create`` under the hood with shape
    translation. ``close()`` cascades so the client-ownership contract holds.
    """

    def __init__(self, real_client: Any, ctx: OAuthTokenContext, platform: str,
                 force_stream: bool = False) -> None:
        self._real = real_client
        self.chat = _ChatShim(_CompletionsFacade(real_client, ctx, platform,
                                                 force_stream=force_stream))
        self.responses = real_client.responses  # passthrough if ever needed
        self.api_key = "oauth-managed"
        self.base_url = getattr(real_client, "base_url", "")

    def close(self) -> None:
        try:
            self._real.close()
        except Exception:
            pass

    def is_closed(self) -> bool:
        # Method form mirrors the openai SDK contract the core probes
        # (_is_openai_client_closed); after a per-request close the cached
        # facade must report dead so the adapter evicts and rebuilds.
        try:
            return bool(self._real.is_closed())
        except Exception:
            return False


# ------------------------------------------------------- shape translation
#
# NOTE on sdk_transform_bypass: core moves bulk ``messages``/``tools`` into
# ``extra_body`` only when ``type(client.chat.completions).__module__``
# starts with "openai." — the gate deliberately skips non-SDK chat facades
# (its own docstring names the MoA aggregator as such). Our facade is not an
# openai-module class, so create() receives plain kwargs and no extra_body
# rescue is needed here.

def _chat_kwargs_to_responses(kw: dict) -> dict:
    msgs = kw.get("messages") or []
    instructions = ""
    rest = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") in ("system", "developer"):
            instructions += str(m.get("content") or "") + "\n"
        else:
            rest.append(m)
    out: dict[str, Any] = {
        "model": kw.get("model"),
        "instructions": instructions.strip(),
        "store": False,
    }
    # Core's own chat->Responses item converter (tool-call replay, reasoning
    # items, image parts) — same semantics as the codex_responses route.
    try:
        from agent.codex_responses_adapter import _chat_messages_to_responses_input

        out["input"] = _chat_messages_to_responses_input(rest)
    except Exception:
        out["input"] = rest  # core internals moved: keep plain passthrough
    if kw.get("max_tokens") is not None:
        out["max_output_tokens"] = kw["max_tokens"]
    if kw.get("temperature") is not None:
        out["temperature"] = kw["temperature"]
    tools = kw.get("tools")
    if tools:
        try:
            from agent.codex_responses_adapter import _responses_tools

            converted = _responses_tools(tools)
            out["tools"] = converted if converted else None
        except Exception:
            out["tools"] = [
                {"type": "function", "name": t["function"]["name"],
                 "description": t["function"].get("description", ""),
                 "parameters": t["function"].get("parameters") or {}}
                for t in tools if isinstance(t, dict) and t.get("type") == "function"
            ]
        if out.get("tools"):
            out["tool_choice"] = "auto"
    extra_body = kw.get("extra_body") or {}
    for key in ("reasoning", "prompt_cache_key", "include", "previous_response_id",
                "max_output_tokens", "temperature"):
        if key in extra_body and out.get(key) is None:
            out[key] = extra_body[key]
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _requires_stream_error(exc: Exception) -> bool:
    """True when the upstream rejected a non-stream request for stream-only policy."""
    text = ""
    try:
        text = str(exc)
    except Exception:
        return False
    low = text.lower()
    if "stream must be set to true" in low or "stream must be enabled" in low:
        return True
    status = getattr(exc, "status_code", None)
    return status == 400 and "stream" in low and "true" in low


def _drain_responses_stream(events: "list[Any]", *, model: str) -> Any:
    """Turn a buffered Responses SSE event list into one response-shaped object.

    Uses the core's own ``_CodexResponseAssembler`` (agent/codex_runtime.py) —
    the same assembly the codex_responses route relies on, so tool calls,
    reasoning deltas and terminal status behave identically. A local fallback
    keeps the facade alive if that internal ever moves.
    """
    try:
        from agent.codex_runtime import _CodexResponseAssembler

        asm = _CodexResponseAssembler(model=model, on_text_delta=None,
                                      on_reasoning_delta=None,
                                      on_commentary_message=None, on_first_delta=None)
        for ev in events:
            if asm.feed(ev):
                break
        return asm.result()
    except Exception:
        logger.debug("advanced-providers: core Responses assembler unavailable, "
                     "using the local fallback", exc_info=True)

    from types import SimpleNamespace

    def _field(obj: Any, name: str, default: Any = None) -> Any:
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name, default)
        return value if value is not None else default

    items: "list[Any]" = []
    text: "list[str]" = []
    completed = None
    for ev in events:
        etype = str(_field(ev, "type", "") or "")
        if etype == "response.output_item.done":
            item = _field(ev, "item")
            if item is not None:
                items.append(item)
        elif etype in ("response.output_text.delta", "response.text.delta"):
            text.append(str(_field(ev, "delta", "") or ""))
        elif etype == "response.completed":
            completed = _field(ev, "response")
    if completed is not None:
        return completed
    if text and not items:
        items = [SimpleNamespace(type="message", role="assistant", status="completed",
                                 content=[SimpleNamespace(type="output_text",
                                                          text="".join(text))])]
    return SimpleNamespace(model=model, output=items, status="completed", usage=None)


def _response_to_chat_object(resp: Any, *, model: str) -> Any:
    from types import SimpleNamespace

    def _f(obj: Any, name: str, default: Any = None) -> Any:
        """attr-or-dict field read: the core assembler can hand back raw dicts
        (relay/SSE paths), the SDK hands back objects."""
        if obj is None:
            return default
        value = getattr(obj, name, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(name, default)
        return default if value is None else value

    text_parts = []
    tool_calls = []
    for item in _f(resp, "output", None) or []:
        itype = _f(item, "type", None)
        if itype == "message":
            for part in _f(item, "content", None) or []:
                if _f(part, "type", "") == "output_text":
                    text_parts.append(_f(part, "text", "") or "")
        elif itype == "function_call":
            try:
                arguments = _f(item, "arguments", "") or "{}"
                if isinstance(arguments, str):
                    json.loads(arguments)
                else:
                    arguments = json.dumps(arguments)
            except Exception:
                arguments = "{}"
            tool_calls.append(SimpleNamespace(
                id=_f(item, "call_id", None) or f"call_{len(tool_calls)}",
                type="function",
                function=SimpleNamespace(name=_f(item, "name", ""), arguments=arguments)))
    content = "".join(text_parts).strip() or None
    finish = "tool_calls" if tool_calls else "stop"
    usage = _f(resp, "usage", None)
    chat_usage = None
    if usage is not None:
        chat_usage = SimpleNamespace(
            prompt_tokens=_f(usage, "input_tokens", 0) or 0,
            completion_tokens=_f(usage, "output_tokens", 0) or 0,
            total_tokens=(_f(usage, "input_tokens", 0) or 0) +
                         (_f(usage, "output_tokens", 0) or 0))
    message = SimpleNamespace(role="assistant", content=content,
                              tool_calls=tool_calls or None)
    return SimpleNamespace(
        id=getattr(resp, "id", None) or "chatcmpl-oauth", model=model,
        choices=[SimpleNamespace(index=0, message=message, finish_reason=finish)],
        usage=chat_usage)


def _new_assembler(model: str):
    """Core's Responses→Response assembler, or None when it moved/went away."""
    try:
        from agent.codex_runtime import _CodexResponseAssembler

        return _CodexResponseAssembler(model=model, on_text_delta=None,
                                       on_reasoning_delta=None,
                                       on_commentary_message=None, on_first_delta=None)
    except Exception:
        logger.debug("advanced-providers: core Responses assembler unavailable",
                     exc_info=True)
        return None


def _ev_field(event: Any, name: str, default: Any = None) -> Any:
    value = getattr(event, name, None)
    if value is None and isinstance(event, dict):
        value = event.get(name, default)
    return default if value is None else value


_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})


def _stream_events_to_chunks(events: Any, *, model: str):
    """Yield chat-shaped chunks from the Responses SSE event iterator.

    Text deltas stream through as they arrive (the point of streaming). The
    OpenAI tool-call surface is emitted ONCE at the terminal frame, assembled
    from the whole event stream by the core's own assembler — the codex
    backend streams function calls as ``output_item.*`` + arguments deltas,
    which a naive delta-passthrough silently drops (live proof 2026-10-30:
    tool-call turn produced zero chunks of interest and finish=stop, so the
    agent never called the tool).

    Generator (NOT context manager): the core chat streaming paths consume
    plain iterators (``for chunk in stream``).
    """
    from types import SimpleNamespace

    asm = _new_assembler(model)
    text_emitted = False
    fallback_calls: "list[Any]" = []  # used only when the core assembler is gone

    def _chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(index=0,
                                     delta=SimpleNamespace(
                                         role="assistant" if (content and not text_emitted) else None,
                                         content=content, tool_calls=tool_calls),
                                     finish_reason=finish_reason)],
            model=model, usage=usage)

    for ev in events:
        etype = str(_ev_field(ev, "type", "") or "")
        terminal = False
        if asm is not None:
            try:
                terminal = bool(asm.feed(ev))
            except Exception:
                logger.debug("advanced-providers: assembler feed failed", exc_info=True)
        terminal = terminal or etype in _TERMINAL_EVENTS

        if etype in ("response.output_text.delta", "response.text.delta"):
            piece = _ev_field(ev, "delta", "") or ""
            if piece:
                yield _chunk(content=piece)
                text_emitted = True
        elif etype == "response.output_item.done" and asm is None:
            item = _ev_field(ev, "item")
            if str(_ev_field(item, "type", "") or "") == "function_call":
                args = _ev_field(item, "arguments", "") or "{}"
                fallback_calls.append(SimpleNamespace(
                    index=len(fallback_calls),
                    id=str(_ev_field(item, "call_id", "") or _ev_field(item, "id", "") or ""),
                    type="function",
                    function=SimpleNamespace(
                        name=str(_ev_field(item, "name", "") or ""),
                        arguments=args if isinstance(args, str) else json.dumps(args))))
        elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            # Reasoning streams as its own channel; not model output.
            continue

        if not terminal:
            continue

        chat = None
        if asm is not None:
            try:
                chat = _response_to_chat_object(asm.result(), model=model)
            except Exception:
                logger.debug("advanced-providers: assembler result failed", exc_info=True)
        message = chat.choices[0].message if chat is not None else None
        tool_calls = None
        if message is not None and message.tool_calls:
            tool_calls = [
                SimpleNamespace(index=i, id=tc.id, type="function",
                                function=SimpleNamespace(name=tc.function.name,
                                                         arguments=tc.function.arguments))
                for i, tc in enumerate(message.tool_calls)
            ]
        leftover = None
        if not text_emitted and message is not None and message.content:
            leftover = message.content  # text we never streamed (e.g. text.delta only)
        if not tool_calls and fallback_calls:
            tool_calls = fallback_calls
        if tool_calls:
            finish = "tool_calls"
        elif etype == "response.failed":
            finish = "error"
        elif etype == "response.incomplete":
            finish = "length"
        else:
            finish = "stop"
        yield _chunk(content=leftover, tool_calls=tool_calls, finish_reason=finish,
                     usage=getattr(chat, "usage", None) if chat is not None else None)
        return
