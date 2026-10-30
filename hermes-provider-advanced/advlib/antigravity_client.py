"""The OpenAI-client-shaped facade that runs chat/completions over the
Antigravity (cloudcode-pa) wire.

Construction mirrors adapters/codex.py's build_openai_client (store-path-keyed
cache, OAuthTokenContext for live tokens, P1-built proxy transport) but the
"SDK client" underneath is replaced by direct httpx POSTs through the SAME
oauth http client — its request hook stamps Authorization + fingerprint
headers on every call, exactly like the SDK path would.

Route decisions replicated from CPA executor (spec §1/§8):
  base       = account base_url/antigravity_base_url else daily cloudcode-pa
  model      = "(effort)" suffix stripped; suffix seeds thinkingLevel
  family     = claude -> always SSE (merged for stream=False); gemini -> plain
  project    = account.project_id else loadCodeAssist (+onboardUser poll once)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Optional

from .antigravity_infer import derive_session_id, translate_request, uses_stream_route
from .antigravity_resp import (
    StreamState, dict_to_ns, iter_sse_json, merge_stream_to_nonstream, nonstream_to_chat,
)
from .oauth_client import AccountNotFound, OAuthTokenContext, build_oauth_http_client

logger = logging.getLogger("hermes.advanced_providers")

GENERATE_PATH = "/v1internal:generateContent"
STREAM_PATH = "/v1internal:streamGenerateContent?alt=sse"
LOAD_PATH = "/v1internal:loadCodeAssist"
ONBOARD_PATH = "/v1internal:onboardUser"

# The consumer cloudaicompanionProject literal Google hands personal accounts
# (live-verified 2026-10-29: loadCodeAssist -> "aicode-consumers").
DEFAULT_PROJECT = "aicode-consumers"

# UA floor: Cloud Code rejects newer models below 2.23.0 (spec §3, §11).
_NATIVE_UA = ("antigravity/hub/{version} (aidev_client; os_type={os}; arch={arch})")
_OS, _ARCH = "windows", "amd64"
_DEFAULT_VERSION = "2.24.0"


def _native_ua(account: dict) -> str:
    ua = str(account.get("user_agent") or "").strip()
    if ua.lower().startswith("antigravity"):
        return ua.split(" google-api-nodejs-client/")[0]
    if ua:
        return ua
    version = str(account.get("antigravity_version") or _DEFAULT_VERSION)
    return _NATIVE_UA.format(version=version, os=_OS, arch=_ARCH)


def strip_effort(model: str) -> "tuple[str, str]":
    m = re.match(r"^(.*?)\(([^)]+)\)\s*$", model or "")
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return (model or ""), ""


class AntigravityFacade:
    """chat.completions.create() -> v1internal, with live-token transport."""

    def __init__(self, *, platform: str, account_name: str, store, ctx: OAuthTokenContext,
                 fingerprint: str, proxy: str, timeout: float,
                 entry: "Optional[dict]" = None) -> None:
        self._platform = platform
        self._account_name = account_name
        self._store = store
        self._ctx = ctx
        self._fingerprint = fingerprint
        self._proxy = proxy
        self._timeout = timeout
        self._entry = entry or {}
        self.api_key = "oauth-managed"
        self.base_url = f"{platform}://{account_name}"
        self.chat = _ChatShim(_Completions(self))
        self.responses = None  # parity attr; core never uses it on this route
        self._project: "Optional[str]" = None
        # The facade does NOT own the connection pool's lifecycle: the core
        # calls close() on every request-scoped client after each turn, but our
        # cached facade is shared across the agent-init (shared) and per-request
        # slots as ONE object — a real close there would poison the shared client
        # ("client has been closed", live case 2026-10-29). So the httpx client is
        # built lazily and rebuilt transparently once closed: close() becomes
        # self-healing, and is_closed() always reports live (the facade never
        # stays dead). Proxy/verify/token context are re-used on rebuild.
        self._http = build_oauth_http_client(
            ctx, platform=platform, fingerprint=fingerprint,
            proxy=proxy, async_mode=False)
        logger.info("antigravity facade built [fp:noop-close-selfheal v2]")

    @property
    def http(self):
        # live-or-rebuild: a closed pool is replaced transparently before use.
        closed = False
        try:
            closed = bool(self._http.is_closed)
        except Exception:
            closed = False
        if closed:
            self._http = build_oauth_http_client(
                self._ctx, platform=self._platform, fingerprint=self._fingerprint,
                proxy=self._proxy, async_mode=False)
        return self._http

    # ---- transport helpers -------------------------------------------------
    def _account(self) -> dict:
        acct = self._store.get(self._platform, self._account_name)
        if acct is None:
            raise AccountNotFound(f"{self._platform} account '{self._account_name}' not found")
        return acct

    def _base(self, acct: dict) -> str:
        for v in (acct.get("base_url"), acct.get("antigravity_base_url"),
                  self._entry.get("inference_base_url")):
            s = str(v or "").strip().rstrip("/")
            if s.startswith("http"):
                return s
        return "https://daily-cloudcode-pa.googleapis.com"

    def _headers(self, acct: dict) -> dict:
        # Authorization is stamped (and Authorization-only auth) by the oauth
        # request hook; here we set the identity headers CPA whitelists (§3).
        return {"Content-Type": "application/json", "User-Agent": _native_ua(acct)}

    def _post(self, url: str, body: dict, headers: dict, timeout: "Optional[float]" = None):
        return self.http.post(url, json=body, headers=headers,
                              timeout=timeout or self._timeout)

    def _ensure_project(self) -> str:
        if self._project:
            return self._project
        acct = self._account()
        pid = str(acct.get("project_id") or "").strip()
        if pid:
            self._project = pid
            return pid
        token = self._ctx.token()
        base = self._base(acct)
        try:
            r = self._post(base + LOAD_PATH, {"metadata": {"ideType": "ANTIGRAVITY"}},
                           self._headers(acct), timeout=20)
            if r.status_code == 200:
                data = r.json()
                proj = data.get("cloudaicompanionProject")
                pid = (proj.get("id") if isinstance(proj, dict) else str(proj or "")).strip()
                pid = pid or str(data.get("projectId") or data.get("project") or "").strip()
        except Exception as exc:
            logger.debug("advanced-providers antigravity: loadCodeAssist project probe failed: %s", exc)
        if not pid:
            pid = self._onboard_user(acct, base, token)
        self._project = pid or DEFAULT_PROJECT
        if self._project != DEFAULT_PROJECT:
            try:
                def _stamp(data: dict):
                    rec = data.setdefault(self._platform, {}).setdefault(self._account_name, {})
                    rec["project_id"] = self._project
                    return rec
                self._store.mutate(_stamp)
            except Exception:
                pass
        return self._project

    def _onboard_user(self, acct: dict, base: str, token: str) -> str:
        """CPA auth.go:275-316 shape: bare metadata, tier_id, nodejs headers;
        poll (re-POST same body) until done. One-shot attempt; fail soft."""
        version = str(acct.get("antigravity_version") or _DEFAULT_VERSION)
        body = {"tier_id": str(acct.get("tier_id") or "free-tier"),
                "metadata": {"ide_type": "ANTIGRAVITY", "ide_version": version,
                             "ide_name": "antigravity"}}
        headers = dict(self._headers(acct))
        headers["Accept"] = "*/*"
        headers["User-Agent"] = f"{_native_ua(acct)} google-api-nodejs-client/10.3.0"
        headers["X-Goog-Api-Client"] = "gl-node/22.21.1"
        try:
            for _ in range(3):
                r = self._post(base + ONBOARD_PATH, body, headers, timeout=20)
                if r.status_code != 200:
                    return ""
                data = r.json()
                if data.get("done"):
                    proj = (data.get("response") or {}).get("cloudaicompanionProject")
                    if isinstance(proj, dict):
                        return str(proj.get("id") or "")
                    return str(proj or "")
        except Exception as exc:
            logger.debug("advanced-providers antigravity: onboardUser failed: %s", exc)
        return ""

    # ---- the one true call path ---------------------------------------------
    def create(self, **kwargs: Any) -> Any:
        stream = bool(kwargs.pop("stream", False))
        kwargs.pop("stream_options", None)
        model_raw = str(kwargs.get("model") or self._entry.get("model") or "")
        model, effort = strip_effort(model_raw)
        kwargs.pop("model", None)
        if effort and not kwargs.get("reasoning_effort"):
            kwargs["reasoning_effort"] = effort

        acct = self._account()
        project = self._ensure_project()
        session_seed = _session_seed(model, kwargs)
        session_id = derive_session_id(session_seed)
        envelope, name_map = translate_request(
            {**kwargs, "model": model}, project=project, model=model,
            session_id=session_id, account=acct)

        claude_route = uses_stream_route(model)
        headers = self._headers(acct)

        if stream:
            # streamed via context manager INSIDE the generator: the core chat
            # path consumes a plain iterator; the connection must stay open
            # across yields (same contract as the codex facade's iterator).
            # `self.http` is read INSIDE _gen (not captured): whatever lifecycle
            # event touched the pool between create() and first iteration, the
            # generator opens on the live instance.
            url = self._base(acct) + STREAM_PATH

            def _gen():
                with self.http.stream("POST", url, json=envelope, headers=headers,
                                      timeout=self._timeout) as r:
                    if r.status_code != 200:
                        try:
                            r.read()
                            snippet = r.text[:400]
                        except Exception:
                            snippet = ""
                        raise RuntimeError(
                            f"antigravity upstream {r.status_code}: {snippet}") from None
                    state = StreamState(model=model, name_map=name_map)
                    for env in iter_sse_json(r.iter_bytes()):
                        for chunk in state.feed(env):
                            yield chunk
                    for chunk in state.finalize():
                        yield chunk

            return _gen()

        url = self._base(acct) + (STREAM_PATH if claude_route else GENERATE_PATH)
        r = self._post(url, envelope, headers)
        if r.status_code != 200:
            snippet = ""
            try:
                snippet = r.text[:400]
            except Exception:
                pass
            raise RuntimeError(f"antigravity upstream {r.status_code}: {snippet}") from None

        if claude_route:
            merged = merge_stream_to_nonstream(iter_sse_json(_bytes(r)), model=model,
                                               name_map=name_map)
            return dict_to_ns(merged)
        return nonstream_to_chat(r.json(), model=model, name_map=name_map)

    def close(self) -> None:
        # NO-OP by contract: the core closes the request-slot client after
        # every request, and our cached facade IS the agent shared client
        # (one object, two slots). A destructive close poisoned the shared path
        # — live case 2026-10-29 21:54: the pool died between agent_init and
        # the first stream despite the rebuild-on-read property (the close
        # raced generator start). Keeping the pool alive is safe: one bounded
        # keepalive pool per (store, account, proxy, fingerprint) — real
        # teardown only on adapter eviction via purge(), and OS reclaims at
        # process exit.
        pass

    def purge(self) -> None:
        """Destructive teardown: only the adapter cache calls this on eviction."""
        try:
            self._http.close()
        except Exception:
            pass

    def is_closed(self) -> bool:
        # Always False: the facade self-heals its pool through the `http`
        # property, so it is never observably dead. Reporting True would make
        # the core discard-and-recreate the shared agent.client on every reuse
        # (_ensure_primary path); False keeps one stable client identity for
        # the whole session, which is what the v1internal wire needs.
        return False


def _bytes(r) -> "Any":
    try:
        data = r.content
        return iter([data])
    except Exception:
        return r.iter_bytes()


def _session_seed(model: str, kwargs: dict) -> str:
    """Stable conversation key: first user text (CPA's fallback #3) + model."""
    for m in kwargs.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and c:
                return f"{model}:{c[:2000]}"
            if isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("text"):
                        return f"{model}:{str(part['text'])[:2000]}"
    return f"{model}:anon-{uuid.uuid4()}"





class _Completions:
    def __init__(self, facade: AntigravityFacade) -> None:
        self._f = facade

    def create(self, **kwargs: Any) -> Any:
        return self._f.create(**kwargs)


class _ChatShim:
    def __init__(self, completions: _Completions) -> None:
        self.completions = completions
