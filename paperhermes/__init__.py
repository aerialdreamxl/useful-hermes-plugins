"""paperhermes — 外发载荷瘦身插件（PROMPT.md v0.4 接线层）。

register(ctx) 全部 fail-open：任何异常只 WARN，不影响会话。行为由
plugins.entries.paperhermes.settings 开关门控（D8）：首会话缺 settings 时
seed-once 播种一次默认值，此后本插件零 config 写。

分层：engine.py = 纯字符串/字典逻辑（离线可测）；本文件 = Hermes 接缝与会话状态。
折叠形态在 (会话×模型) 首个请求定型冻结（5.2 缓存铁律），观测写 observe.log，
技能使用信号会话末合并进 session_hints.json —— 这两份文件是仅有的盘外落点。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Optional

from .engine import (
    Freezer,
    NOTICES,
    Observer,
    fold_skills_block,
    fold_tools,
    net_gate,
    pick_notice_lang,
    system_slot_apply,
)

logger = logging.getLogger("paperhermes")

PLUGIN_ID = "paperhermes"
DEFAULTS: dict[str, Any] = {
    "observe": True,            # 只读观测项默认 true（D11/裁定#6）
    "fold_tool_desc": False,
    "fold_skills_index": False,
    "fold_skills_dynamic": False,
    "keep_eager": [],
    "fold_first_sentence_max_chars": 120,
    "min_savings_chars": 2048,
}
FOLDABLE_MODES = frozenset({"anthropic_messages", "chat_completions"})
SIGNAL_TTL_DAYS = 30
_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off", ""}

_freezer = Freezer()
_observer: Optional[Observer] = None
_session_signals: dict[str, set] = {}   # session_id -> {skill names viewed this session}
_seeded = False


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", "")


def _dir() -> str:
    base = _home()
    d = os.path.join(base, "paperhermes") if base else os.path.join(tempfile.gettempdir(), "paperhermes")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(tempfile.gettempdir(), "paperhermes")
        os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 设置与播种（D8）
def _settings_missing_or_empty() -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        conf = load_config_readonly() or {}
        entries = (conf.get("plugins") or {}).get("entries") or {}
        entry: Any = None
        for key, val in entries.items():
            if key == PLUGIN_ID or str(key).endswith("/" + PLUGIN_ID):
                entry = val
                break
        if not isinstance(entry, dict):
            return True
        settings = entry.get("settings")
        return not (isinstance(settings, dict) and settings)
    except Exception as exc:
        logger.debug("paperhermes: settings probe failed (%s) — treat as present", exc)
        return False  # 探测失败不播种（保守）


def _seed_once(ctx) -> None:
    global _seeded
    if _seeded:
        return
    _seeded = True
    try:
        if _settings_missing_or_empty():
            for key, val in DEFAULTS.items():
                ctx.set_config(key, val)
            ctx.log("INFO", "paperhermes: seeded defaults into "
                            "plugins.entries.paperhermes.settings (observe=true, folds=false)")
    except Exception as exc:
        logger.warning("paperhermes: seeding failed (%s) — running on code defaults", exc)


def _get(ctx, key: str):
    try:
        val = ctx.get_config(key, DEFAULTS[key])
    except Exception:
        return DEFAULTS[key]
    ref = DEFAULTS[key]
    if isinstance(ref, bool):
        if isinstance(val, bool):
            return val
        s = str(val).strip().lower()
        if s in _TRUTHY:
            return True
        if s in _FALSY:
            return False
        return ref
    if isinstance(ref, int):
        try:
            return max(16, int(val))
        except (TypeError, ValueError):
            return ref
    if isinstance(ref, list):
        return tuple(str(x) for x in val) if isinstance(val, (list, tuple)) else ()
    return val


# ---------------------------------------------------------------- hint 文件
def _hints_path() -> str:
    return os.path.join(_dir(), "session_hints.json")


def _load_hints() -> dict:
    try:
        with open(_hints_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_hints(hints: dict) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=_dir(), suffix=".hints")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(hints, f, ensure_ascii=False)
        os.replace(tmp, _hints_path())
    except Exception as exc:
        logger.debug("paperhermes: hints save failed: %s", exc)


def _hint_names() -> set:
    cutoff = time.time() - SIGNAL_TTL_DAYS * 86400
    return {n for n, ts in _load_hints().items()
            if isinstance(ts, (int, float)) and ts >= cutoff}


def _system_text_of(request: dict, api_mode: str) -> str:
    if api_mode == "anthropic_messages":
        sysv = request.get("system")
        if isinstance(sysv, str):
            return sysv
        if isinstance(sysv, list):
            return " ".join(b.get("text", "") for b in sysv if isinstance(b, dict))
        return ""
    msgs = request.get("messages") or []
    if msgs and str(msgs[0].get("role")) in ("system", "developer") \
            and isinstance(msgs[0].get("content"), str):
        return msgs[0]["content"]
    return ""


# ---------------------------------------------------------------- 技能 keep-full 集（5.2 静态规则）
# 镜像 agent/coding_context._NON_CODING_SKILL_CATEGORIES（v0.23.0 抄录）。
# 为什么不 import：该模块不在 plugin_validate_private_imports._PUBLIC 允许面，私有耦合
# 会在上游重构时断裂（catalog rule 9）。漂移后果有界：最坏折错/漏折某类目描述——名字
# 恒在场 + skills_list 可搜，召回不塌。README（D9）载明此表来源，宿主若已渲染
# `[names only]`（focus 姿态），这些行本就无描述可折，两层自然兼容。
MIRROR_NON_CODING_ROOTS = frozenset({
    "apple", "communication", "cooking", "creative", "email", "finance", "gaming", "gifs",
    "health", "media", "music", "note-taking", "productivity", "shopping", "smart-home",
    "social-media", "travel", "yuanbao",
})


def _keep_full_skill_names(text: str) -> frozenset:
    """编码类目全保留 + hints/本会话信号点名。名字永不删，只有描述可折。"""
    keep: set = set(_hint_names())
    for names in _session_signals.values():
        keep |= names
    start = text.find("## Skills")
    if start < 0:
        return frozenset(keep)
    cur_demote = False
    for line in text[start:].split("\n"):
        stripped = line.strip()
        if line.startswith("  ") and (stripped.endswith(":") or "[names only]:" in stripped) \
                and not stripped.startswith("- "):
            root = stripped.split("[")[0].strip().rstrip(":").strip()
            cur_demote = root in MIRROR_NON_CODING_ROOTS
        elif stripped.startswith("- "):
            name = stripped[2:].split(":")[0].strip()
            if not name:
                continue
            if line.startswith("    "):
                if not cur_demote:
                    keep.add(name)          # 多条目行：跟随所属类目
            else:
                keep.add(name)              # 2-空格行=单技能类目，类目名不可见 → 保守全量
    return frozenset(keep)


# ---------------------------------------------------------------- 决策与执行
def _fkey(session_id: str, model: str) -> str:
    return f"{session_id}|{model}"


def _measure(request: dict, ctx, api_mode: str, model: str) -> dict:
    """(会话×模型) 首请求评估一次。异常由 Freezer 兜成 None=不介入。"""
    fold_t = bool(_get(ctx, "fold_tool_desc"))
    fold_s = bool(_get(ctx, "fold_skills_index"))
    lang = pick_notice_lang(model)
    keep_full = _keep_full_skill_names(_system_text_of(request, api_mode)) if fold_s \
        else frozenset()
    est_tools = est_skills = 0
    if fold_t:
        _nt, _n1, est_tools = fold_tools(
            list(request.get("tools") or []), api_mode,
            _get(ctx, "fold_first_sentence_max_chars"),
            tuple(_get(ctx, "keep_eager")))
    if fold_s:
        _t2, _n2, est_skills = fold_skills_block(_system_text_of(request, api_mode), keep_full)
    notice_bytes = (len(NOTICES[lang]["tools"]) + 1 if fold_t else 0) + \
                   (len(NOTICES[lang]["skills"]) if fold_s else 0)
    gate = _get(ctx, "min_savings_chars")
    act = (fold_t or fold_s) and net_gate(est_tools + est_skills, notice_bytes, gate)
    return {"act": bool(act), "tools": fold_t, "skills": fold_s, "lang": lang,
            "keep_full": keep_full, "est": (est_tools, est_skills, notice_bytes, gate)}


def _apply(request: dict, ctx, decision: dict, api_mode: str):
    """返回 (new_request, tools_folded, skills_folded) 或 (None, 0, 0)。"""
    if not decision or not decision.get("act"):
        return None, 0, 0
    out = request
    tools_folded = skills_folded = 0
    skills_notice_added_in_block = False
    if decision["tools"]:
        new_tools, n, _saved = fold_tools(
            list(out.get("tools") or []), api_mode,
            _get(ctx, "fold_first_sentence_max_chars"),
            tuple(_get(ctx, "keep_eager")))
        if n:
            tools_folded = n
            out = {**out, "tools": new_tools}
    if decision["skills"]:
        skills_notice_added_in_block = False
        sysv = out.get("system") if api_mode == "anthropic_messages" else None
        if api_mode == "anthropic_messages" and isinstance(sysv, list):
            # 多块 system：在含 ## Skills 的块内折档并就地追加告知；cache_control 字段原样保留。
            idx_b = next((i for i, b in enumerate(sysv)
                          if isinstance(b, dict) and "## Skills" in b.get("text", "")), -1)
            if idx_b >= 0:
                blocks = [dict(b) if isinstance(b, dict) else b for b in sysv]
                new_text, n2, _s2 = fold_skills_block(blocks[idx_b]["text"], decision["keep_full"])
                if n2:
                    lang = decision["lang"]
                    blocks[idx_b] = {**blocks[idx_b],
                                     "text": new_text + NOTICES[lang]["skills"].strip()}
                    out = {**out, "system": blocks}
                    skills_folded = n2
                    skills_notice_added_in_block = True
        else:
            text = _system_text_of(out, api_mode)
            new_text, n2, _s2 = fold_skills_block(text, decision["keep_full"])
            if n2:
                if api_mode == "anthropic_messages":
                    out = {**out, "system": new_text}
                else:
                    msgs = list(out.get("messages") or [])
                    if msgs and isinstance(msgs[0].get("content"), str):
                        msgs[0] = {**msgs[0], "content": new_text}
                        out = {**out, "messages": msgs}
                skills_folded = n2
    if not (tools_folded or skills_folded):
        return None, 0, 0  # 事实门：预估≠实际，本轮无可折 → 不介入不注入
    lang = decision["lang"]
    out, _added = system_slot_apply(
        api_mode, out,
        NOTICES[lang]["tools"] if tools_folded else None,
        None if skills_notice_added_in_block else (
            NOTICES[lang]["skills"] if skills_folded else None))
    return out, tools_folded, skills_folded


def _handle_request(request: dict, ctx, api_mode: str, session_id: str, model: str,
                    provider: str):
    if api_mode not in FOLDABLE_MODES:
        return None, 0, 0
    key = _fkey(session_id, model)
    decision = _freezer.decide(key, lambda: _measure(request, ctx, api_mode, model))
    return _apply(request, ctx, decision, api_mode)


# ---------------------------------------------------------------- register
def register(ctx) -> None:
    try:
        _seed_once(ctx)
        observe = bool(_get(ctx, "observe"))
        global _observer
        if observe:
            _observer = Observer(os.path.join(_dir(), "observe.log"))

        def _llm_middleware(kind=None, **kw):
            try:
                request = kw.get("request")
                if not isinstance(request, dict):
                    return None
                api_mode = str(kw.get("api_mode") or "")
                session_id = str(kw.get("session_id") or "")
                model = str(kw.get("model") or "")
                provider = str(kw.get("provider") or "")
                folds_on = bool(_get(ctx, "fold_tool_desc") or _get(ctx, "fold_skills_index"))
                if not folds_on:
                    if _observer is not None:
                        _observer.record_request(request, provider=provider, model=model,
                                                 api_mode=api_mode, session_id=session_id,
                                                 action="switches-off")
                    return None
                new_req, nt, ns = _handle_request(request, ctx, api_mode, session_id,
                                                  model, provider)
                if _observer is not None:
                    _observer.record_request(request, provider=provider, model=model,
                                             api_mode=api_mode, session_id=session_id,
                                             action=("fold" if new_req else "pass"),
                                             tools_folded=nt, skills_folded=ns,
                                             post=(new_req if new_req is not None else None))
                if new_req is None:
                    return None
                return {"request": new_req}
            except Exception as exc:
                logger.warning("paperhermes: middleware error (%s) — request untouched", exc)
                return None

        ctx.register_middleware("llm_request", _llm_middleware)

        def _on_tool(_hook=None, **kw):
            try:
                if str(kw.get("tool_name")) != "skill_view":
                    return
                args = kw.get("args") or {}
                name = str(args.get("name") or "").strip()
                if name:
                    sid = str(kw.get("session_id") or "")
                    _session_signals.setdefault(sid, set()).add(name)
            except Exception:
                pass  # 信号观测 fail-open

        def _on_end(_hook=None, **kw):
            try:
                sid = str(kw.get("session_id") or "")
                sig = _session_signals.pop(sid, set())
                for k in [k for k in _freezer.keys() if k.startswith(sid + "|")]:
                    _freezer._frozen.pop(k, None)
                if not sig or not _get(ctx, "fold_skills_dynamic"):
                    return
                hints = _load_hints()
                now = time.time()
                for n in sig:
                    hints[n] = now
                _save_hints(hints)
            except Exception:
                pass

        ctx.register_hook("post_tool_call", _on_tool)
        ctx.register_hook("on_session_end", _on_end)

        def _panel(args=""):
            try:
                st = {k: _get(ctx, k) for k in DEFAULTS}
                lines = [
                    "paperhermes v0.4 —— 只读面板（改开关：写 plugins.entries.paperhermes."
                    "settings 或插件设置页，下一会话生效）",
                    "  observe=%s fold_tool_desc=%s fold_skills_index=%s fold_skills_dynamic=%s"
                    % (st["observe"], st["fold_tool_desc"], st["fold_skills_index"],
                       st["fold_skills_dynamic"]),
                    "  keep_eager=%s fold_first_sentence_max_chars=%s min_savings_chars=%s"
                    % (list(st["keep_eager"]), st["fold_first_sentence_max_chars"],
                       st["min_savings_chars"]),
                    "  冻结决策=%d 会话技能信号=%d"
                    % (len(_freezer.keys()), sum(len(s) for s in _session_signals.values())),
                ]
                snap = getattr(_observer, "last_folded", None)
                if snap:
                    lines.append("  最近请求 pre=%s chars action=%s (tools↓%s skills↓%s)"
                                 % (snap.get("pre_chars"), snap.get("action"),
                                    snap.get("tools_folded"), snap.get("skills_folded")))
                lines.append("  observe.log=%s" % os.path.join(_dir(), "observe.log"))
                lines.append("  hints=%s" % _hints_path())
                return "\n".join(lines)
            except Exception as exc:
                return "paperhermes panel error: %s" % exc

        try:
            ctx.register_command("paperhermes", handler=_panel,
                                 description="paperhermes 只读状态面板（载荷折叠开关/账本）")
        except Exception as exc:
            logger.debug("paperhermes: register_command skipped (%s)", exc)
        ctx.log("INFO", "paperhermes registered (observe=%s)" % observe)
    except Exception as exc:
        logger.warning("paperhermes: registration degraded: %s", exc)
