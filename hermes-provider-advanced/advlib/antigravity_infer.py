"""Antigravity (cloudcode-pa) inference wire — OpenAI chat <-> v1internal.

Ported from CLIProxyAPI's translators (spec: antigravity-wire-spec.md, every
rule cites the Go file it mirrors):

  envelope            executor/antigravity_executor_request.go:653 (geminiToAntigravity)
  request translate   translator/antigravity/openai/chat-completions/antigravity_openai_request.go
  response translate  translator/antigravity/openai/chat-completions/antigravity_openai_response.go
  stream parse        executor/antigravity_executor_stream.go (SSE buffering, in-band error)
  family rules        executor/antigravity_executor_execute.go:57 (claude -> stream-only)

Transport identity (headers, base selection) rides oauth_client's http client;
this module only builds/reads the JSON bodies. No token ever passes through
here — Authorization is stamped by the request hook.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import time
import uuid
from typing import Any, Optional

# ------------------------------------------------------------------ registry
# maxOutputTokens caps mirrored from CPA registry/models/models.json
# (antigravity section, recon 2026-10-29). Update alongside the catalog table
# in adapters/antigravity.py when Google drifts.
MODEL_CAPS = {
    "claude-opus-4-6-thinking": 64000,
    "claude-sonnet-4-6": 64000,
    "claude-opus-5-5-high": 128000,
    "claude-sonnet-5-5-high": 128000,
    "gemini-4-argon-high": 1048576,
    "gemini-3.8-flash-high": 65536,
    "gemini-3-flash": 65536,
    "gemini-3.1-flash-lite": 65535,
    "gemini-3.5-flash-lite": 65535,
    "gpt-oss-120b-medium": 32768,
}

STREAM_ONLY_MODELS = ("gemini-3-pro", "gemini-3.1-pro", "gemini-3.1-flash-image")

_THOUGHT_SKIP_SIG = "skip_thought_signature_validator"


def model_family(model: str) -> str:
    """'claude' | 'gemini' — the two schema/replay branches (spec §8)."""
    return "claude" if "claude" in (model or "").lower() else "gemini"


def uses_stream_route(model: str) -> bool:
    """Claude (and two Gemini special cases) always ride streamGenerateContent."""
    m = (model or "").lower()
    return "claude" in m or any(s in m for s in STREAM_ONLY_MODELS)


# ------------------------------------------------------------------ session id
def derive_session_id(payload_text: str) -> str:
    """Negative-int64 decimal string, stable per conversation (spec §6.4).

    Mirrors CPA DerivedAntigravitySessionID: SHA-256 over the
    'cli-proxy-api:antigravity:derived-session\\x00' domain prefix + the seed
    text, first 8 bytes big-endian, masked to positive int64 range, negated.
    """
    digest = hashlib.sha256(
        b"cli-proxy-api:antigravity:derived-session\x00" + payload_text.encode("utf-8")
    ).digest()
    value = struct.unpack(">q", digest[:8])[0] & 0x7FFFFFFFFFFFFFFF
    return "-" + str(value)


# ------------------------------------------------------------------ names
_NAME_BAD = re.compile(r"[^A-Za-z0-9_]")


def sanitize_tool_name(name: str, used: "dict[str, str]") -> str:
    """Gemini function-name constraints (spec §6.7), collision-safe, mapped."""
    raw = str(name or "")
    base = _NAME_BAD.sub("_", raw)
    if base and not base[0].isalpha():
        base = "f_" + base
    base = base[:64] or "function"
    cand, n = base, 1
    while cand in used and used[cand] != raw:
        cand = f"{base[:60]}_{n}"
        n += 1
    used[cand] = raw
    return cand


# ------------------------------------------------------------------ request
def translate_request(kw: dict, *, project: str, model: str, session_id: str,
                      account: "Optional[dict]" = None) -> dict:
    """OpenAI chat.completions kwargs -> Antigravity envelope (spec §5-§6)."""
    family = model_family(model)
    req: dict[str, Any] = {"contents": []}

    # ---- generationConfig
    gc: dict[str, Any] = {}
    src_gc = kw.get("generationConfig") or kw.get("generation_config")
    if isinstance(src_gc, dict):
        gc.update(src_gc)  # client passthrough wins baseline, per CPA order
    effort = kw.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        tc = dict(gc.get("thinkingConfig") or {})
        if effort.strip().lower() == "auto":
            tc["thinkingBudget"] = -1
        else:
            tc["thinkingLevel"] = effort.strip().lower()
        gc["thinkingConfig"] = tc
    for ok_, ak_ in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK")):
        v = kw.get(ok_)
        if isinstance(v, (int, float)):
            gc[ak_] = v
    max_tok = kw.get("max_tokens")
    if not isinstance(max_tok, (int, float)):
        max_tok = kw.get("max_completion_tokens")
    if isinstance(max_tok, (int, float)):
        cap = MODEL_CAPS.get(model)
        gc["maxOutputTokens"] = min(int(max_tok), cap) if cap else int(max_tok)
    rf = kw.get("response_format")
    if isinstance(rf, dict):
        rtype = str(rf.get("type") or "").strip().lower()
        if rtype in ("json_object", "json_schema"):
            for k in ("responseSchema", "responseJsonSchema", "response_schema", "response_json_schema"):
                gc.pop(k, None)
            gc["responseMimeType"] = "application/json"
            if rtype == "json_schema":
                schema = (rf.get("json_schema") or {}).get("schema")
                if isinstance(schema, dict):
                    gc["responseSchema"] = schema
    n_val = kw.get("n")
    if isinstance(n_val, (int, float)) and n_val > 1:
        gc["candidateCount"] = int(n_val)
    mods = kw.get("modalities")
    if isinstance(mods, list):
        rm = [{"text": "TEXT", "image": "IMAGE"}.get(str(m).lower()) for m in mods]
        rm = [x for x in rm if x]
        if rm:
            gc["responseModalities"] = rm
    if gc:
        req["generationConfig"] = gc

    # ---- messages -> systemInstruction + contents (spec §6.2/§6.3)
    msgs = kw.get("messages") or []
    system_parts: "list[dict]" = []
    seen_conversation = False
    i = 0
    while i < len(msgs):
        m = msgs[i] if isinstance(msgs[i], dict) else {}
        role = str(m.get("role") or "")
        content = m.get("content")
        if role in ("system", "developer") and len(msgs) > 1 and not seen_conversation:
            system_parts.extend(_text_parts(content))
            i += 1
            continue
        if role in ("user", "system", "developer"):
            seen_conversation = True
            demoted = role != "user"
            parts = _user_parts(content, demoted=demoted)
            if parts:
                req["contents"].append({"role": "user", "parts": parts})
            i += 1
            continue
        if role == "assistant":
            seen_conversation = True
            parts = _assistant_parts(m)
            if parts:
                req["contents"].append({"role": "model", "parts": parts})
            tcs = m.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                responses, j = _collect_tool_responses(msgs, i + 1)
                fr_parts = _function_response_parts(tcs, responses, family)
                if fr_parts:
                    # CPA emits the collected functionResponse turn as role
                    # "user" (antigravityOpenAIContent("user", ...)); the
                    # gemini functionResponse->model role normalization is a
                    # reasoning-replay feature out of scope here (spec §8).
                    req["contents"].append({"role": "user", "parts": fr_parts})
                i = j
            else:
                i += 1
            continue
        if role == "tool":
            # stray tool message with no owning assistant turn: fold as user text
            seen_conversation = True
            txt = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            req["contents"].append({"role": "user",
                                    "parts": [{"text": f"[tool result] {txt}"}]})
            i += 1
            continue
        i += 1

    if system_parts:
        req["systemInstruction"] = {"role": "user", "parts": system_parts}

    # ---- tools / toolConfig (spec §6.5/§6.6)
    name_map: "dict[str, str]" = {}
    tools = kw.get("tools")
    if isinstance(tools, list) and tools:
        decls: "list[dict]" = []
        builtin: "list[dict]" = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            ttype = str(t.get("type") or "")
            if ttype == "function":
                fn = t.get("function") or {}
                name = sanitize_tool_name(str(fn.get("name") or ""), name_map)
                decl: dict[str, Any] = {k: v for k, v in fn.items()
                                        if k in ("description",)}
                decl["name"] = name
                params = fn.get("parameters") or fn.get("parametersJsonSchema")
                decl["parametersJsonSchema"] = params if isinstance(params, dict) else \
                    {"type": "object", "properties": {}}
                decls.append(decl)
            elif ttype in ("google_search", "code_execution", "url_context"):
                key = {"google_search": "googleSearch", "code_execution": "codeExecution",
                       "url_context": "urlContext"}[ttype]
                entry = {key: t.get(key) if isinstance(t.get(key), dict) else {}}
                builtin.append(entry)
        merged: "list[dict]" = []
        if decls:
            # dedupe by name
            seen_names: "set[str]" = set()
            uniq = []
            for d in decls:
                if d["name"] not in seen_names:
                    seen_names.add(d["name"])
                    uniq.append(d)
            merged.append({"functionDeclarations": uniq})
        merged.extend(builtin)
        if merged:
            req["tools"] = merged

    tc_src = kw.get("tool_choice")
    if tc_src is not None and req.get("tools"):
        fc: dict[str, Any] = {}
        if tc_src == "none":
            fc["mode"] = "NONE"
        elif tc_src == "auto":
            fc["mode"] = "AUTO"
        elif tc_src in ("required", "any"):
            fc["mode"] = "ANY"
        elif isinstance(tc_src, dict) and tc_src.get("type") == "function":
            fc["mode"] = "ANY"
            nm = sanitize_tool_name(str((tc_src.get("function") or {}).get("name") or ""), name_map)
            fc["allowedFunctionNames"] = [nm]
        if fc:
            req["toolConfig"] = {"functionCallingConfig": fc}

    # ---- family branch (spec §8)
    if family == "claude":
        if req.get("tools") and not (gc.get("responseMimeType") == "application/json"
                                      and gc.get("responseSchema") is None):
            tcfg = dict(req.get("toolConfig") or {})
            fcc = dict(tcfg.get("functionCallingConfig") or {})
            if fcc.get("mode") != "NONE":
                fcc["mode"] = "VALIDATED"
            tcfg["functionCallingConfig"] = fcc
            req["toolConfig"] = tcfg
        _clean_tool_schemas(req, claude=True, aggressive=True)
    else:
        # CPA sanitizes tool schemas on EVERY family; only the flags differ
        # (executor_request.go:72): useAntigravitySchema = claude OR the
        # gemini-3-pro / gemini-3.1-pro family -> placeholder required,
        # multi-branch unions preserved; every other gemini keeps unions and
        # gets no placeholder. Un-cleaned $ref/type-arrays are a 400.
        _clean_tool_schemas(req, claude=False,
                            aggressive=_uses_antigravity_schema(model))
        gc.pop("maxOutputTokens", None)  # upstream ignores/errors; effintent via thinking
        if gc:
            req["generationConfig"] = gc
        else:
            req.pop("generationConfig", None)
        req.pop("safetySettings", None)
        _gemini_boundary_fix(req)

    if req.get("tools"):
        _rename_params_json_schema(req)

    request_id = "agent-" + str(uuid.uuid4())
    if "image" in (model or ""):
        request_id = f"image_gen/{int(time.time() * 1000)}/{uuid.uuid4()}/12"
        req.pop("sessionId", None)
    else:
        req["sessionId"] = session_id

    envelope = {
        "project": project,
        "model": model,
        "userAgent": "antigravity",
        "requestId": request_id,
        "requestType": "image_gen" if "image" in (model or "") else "agent",
        "request": req,
    }
    return envelope, name_map


def _text_parts(content: Any) -> "list[dict]":
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if isinstance(content, dict) and content.get("type") == "text":
        return [{"text": content.get("text")}] if content.get("text") else []
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("text"):
                out.append({"text": p["text"]})
        return out
    return []


def _demoted_system(text: str, is_demoted: bool) -> str:
    if is_demoted:
        return f"<system-reminder>\n{text}\n</system-reminder>"
    return text


def _user_parts(content: Any, demoted: bool = False) -> "list[dict]":
    if isinstance(content, str):
        return [{"text": _demoted_system(content, demoted)}] if content.strip() else []
    if isinstance(content, dict) and content.get("type") == "text":
        txt = str(content.get("text") or "")
        return [{"text": _demoted_system(txt, demoted)}] if txt else []
    if not isinstance(content, list):
        return []
    parts: "list[dict]" = []
    for item in content:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype == "text":
            txt = str(item.get("text") or "")
            if txt:
                parts.append({"text": _demoted_system(txt, demoted)})
        elif itype in ("image_url", "video_url"):
            url = str((item.get(itype) or {}).get("url") or item.get(itype) or "")
            mime, data = _data_url(url)
            if data:
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
        elif itype == "file":
            f = item.get("file") or {}
            mime, data = _data_url(str(f.get("file_data") or ""))
            if not data:
                mime = mime or _mime_from_filename(str(f.get("filename") or ""))
                data = str(f.get("file_data") or "")
            if data:
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
        elif itype == "input_audio":
            a = item.get("input_audio") or {}
            data = str(a.get("data") or "")
            if data:
                parts.append({"inlineData": {
                    "mime_type": _audio_mime(str(a.get("format") or "")), "data": data}})
    return parts


def _assistant_parts(m: dict) -> "list[dict]":
    parts: "list[dict]" = []
    reasoning = m.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        parts.append({"text": reasoning, "thought": True})
    content = m.get("content")
    if isinstance(content, str) and content:
        parts.append({"text": content})
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = str(item.get("type") or "")
            if itype == "text" and item.get("text"):
                parts.append({"text": item["text"]})
            elif itype == "image_url":
                url = str((item.get("image_url") or {}).get("url") or "")
                mime, data = _data_url(url)
                if data:
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
    used_ids = set()
    for tc in m.get("tool_calls") or []:
        if not isinstance(tc, dict) or tc.get("type") not in (None, "function"):
            continue
        fn = tc.get("function") or {}
        raw_id = str(tc.get("id") or "")
        fid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_id) or f"toolu_{int(time.time_ns())}"
        while fid in used_ids:
            fid = fid + "_1"
        used_ids.add(fid)
        call: dict[str, Any] = {"id": fid, "name": str(fn.get("name") or "")}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                call["args"] = json.loads(args)
            except Exception:
                call["args"] = {"params": args}
        elif isinstance(args, dict):
            call["args"] = args
        else:
            call["args"] = {}
        parts.append({"functionCall": call, "thoughtSignature": _THOUGHT_SKIP_SIG})
    return parts


def _collect_tool_responses(msgs: list, start: int) -> "tuple[dict, int]":
    responses: "dict[str, Any]" = {}
    j = start
    while j < len(msgs):
        nxt = msgs[j] if isinstance(msgs[j], dict) else {}
        if str(nxt.get("role")) == "tool":
            responses[str(nxt.get("tool_call_id") or "")] = nxt.get("content")
            j += 1
        elif str(nxt.get("role")) == "assistant":
            break
        else:
            break
    return responses, j


def _function_response_parts(tcs: list, responses: dict, family: str) -> "list[dict]":
    parts: "list[dict]" = []
    for tc in tcs:
        if not isinstance(tc, dict):
            continue
        raw_id = str(tc.get("id") or "")
        fid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_id) or raw_id
        name = str((tc.get("function") or {}).get("name") or "")
        content = responses.get(raw_id)
        if content is None:
            content = "{}"
        result = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        parts.append({"functionResponse": {"id": fid, "name": name,
                                           "response": {"result": result}}})
    return parts


def _data_url(url: str) -> "tuple[str, str]":
    if url.startswith("data:"):
        try:
            head, body = url[5:].split(",", 1)
            mime = head.split(";")[0] or "application/octet-stream"
            return mime, body
        except Exception:
            return "", ""
    return "", ""


def _mime_from_filename(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown",
            "csv": "text/csv", "json": "application/json", "html": "text/html"}.get(ext, "")


def _audio_mime(fmt: str) -> str:
    return {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
            "aac": "audio/aac", "webm": "audio/webm", "ogg": "audio/ogg",
            "pcm16": "audio/pcm"}.get(fmt, f"audio/{fmt}" if fmt else "audio/wav")


def _rename_params_json_schema(req: dict) -> None:
    """buildRequest renames parametersJsonSchema -> parameters on the wire (§7)."""
    for entry in req.get("tools") or []:
        for decl in entry.get("functionDeclarations") or entry.get("function_declarations") or []:
            if "parametersJsonSchema" in decl:
                decl["parameters"] = decl.pop("parametersJsonSchema")


def _uses_antigravity_schema(model: str) -> bool:
    """CPA executor_request.go:72 — the model families that get the
    Antigravity schema dialect (placeholder + its own cleaning flags)."""
    m = (model or "").lower()
    return "claude" in m or "gemini-3-pro" in m or "gemini-3.1-pro" in m


def _clean_tool_schemas(req: dict, *, claude: bool, aggressive: bool = False) -> None:
    """Clean every declaration schema + the response schema.

    Delegates wholesale to the ported engine (``gemini_schema_clean``, a
    line-for-line port of CPA's gemini_schema.go): $ref inlining, type-array
    collapse, allOf merge, placeholder injection and union handling all live
    there now; the old stopgap cleaner in this file is gone.

      claude      -> CleanJSONSchemaForAntigravityClaudeTool (placeholder, unions flat)
      aggressive  -> CleanJSONSchemaForAntigravityTool(requirePlaceholder=true),
                     used for the gemini-3-pro / gemini-3.1-pro families
      otherwise   -> CleanJSONSchemaForAntigravityTool(requirePlaceholder=false)
    responseSchema -> CleanJSONSchemaForAntigravityResponse (unions preserved)
    """
    from .gemini_schema_clean import (
        antigravity_tool_schema,
        clean_antigravity_claude_tool,
        clean_antigravity_response,
        clean_antigravity_tool,
    )

    if claude:
        cleaner = clean_antigravity_claude_tool
    elif aggressive:
        def cleaner(schema):  # requirePlaceholder, multi-branch unions preserved
            return antigravity_tool_schema(schema, require_placeholder=True,
                                           flatten_multi_branch_unions=False)
    else:
        cleaner = clean_antigravity_tool

    for entry in req.get("tools") or []:
        for key in ("functionDeclarations", "function_declarations"):
            for decl in entry.get(key) or []:
                for pk in ("parameters", "parametersJsonSchema"):
                    if isinstance(decl.get(pk), dict):
                        decl[pk] = cleaner(decl[pk])
    gc = req.get("generationConfig") or {}
    for pk in ("responseSchema", "responseJsonSchema", "response_schema", "response_json_schema"):
        if isinstance(gc.get(pk), dict):
            gc[pk] = clean_antigravity_response(gc[pk])


def _gemini_boundary_fix(req: dict) -> None:
    """CPA ensureAntigravityGeminiBoundaryUserContent, exact three-step order
    (gemini_content_turns.go + executor.go:521-531):
      1. DropGeminiTrailingThoughtOnlyContent — trailing model turns made only
         of thought parts are removed (keeping at least one turn); they come
         from replayed reasoning and would otherwise leave a model tail.
      2. leading — if contents[0].role == 'model', prepend empty user turn.
      3. trailing — if the last turn's role is model/assistant, append an
         empty user turn UNLESS it carries functionResponse parts without
         nested media (upstream expects the model to answer those).
    Claude targets never reach this function (adapter rejects empty text)."""
    contents = req.get("contents") or []
    if not contents:
        return

    def _parts(turn: dict) -> "list":
        return turn.get("parts") or []

    def _thought_only(turn: dict) -> bool:
        ps = _parts(turn)
        return bool(ps) and all(p.get("thought") for p in ps if isinstance(p, dict))

    # 1. strip trailing thought-only model turns
    while len(contents) > 1 and str(contents[-1].get("role")) == "model" \
            and _thought_only(contents[-1]):
        contents.pop()
    # 2. leading user
    if str(contents[0].get("role")) == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": ""}]})
    # 3. trailing user (unless a functionResponse turn awaits the model)
    last_role = str(contents[-1].get("role"))
    if last_role in ("model", "assistant"):
        fr = [p for p in _parts(contents[-1]) if isinstance(p, dict) and "functionResponse" in p]
        has_media = any(isinstance((p.get("functionResponse") or {}).get("parts"), list)
                        for p in fr)
        if not fr or has_media:
            contents.append({"role": "user", "parts": [{"text": ""}]})
    req["contents"] = contents
