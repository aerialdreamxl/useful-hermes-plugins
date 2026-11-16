"""Antigravity response -> OpenAI chat shapes (stream + non-stream).

Mirrors CPA translator/antigravity/openai/chat-completions/antigravity_openai_response.go
and executor/antigravity_executor_{stream,execute}.go. The facade client hands
core plain chunk iterators / chat.completion objects — the same contract the
codex facade (oauth_client.py) established.
"""

from __future__ import annotations

import calendar
import codecs
import json
import time
from types import SimpleNamespace
from typing import Any, Iterator, Optional

_counter = 0


def _synth_id(name: str) -> str:
    global _counter
    _counter += 1
    return f"{name}-{time.time_ns()}-{_counter}"


_CONTENT_FILTER_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
                           "SPII", "IMAGE_SAFETY"}


def dict_to_ns(obj: Any) -> Any:
    """Deep SimpleNamespace so core reads chat.completion attrs uniformly."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: dict_to_ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [dict_to_ns(x) for x in obj]
    return obj


class _Usage:
    """Token usage object readable BOTH as attributes and as a mapping."""

    def __init__(self, d: "Optional[dict]") -> None:
        self._d = dict(d or {})
        self.prompt_tokens = int(self._d.get("prompt_tokens") or 0)
        self.completion_tokens = int(self._d.get("completion_tokens") or 0)
        self.total_tokens = int(self._d.get("total_tokens") or 0)
        det = self._d.get("completion_tokens_details")
        self.completion_tokens_details = _Usage(det) if isinstance(det, dict) and det else det
        if isinstance(det, dict):
            self.reasoning_tokens = int(det.get("reasoning_tokens") or 0)
        pdet = self._d.get("prompt_tokens_details")
        self.prompt_tokens_details = _Usage(pdet) if isinstance(pdet, dict) and pdet else pdet
        if isinstance(pdet, dict):
            self.cached_tokens = int(pdet.get("cached_tokens") or 0)

    def __getattr__(self, item):  # any further nested detail hop
        return self._d.get(item, 0)

    def get(self, item, default=None):
        return self._d.get(item, default)

    def __getitem__(self, item):
        return self._d[item]

    def __contains__(self, item):
        return item in self._d

    def __bool__(self):
        return bool(self._d)


class StreamState:
    """Cross-chunk accumulator (CPA convertCliResponseToOpenAIChatParams)."""

    def __init__(self, *, model: str, name_map: "Optional[dict[str, str]]" = None) -> None:
        self.model = model
        self.name_map = name_map or {}
        self.saw_response = False
        self.saw_tool_call = False
        self.saw_finish = False
        self.upstream_finish = ""
        self.model_version = ""
        self.response_id = ""
        self.pending_usage: "Optional[dict]" = None
        self.created = 0
        self.function_index = 0

    # ---- finish resolution (spec §9.6)
    def _finish_pair(self) -> "tuple[str, str]":
        native = (self.upstream_finish or "STOP").lower()
        if self.saw_tool_call:
            return "tool_calls", native
        if native == "max_tokens":
            return "length", native
        if self.upstream_finish in _CONTENT_FILTER_REASONS:
            return "content_filter", native
        return "stop", native

    def _usage_dict(self, um: dict) -> dict:
        pt = int(um.get("promptTokenCount") or 0)
        ct = int(um.get("candidatesTokenCount") or 0)
        th = int(um.get("thoughtsTokenCount") or 0)
        cached = int(um.get("cachedContentTokenCount") or 0)
        usage = {"prompt_tokens": pt, "completion_tokens": ct + th,
                 "total_tokens": int(um.get("totalTokenCount") or (pt + ct + th))}
        details: dict[str, Any] = {}
        if th > 0:
            details["completion_tokens_details"] = {"reasoning_tokens": th}
        if cached > 0:
            details["prompt_tokens_details"] = {"cached_tokens": cached}
        usage.update(details)
        return usage

    def _chunk(self, *, delta: dict, finish: "Optional[str]" = None,
               native: "Optional[str]" = None, usage: "Optional[dict]" = None) -> Any:
        full = {"role": None, "content": None, "reasoning_content": None,
                "tool_calls": None, "images": None}
        full.update(delta)
        for key in ("tool_calls", "images"):
            if isinstance(full.get(key), list):
                full[key] = [dict_to_ns(x) for x in full[key]]
        choice = SimpleNamespace(index=0, delta=SimpleNamespace(**full),
                                 finish_reason=finish,
                                 native_finish_reason=native if finish else None)
        obj = SimpleNamespace(id=self.response_id or "chatcmpl-antigravity",
                              object="chat.completion.chunk",
                              created=self.created or int(time.time()),
                              model=self.model_version or self.model,
                              choices=[choice])
        if usage is not None:
            obj.usage = usage  # callers pass _Usage (or None) already
        return obj

    # ---- one upstream envelope -> zero or more chunks
    def feed(self, envelope: dict) -> "list[Any]":
        # in-band error frame (HTTP 200 carrying {"error":...}) — caller raises
        if isinstance(envelope.get("error"), dict):
            raise _InBandError(envelope["error"])
        resp = envelope.get("response") if isinstance(envelope.get("response"), dict) else envelope
        if not isinstance(resp, dict):
            return []
        out: "list[Any]" = []

        mv = resp.get("modelVersion")
        if mv:
            self.model_version = str(mv)
        rid = resp.get("responseId")
        if rid:
            self.response_id = str(rid)
        ct = resp.get("createTime")
        if ct:
            try:
                # createTime is UTC (proto JSON); timegm reads it as such —
                # mktime would reinterpret it in the host zone (+8h under CST).
                self.created = int(calendar.timegm(time.strptime(
                    str(ct)[:19], "%Y-%m-%dT%H:%M:%S")))
            except Exception:
                pass
        if not self.saw_response:
            self.saw_response = True  # any envelope counts (CPA hasAntigravityResponsePayload is looser)

        cand = (resp.get("candidates") or [None])[0] or {}
        fr = str(cand.get("finishReason") or "").upper()
        if fr:
            self.upstream_finish = fr

        usage_meta = resp.get("usageMetadata")
        if isinstance(usage_meta, dict):
            # CPA keeps usage out of non-terminal frames; emit only on terminal
            self.pending_usage = self._usage_dict(usage_meta)

        parts = cand.get("content", {}).get("parts") if isinstance(cand.get("content"), dict) else None
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            sig = part.get("thoughtSignature") or part.get("thought_signature")
            fc = part.get("functionCall")
            text = part.get("text")
            inline = part.get("inlineData") or part.get("inline_data")
            has_payload = text is not None or fc is not None or inline is not None
            if sig and not has_payload:
                continue  # encrypted-only thought carrier: ignore (§9.4)
            if text is not None:
                if part.get("thought"):
                    out.append(self._chunk(delta={"role": "assistant", "content": None,
                                                  "reasoning_content": text}))
                else:
                    out.append(self._chunk(delta={"role": "assistant", "content": text,
                                                  "reasoning_content": None}))
            elif isinstance(fc, dict):
                self.saw_tool_call = True
                name = self.name_map.get(str(fc.get("name") or ""), str(fc.get("name") or ""))
                fid = str(fc.get("id") or "").strip() or _synth_id(name)
                args = fc.get("args")
                args_s = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) \
                    else json.dumps({"params": args}, ensure_ascii=False)
                idx = self.function_index
                self.function_index += 1
                out.append(self._chunk(delta={
                    "role": "assistant", "content": None, "reasoning_content": None,
                    "tool_calls": [{"id": fid, "index": idx, "type": "function",
                                    "function": {"name": name, "arguments": args_s}}]}))
            elif isinstance(inline, dict):
                data = str(inline.get("data") or "")
                if not data:
                    continue
                mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
                out.append(self._chunk(delta={
                    "role": "assistant", "content": None, "reasoning_content": None,
                    "images": [{"type": "image_url", "index": 0,
                                "image_url": {"url": f"data:{mime};base64,{data}"}}]}))

        if fr and not self.saw_finish:
            # terminal frame: flush finish (+ pending usage)
            self.saw_finish = True
            finish, native = self._finish_pair()
            out.append(self._chunk(delta={"role": None, "content": None,
                                          "reasoning_content": None},
                                   finish=finish, native=native,
                                   usage=_Usage(self.pending_usage) if self.pending_usage else None))
        return out

    # ---- stream EOF (no finishReason seen = clean end, tool streams do this)
    def finalize(self) -> "list[Any]":
        if self.saw_response and not self.saw_finish:
            self.saw_finish = True
            finish, native = self._finish_pair()
            return [self._chunk(delta={"role": None, "content": None,
                                       "reasoning_content": None},
                                finish=finish, native=native,
                                usage=_Usage(self.pending_usage) if self.pending_usage else None)]
        if not self.saw_response:
            # empty upstream body = failure, not an empty success (§response.go:69)
            raise RuntimeError("antigravity: empty upstream response (no terminal frame)")
        return []


class _InBandError(RuntimeError):
    def __init__(self, payload: dict) -> None:
        code = payload.get("code")
        try:
            self.status_code = int(code) if int(code) in range(400, 600) else 502
        except Exception:
            self.status_code = 502
        super().__init__(f"antigravity in-band error {self.status_code}: "
                         f"{json.dumps(payload, ensure_ascii=False)[:300]}")


# ------------------------------------------------------------------ SSE parse
def iter_sse_json(chunks: "Iterator[bytes]") -> "Iterator[dict]":
    """Yield parsed JSON envelopes from an SSE byte iterator.

    Faithful port of CPA's scanner buffer (stream.go:271-295): a data: line
    that fails validity becomes PENDING and every following non-comment line
    (with or without a data: prefix) is appended with a newline until the
    accumulation parses — emit happens the instant the buffer is VALID, not
    on the blank-line boundary. No [DONE] sentinel exists upstream;
    end-of-stream is the generator ending.
    """
    buf = ""
    pending = ""

    def _consume(line: str) -> "Optional[dict]":
        nonlocal pending
        payload = line[5:].lstrip(" ") if line.startswith("data:") else None
        if pending:
            frag = (payload if payload is not None else line.strip())
            if payload is not None or (frag and not frag.startswith(":")):
                if frag.strip():
                    pending = pending + "\n" + frag.strip()
            try:
                obj = json.loads(pending, strict=False)
            except Exception:
                return None          # still incomplete — keep buffering
            pending = ""
            return obj
        if payload is not None:
            ps = payload.strip()
            if not ps:
                return None
            try:
                return json.loads(ps, strict=False)
            except Exception:
                pending = ps          # start of a multi-line JSON value
                return None
        return None                   # bare line with no pending: event:/id:/comment
    # Incremental decoder: a network chunk can end mid-codepoint, so a
    # per-chunk decode("utf-8", "replace") turned split CJK text into U+FFFD.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    for raw in chunks:
        try:
            buf += decoder.decode(raw, final=False)
        except Exception:
            continue
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            obj = _consume(line.rstrip("\r"))
            if obj is not None:
                yield obj
    try:
        buf += decoder.decode(b"", final=True)   # flush a split multi-byte tail
    except Exception:
        pass
    if buf:
        obj = _consume(buf.rstrip("\r"))
        if obj is not None:
            yield obj
    if pending:
        try:
            yield json.loads(pending, strict=False)
        except Exception:
            pass  # truncated stream tail — nothing valid was ever assembled


def merge_stream_to_nonstream(envelopes: "Iterator[dict]", *, model: str,
                              name_map: "Optional[dict[str, str]]" = None) -> dict:
    """Claude-path non-streaming: merge SSE frames into one generateContent-style body.

    Mirrors executor convertStreamToNonStream (execute.go:490-681): parts
    concatenated in order, finishReason from the terminal frame, usage lifted
    from the last frame carrying it.
    """
    state = StreamState(model=model, name_map=name_map)
    parts_acc: "list[dict]" = []
    for env in envelopes:
        resp = env.get("response") if isinstance(env.get("response"), dict) else env
        if not isinstance(resp, dict):
            continue
        state.saw_response = True
        if resp.get("modelVersion"):
            state.model_version = str(resp["modelVersion"])
        if resp.get("responseId"):
            state.response_id = str(resp["responseId"])
        cand = (resp.get("candidates") or [None])[0] or {}
        fr = str(cand.get("finishReason") or "").upper()
        if fr:
            state.upstream_finish = fr
        um = resp.get("usageMetadata")
        if isinstance(um, dict):
            state.pending_usage = state._usage_dict(um)
        for part in ((cand.get("content") or {}).get("parts") or []):
            if isinstance(part, dict):
                parts_acc.append(part)
    if not state.saw_response:
        raise RuntimeError("antigravity: empty upstream stream (claude merge path)")
    finish, native = state._finish_pair()
    msg = _build_message(parts_acc, name_map or state.name_map)
    message: "dict[str, Any]" = {"role": "assistant", "content": msg.content,
                                 "reasoning_content": msg.reasoning_content}
    if msg.tool_calls:
        message["tool_calls"] = [
            {"id": t.id, "type": "function", "index": k,
             "function": {"name": t.function.name, "arguments": t.function.arguments}}
            for k, t in enumerate(msg.tool_calls)]
    usage_dict = state.pending_usage._d if isinstance(state.pending_usage, _Usage) else state.pending_usage
    return {
        "id": state.response_id or "chatcmpl-antigravity",
        "object": "chat.completion",
        "created": state.created or int(time.time()),
        "model": state.model_version or model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish,
                     "native_finish_reason": native}],
        "usage": usage_dict,
    }


def nonstream_to_chat(data: dict, *, model: str,
                      name_map: "Optional[dict[str, str]]" = None) -> Any:
    """Gemini-path plain :generateContent body -> chat.completion object."""
    resp = data.get("response") if isinstance(data.get("response"), dict) else data
    if isinstance(data.get("error"), dict):
        raise _InBandError(data["error"])
    cand = (resp.get("candidates") or [None])[0] or {}
    parts = (cand.get("content") or {}).get("parts") or []
    fr = str(cand.get("finishReason") or "").upper()
    saw_tool = any(isinstance(p, dict) and p.get("functionCall") for p in parts)
    state = StreamState(model=model, name_map=name_map or {})
    state.upstream_finish = fr
    state.saw_tool_call = saw_tool
    state.model_version = str(resp.get("modelVersion") or "")
    state.response_id = str(resp.get("responseId") or "")
    um = resp.get("usageMetadata")
    if isinstance(um, dict):
        state.pending_usage = state._usage_dict(um)
    finish, native = state._finish_pair()
    message = _build_message(parts, name_map or {})
    return SimpleNamespace(
        id=state.response_id or "chatcmpl-antigravity",
        object="chat.completion",
        created=state.created or int(time.time()),
        model=state.model_version or model,
        choices=[SimpleNamespace(index=0, message=message, finish_reason=finish,
                                 native_finish_reason=native)],
        usage=_Usage(state.pending_usage) if state.pending_usage else None)


def _build_message(parts: "list[dict]", name_map: dict) -> SimpleNamespace:
    text_parts: "list[str]" = []
    reasoning_parts: "list[str]" = []
    tool_calls: "list[SimpleNamespace]" = []
    images: "list[dict]" = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        fc = part.get("functionCall")
        if isinstance(fc, dict):
            name = name_map.get(str(fc.get("name") or ""), str(fc.get("name") or ""))
            fid = str(fc.get("id") or "").strip() or _synth_id(name)
            args = fc.get("args")
            args_s = json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) \
                else json.dumps({"params": args}, ensure_ascii=False)
            tool_calls.append(SimpleNamespace(
                id=fid, type="function", index=len(tool_calls),
                function=SimpleNamespace(name=name, arguments=args_s)))
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "image/png")
            images.append({"type": "image_url",
                           "image_url": {"url": f"data:{mime};base64,{inline['data']}"}})
            continue
        text = part.get("text")
        if text:
            (reasoning_parts if part.get("thought") else text_parts).append(str(text))
    content = "".join(text_parts) or None
    msg = SimpleNamespace(role="assistant", content=content,
                          reasoning_content="".join(reasoning_parts) or None,
                          tool_calls=tool_calls or None)
    if images:
        msg.images = images
    return msg
