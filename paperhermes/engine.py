"""paperhermes engine — 外发载荷折叠核心 v0.5（纯 stdlib，可离线单测，不 import 核心）。

职责分层：本文件只做字符串/字典/快照数据结构操作；Hermes 接缝、配置读取、
持久化、日志全在 __init__.py。

核心概念（PROMPT.md v0.5 §13.2）：快照 snapshot 是冻结契约的载体——
首请求由 plan_session 定型并生成，宿主首次 post_api_request 提交（committed），
此后 apply_snapshot 只按快照里存好的替换文本逐名字套用，不重新折叠、不重读配置。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, List, Optional

SNAPSHOT_VERSION = 2

# ---------------------------------------------------------------- 语言规则（9.7/D10）
# 有序列表非 dict：保"从左到右先匹配者生效"语义。
# v0.5.3：补 hunyuan/qianwen/tongyi/doubao/baichuan 等中文厂商名，并对 `cn:`/`zh:`
# 前缀直接判中文——cn:hy4free 这类自建模型名没有厂商关键词，原先落到英文告知。
ZH_KEYWORDS = ("qwen", "deepseek", "glm", "ernie", "step", "minimax", "kimi", "spark",
               "hunyuan", "qianwen", "tongyi", "doubao", "baichuan", "yi-")
EN_KEYWORDS = ("gpt", "claude", "gemini", "grok", "mistral")

NOTICES: dict[str, dict[str, str]] = {
    "en": {
        "tools": (
            "Some tool descriptions are shortened to save context; search tool names with "
            "tool_search and read the full schema with tool_describe."
        ),
        "skills": (
            "(Skill descriptions are folded here because the list is long — every name is "
            "listed; search with skills_list and load with skill_view(name) as usual.)"
        ),
    },
    "zh": {
        "tools": (
            "部分工具描述已折叠以节省上下文；需要完整说明时用 tool_search 搜索、tool_describe 读取。"
        ),
        "skills": (
            "（技能列表较长，此处仅列名称、描述已折叠——按需自行搜索：用 skills_list 查找，"
            "用 skill_view(名字) 加载，功能不受影响。）"
        ),
    },
}

SKILL_SEARCH_HINTS: dict[str, str] = {
    "en": (
        "Proactively check the skill catalog at the start of each task: if the work may match a "
        "specialized workflow (docs, decks, code review, research, layout, media), search with "
        "skills_list and load it with skill_view(name) before starting. Do not wait for the user "
        "to name a skill; folded skill descriptions do not affect search."
    ),
    "zh": (
        "开工前主动扫一眼技能库：任务可能对应专门流程（写文档、做 PPT、审代码、查资料、排版、多媒体等）时，"
        "先用 skills_list 搜索、再用 skill_view(名字) 加载，不要等用户点名技能；技能描述被折叠不影响检索。"
    ),
}

# 与 NOTICES["skills"] 分工：后者只说明「索引被折过」，这一句是行为指令。
# v0.5.4：只要插件折叠实际介入就注入，不再依赖 fold_skills_index 是否折过技能行。
# （实机 local-62d0334f… / global:glm-5.3-flash：技能索引一条没砍，模型仍没主动找技能。）

SPILL_NOTICE_ZH = "…[paperhermes：中段省略 {omitted} 字符，完整结果已外置到 {path}，可用 read_file 读取]…"


def pick_notice_lang(model: str) -> str:
    m = (model or "").lower()
    # `cn:`/`zh:` 是模型前缀约定（cn:hy4free 之类），比厂商关键词可靠。
    if m.startswith(("cn:", "zh:", "zh-", "zh_")):
        return "zh"
    for kw in ZH_KEYWORDS:
        if kw in m:
            return "zh"
    for kw in EN_KEYWORDS:
        if kw in m:
            return "en"
    return "en"


# ---------------------------------------------------------------- 折叠原语
# v0.4.1 修复（13.9.1）：CJK 句读。！？不要求尾随空白即视为句界；
# ASCII .!? 仍要求尾随空白（防 "e.g. foo" 误断），换行恒为句界。
_SENT_SPLIT = re.compile(r"(?<=[。！？])|(?<=[.!?])[ \t]+|\n")


def fold_description(text: str, max_chars: int = 120) -> str:
    """首句折叠：≤max_chars 原样，超长截断+省略号。纯函数、幂等（折叠结果再折叠=自身）。"""
    if not text:
        return text
    first = _SENT_SPLIT.split(text, 1)[0].strip()
    if len(first) <= max_chars:
        return first
    return first[: max_chars - 1].rstrip() + "…"


# v0.5.3：规则句保留。首句折叠会删掉首句之后的强制/禁止约束——browser_vault 的
# "Never type … password"、terminal 的 "never pipe through head/tail"、write_file 的
# "do NOT re-read the file" 全在首句之后。手写白名单挡不住新插件工具（实机 32 个被折
# 工具里 21 个 lcm_*/minimem_* 无一入册），改为按内容判定：首句 + 含规则标记的句子。
_RULE_RE = re.compile(
    r"(?i)\b(?:must|never|do not|don't|always|only|required|mandatory|important)\b"
    r"|必须|禁止|不要|务必|绝不|严禁|只能|注意|仅")


def fold_description_keep_rules(text: str, max_chars: int = 120,
                                rule_budget: int = 260) -> str:
    """首句折叠 + 保留规则句，总量封顶 max_chars + rule_budget。纯函数、幂等。

    幂等性：二次折叠时首句不变，追加的规则句已 `in out` → 跳过，输出与输入相同。"""
    if not text:
        return text
    first = fold_description(text, max_chars)
    body = text.strip()
    if len(first) >= len(body):
        return first
    out = first
    for sent in _SENT_SPLIT.split(body[len(first):]):
        s = sent.strip()
        if not s or not _RULE_RE.search(s) or s in out:
            continue
        if len(out) + 1 + len(s) > max_chars + rule_budget:
            continue
        out = out + " " + s
    return out


# ---------------------------------------------------------------- 净益门
def net_gate(gross_savings: int, notice_bytes: int, min_savings: int) -> bool:
    """裁定 9.1：本轮预估回收 − 告知行字节 < 门限 → 完全不介入。"""
    return (gross_savings - notice_bytes) >= min_savings


# ---------------------------------------------------------------- tools 槽：形状工具
# 内置豁免：doctor 排障/自愈入口 + 描述里带安全铁律/行为契约的工具。
# 后一类依据 2026-11-10 另一个会话的实机审查：browser_vault 的 "Never type … password"
# 等关键规则都在首句之后，折首句会把安全约束删掉（折后 eager 工具又无回读通道）。
BUILTIN_KEEP = frozenset({
    "clarify", "tool_search", "tool_describe", "tool_call", "manage_catalog",
    "browser_vault", "browser_exec", "delegate_task", "skill_manage",
    "manage_connections", "memory",
})

_DESC_MIN = 40  # 短于此长度的描述没有折叠价值


def keep_name(name: str, user_keep: tuple) -> bool:
    """豁免集：内置关键名 + 用户点名。tools 数组本身就是本会话准入面
    （host 组装，成员必可 dispatch），不拿 registry 当参照。"""
    return name in BUILTIN_KEEP or name in set(user_keep or ())


def _fn_of(td: dict) -> dict:
    f = td.get("function") if isinstance(td, dict) else None
    return f if isinstance(f, dict) else (td if isinstance(td, dict) and td.get("name") else {})


def _schema_of(td: dict, mode: str) -> Optional[dict]:
    """工具参数 schema 的所在槽位（chat_completions=function.parameters /
    anthropic_messages=input_schema）。不认识 → None。"""
    if not isinstance(td, dict):
        return None
    if mode == "chat_completions":
        f = td.get("function")
        sch = f.get("parameters") if isinstance(f, dict) else None
    elif mode == "anthropic_messages":
        sch = td.get("input_schema")
    else:
        sch = None
    return sch if isinstance(sch, dict) else None


def _with_schema(td: dict, mode: str, schema: dict) -> dict:
    if mode == "chat_completions" and isinstance(td.get("function"), dict):
        new_td = dict(td)
        new_td["function"] = {**td["function"], "parameters": schema}
        return new_td
    return {**td, "input_schema": schema}


def _replace_tool_desc(td: dict, mode: str, folded: str) -> dict:
    """按协议形状替换顶层 description；形状不认识或替换后不更短→原对象。"""
    fn = _fn_of(td)
    cur = fn.get("description")
    if not isinstance(cur, str) or len(folded) >= len(cur):
        return td
    if mode == "chat_completions" and isinstance(td.get("function"), dict):
        new_td = dict(td)
        new_td["function"] = {**td["function"], "description": folded}
        return new_td
    if mode == "anthropic_messages" and "input_schema" in td:
        return {**td, "description": folded}
    return td


def fold_tools(tools: list, mode: str, max_chars: int, user_keep: tuple) -> tuple[list, int, int]:
    """M1：折顶层 description。返回 (new_tools, n_folded, gross_saved_chars)。原列表不改。"""
    saved = 0
    folded_n = 0
    out = []
    for td in tools:
        try:
            fn = _fn_of(td)
            name = str(fn.get("name") or "")
            desc = fn.get("description")
            if not name or not isinstance(desc, str) or len(desc) < _DESC_MIN:
                out.append(td)
                continue
            if keep_name(name, user_keep):
                out.append(td)
                continue
            folded = fold_description_keep_rules(desc, max_chars)
            if len(folded) >= 0.8 * len(desc):  # 80% 不值得回退（5.1 步骤2）
                out.append(td)
                continue
            new_td = _replace_tool_desc(td, mode, folded)
            if new_td is td:
                out.append(td)  # 协议形状不认识：不动、不计数
                continue
            saved += len(desc) - len(folded)
            folded_n += 1
            out.append(new_td)
        except Exception:
            out.append(td)
    return out, folded_n, saved


# ---------------------------------------------------------------- M3 参数说明折叠
def _enum_survives(node: dict, folded: str) -> bool:
    """折叠后必须仍含全部枚举字面量，否则不折。

    枚举语义全写在首句之后（lcm_grep.mode 的 full_text/semantic/hybrid 差异、
    content_scope 的 history/externalized/both），折掉等于让调用方盲选——
    选错不报错、不触发校验失败，是隐性错误里最贵的一类。"""
    vals = node.get("enum")
    if not isinstance(vals, list) or not vals:
        return True
    return all(str(x) in folded for x in vals)


def collect_param_folds(schema: dict, max_chars: int,
                        keep_params: frozenset = frozenset()) -> tuple[dict, int]:
    """递归收集 schema 内可折的 description：返回 ({参数名: 折叠文本}, saved_chars)。
    只读 description 字符串；type/properties/required/enum/default/$ref/items/
    anyOf/allOf/oneOf/additionalProperties 等结构与语义键一律不进入返回值。
    同名嵌套参数共享一条目（keep_params 同名全保留的镜像语义）。"""
    folds: dict[str, str] = {}
    saved = 0

    def walk(node: Any, pname: str) -> None:
        nonlocal saved
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "description" and isinstance(v, str):
                    if pname and pname not in keep_params and len(v) >= _DESC_MIN:
                        f = fold_description(v, max_chars)
                        if len(f) < 0.8 * len(v) and _enum_survives(node, f):
                            folds.setdefault(pname, f)
                            saved += len(v) - len(f)
                    continue
                if k == "properties" and isinstance(v, dict):
                    for pk, pv in v.items():
                        walk(pv, str(pk))
                elif isinstance(v, (dict, list)):
                    walk(v, pname)
        elif isinstance(node, list):
            for x in node:
                walk(x, pname)

    walk(schema, "")
    return folds, saved


def apply_param_folds(schema: dict, folds: dict) -> dict:
    """把 collect_param_folds 的结果套回 schema：仅替换同名参数的 description，
    其余字节不动。folds 为空→原对象。"""
    if not folds:
        return schema

    def walk(node: Any, pname: str) -> Any:
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "description" and isinstance(v, str) and pname in folds \
                        and len(folds[pname]) < len(v):
                    out[k] = folds[pname]
                elif k == "properties" and isinstance(v, dict):
                    out[k] = {pk: walk(pv, str(pk)) for pk, pv in v.items()}
                elif isinstance(v, (dict, list)):
                    out[k] = walk(v, pname)
                else:
                    out[k] = v
            return out
        if isinstance(node, list):
            return [walk(x, pname) for x in node]
        return node

    return walk(schema, "")


def split_keep_param_entries(entries) -> dict:
    """('tool.param', ...) → {tool: {param, ...}}；畸形条目忽略。"""
    per: dict[str, set] = {}
    for entry in entries or ():
        s = str(entry)
        if "." in s:
            t, p = s.split(".", 1)
            if t and p:
                per.setdefault(t, set()).add(p)
    return per


# ---------------------------------------------------------------- skills 区块降档（M2/M7）
_SKILLS_HEAD = "## Skills"
_ROW_RE = re.compile(r"^(\s*)- ([^:\n]+): (.*)$")


def _skills_block_span(system_text: str) -> tuple[int, int]:
    start = system_text.find(_SKILLS_HEAD)
    if start < 0:
        return -1, -1
    m = re.search(r"\n#{1,3} ", system_text[start + len(_SKILLS_HEAD):])
    end = start + len(_SKILLS_HEAD) + m.start() + 1 if m else len(system_text)
    return start, end


def collect_skill_folds(system_text: str, keep_full: frozenset, trigger_only: bool = False,
                        max_chars: int = 120) -> tuple[dict, int]:
    """收集 ## Skills 区块内可折行：{name: 替换文本}——name-only 模式为 ""，
    M7 触发句模式为首句。keep_full 内名字不出现（=保持全量）。返回 (folds, saved)。"""
    start, end = _skills_block_span(system_text)
    if start < 0:
        return {}, 0
    folds: dict[str, str] = {}
    saved = 0
    for line in system_text[start:end].split("\n"):
        mm = _ROW_RE.match(line)
        if not mm:
            continue
        name, desc = mm.group(2).strip(), mm.group(3)
        if name in keep_full or len(desc) <= 8:
            continue
        if trigger_only:
            tri = fold_description(desc, max_chars)
            if len(tri) < 0.8 * len(desc):
                folds[name] = tri
                saved += len(desc) - len(tri)
        else:
            folds[name] = ""
            saved += len(desc) + 2
    return folds, saved


def apply_skill_folds_text(system_text: str, folds: dict) -> tuple[str, int]:
    """把技能折叠集按名字套回文本。folds 外的行（新技能）原样保留。幂等。"""
    if not folds:
        return system_text, 0
    start, end = _skills_block_span(system_text)
    if start < 0:
        return system_text, 0
    n = 0
    out_lines = []
    for line in system_text[start:end].split("\n"):
        mm = _ROW_RE.match(line)
        if mm:
            indent, name = mm.group(1), mm.group(2).strip()
            if name in folds:
                rep = folds[name]
                repl = f"{indent}- {name}" if rep == "" else f"{indent}- {name}: {rep}"
                if repl != line:
                    out_lines.append(repl)
                    n += 1
                    continue
        out_lines.append(line)
    if n == 0:
        return system_text, 0
    return system_text[:start] + "\n".join(out_lines) + system_text[end:], n


def fold_skills_block(system_text: str, keep_full: frozenset, trigger_only: bool = False,
                      max_chars: int = 120) -> tuple[str, int, int]:
    """一次性便捷封装：collect + apply。返回 (new_text, n_folded, saved_chars)。"""
    folds, saved = collect_skill_folds(system_text, keep_full, trigger_only, max_chars)
    if not folds:
        return system_text, 0, 0
    new_text, n = apply_skill_folds_text(system_text, folds)
    if n == 0:
        return system_text, 0, 0
    return new_text, n, saved


def _system_text_of(request: dict, api_mode: str) -> str:
    if api_mode == "anthropic_messages":
        sysv = request.get("system")
        if isinstance(sysv, str):
            return sysv
        if isinstance(sysv, list):
            return " ".join(str(b.get("text", "")) for b in sysv if isinstance(b, dict))
        return ""
    msgs = request.get("messages") or []
    if msgs and isinstance(msgs[0], dict) and str(msgs[0].get("role")) in ("system", "developer") \
            and isinstance(msgs[0].get("content"), str):
        return msgs[0]["content"]
    return ""


def _write_system_text(request: dict, api_mode: str, new_text: str) -> dict:
    if api_mode == "anthropic_messages":
        return {**request, "system": new_text}
    msgs = list(request.get("messages") or [])
    if msgs and isinstance(msgs[0], dict) and isinstance(msgs[0].get("content"), str):
        msgs[0] = {**msgs[0], "content": new_text}
        return {**request, "messages": msgs}
    return request


# ---------------------------------------------------------------- 告知行注入（4.2）
def _join_notice(text: str, notice: str) -> str:
    """v0.4.1 修复（13.9.2）：告知句另起一行，不再黏连上一句。"""
    return text.rstrip() + "\n" + notice.strip()


def system_slot_apply(mode: str, request: dict, notice_tools: Optional[str],
                      notice_skills: Optional[str],
                      notice_hint: Optional[str] = None) -> tuple[dict, int]:
    """告知行注入。返回 (request', injected_chars)。哨兵=文本已含该句则跳过。

    v0.5.4：notice_hint 是技能主动发现指令，与折了什么无关；默认 None 保持旧调用兼容。
    """
    added = 0
    req = dict(request)
    if mode == "chat_completions":
        msgs = list(req.get("messages") or [])
        if msgs and isinstance(msgs[0], dict) \
                and str(msgs[0].get("role")) in ("system", "developer"):
            content = msgs[0].get("content")
            if isinstance(content, str):
                text = content
                if notice_tools and notice_tools not in text:
                    text = _join_notice(text, notice_tools)
                    added += len(notice_tools.strip()) + 1
                if notice_skills and notice_skills.strip() not in text:
                    text = _join_notice(text, notice_skills)
                    added += len(notice_skills.strip()) + 1
                if notice_hint and notice_hint.strip() not in text:
                    text = _join_notice(text, notice_hint)
                    added += len(notice_hint.strip()) + 1
                if added:
                    msgs[0] = {**msgs[0], "content": text}
                    req["messages"] = msgs
        return req, added
    # anthropic_messages
    sysv = req.get("system")
    if isinstance(sysv, str):
        if not sysv:
            return req, 0
        inject = []
        if notice_tools and notice_tools not in sysv:
            inject.append(notice_tools.strip())
        if notice_skills and notice_skills.strip() not in sysv:
            inject.append(notice_skills.strip())
        if notice_hint and notice_hint.strip() not in sysv:
            inject.append(notice_hint.strip())
        if not inject:
            return req, 0
        tail = "\n".join(inject)
        req["system"] = sysv.rstrip() + "\n" + tail
        return req, len(tail) + 1
    if isinstance(sysv, list):
        joined = "".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in sysv)
        inject = []
        if notice_tools and notice_tools not in joined:
            inject.append(notice_tools.strip())
        if notice_skills and notice_skills.strip() not in joined:
            inject.append(notice_skills.strip())
        if notice_hint and notice_hint.strip() not in joined:
            inject.append(notice_hint.strip())
        if not inject:
            return req, 0
        # 4.2 规则④：落"最后一个纯文本、无 cache_control 的块"；没有就不动（fail-open）。
        idx = -1
        for i, b in enumerate(sysv):
            if isinstance(b, dict) and b.get("type") == "text" and "cache_control" not in b:
                idx = i
        if idx < 0:
            return request, 0
        blocks = [dict(b) if isinstance(b, dict) else b for b in sysv]
        tail = "\n".join(inject)
        blocks[idx]["text"] = str(blocks[idx].get("text", "")).rstrip() + "\n" + tail
        req["system"] = blocks
        return req, len(tail) + 1
    return req, 0


# ---------------------------------------------------------------- 快照：定型与套用（13.2）
def plan_session(request: dict, api_mode: str, st: dict, model: str,
                 keep_full: frozenset, compact_store: Optional[dict] = None,
                 compact_path: str = "") -> dict:
    """首请求定型（纯函数）：从当前设置与首请求载荷算出全部折叠文本与净益门结果。
    st = 全部设置值（已由接线层强转）。
    compact_store 非空且 use_compact_store=true 时，工具描述改用辅助模型生成的忠实
    摘要（逐字段 sha256 校验，过期/缺失的工具原样不折）；否则退回 M1 首句折果。"""
    max_chars = int(st.get("fold_first_sentence_max_chars") or 120)
    user_keep = tuple(st.get("keep_eager") or ())
    lang = pick_notice_lang(model)
    hint = (SKILL_SEARCH_HINTS.get(lang, SKILL_SEARCH_HINTS["en"])
            if st.get("inject_skill_search_hint") else "")
    fold_t = bool(st.get("fold_tool_desc"))
    fold_p = bool(st.get("fold_tool_param_desc"))
    fold_s = bool(st.get("fold_skills_index"))
    trigger_only = bool(st.get("fold_skills_trigger_only")) and fold_s
    use_compact = bool(st.get("use_compact_store")) and isinstance(compact_store, dict)

    folded_tools: dict[str, str] = {}
    folded_params: dict[str, dict] = {}
    est_tools = est_params = 0
    compact_used = 0
    compact_stale: list = []
    kp_map = split_keep_param_entries(st.get("keep_param_desc"))
    for td in list(request.get("tools") or []):
        try:
            fn = _fn_of(td)
            name = str(fn.get("name") or "")
            if not name or keep_name(name, user_keep):
                continue
            desc = fn.get("description")
            sch = _schema_of(td, api_mode)
            compact_hit = False
            if use_compact:
                hit = compact_lookup(compact_store, name, desc or "", sch or {})
                if hit is not None:
                    folded_tools[name] = hit["description"]
                    est_tools += len(str(desc or "")) - len(hit["description"])
                    compact_used += 1
                    compact_hit = True
                    if not hit.get("schema_ok"):
                        # 结构 hash 变了（换协议/上游改 schema）：描述照用，只记一笔
                        compact_stale.append(name)
                elif isinstance(desc, str) and len(desc) >= _DESC_MIN:
                    compact_stale.append(name)
            if not compact_hit and fold_t:
                # 精简库缺失/过期时退回 M1 首句折果：宁可少省，不能一刀不砍。
                # v0.5.3 起改用规则句保留版，首句之后的禁令/强制句不再被删。
                if isinstance(desc, str) and len(desc) >= _DESC_MIN:
                    f = fold_description_keep_rules(desc, max_chars)
                    if len(f) < 0.8 * len(desc):
                        folded_tools[name] = f
                        est_tools += len(desc) - len(f)
            if fold_p:
                if sch:
                    pf, s = collect_param_folds(sch, max_chars,
                                                frozenset(kp_map.get(name, ())))
                    if pf:
                        folded_params[name] = pf
                        est_params += s
        except Exception:
            continue

    folded_skills: dict[str, str] = {}
    est_skills = 0
    if fold_s:
        folded_skills, est_skills = collect_skill_folds(
            _system_text_of(request, api_mode), keep_full, trigger_only, max_chars)
        # 净益：技能档折出的字符不够付告知行就不折也不告知。实机触发句档只折 1 行
        # 省 32 字符，却注入 ~148 字符告知，净亏且让模型以为索引被截断。
        if folded_skills and est_skills <= len(NOTICES[lang]["skills"]) + 1:
            folded_skills, est_skills = {}, 0

    notice_tools = compact_notice(lang, compact_path) if (use_compact and compact_used) else None
    notice_bytes = 0
    if folded_tools or folded_params:
        # 按实际会注入的那条告知行计：compact 告知带库路径，比 NOTICES 短句长 200+ 字符，
        # 拿短句估会系统性高估净收益（实机估 285、实注入 509）。
        notice_bytes += len(notice_tools or NOTICES[lang]["tools"]) + 1
    if folded_skills:
        notice_bytes += len(NOTICES[lang]["skills"]) + 1
    # v0.5.4：按实际会注入的那条指令计字节，不能用短句估算（净益门会被系统性高估）。
    if hint:
        notice_bytes += len(hint.strip()) + 1
    gate = int(st.get("min_savings_chars") or 2048)
    gross = est_tools + est_params + est_skills
    act = bool((folded_tools or folded_params or folded_skills)
               and net_gate(gross, notice_bytes, gate))
    return {"act": act, "lang": lang, "folded_tools": folded_tools,
            "folded_params": folded_params, "folded_skills": folded_skills,
            "est": [est_tools, est_params, est_skills, notice_bytes, gate],
            "compact_used": compact_used, "compact_stale": compact_stale,
            "notice_tools": notice_tools, "notice_hint": hint}


def _base_snapshot(session_id: str, api_mode: str, st: dict, lang: str,
                   model: str = "") -> dict:
    return {"version": SNAPSHOT_VERSION, "session_id": session_id, "api_mode": api_mode,
            "model": model, "created_at": _now(), "committed": False, "melted": False,
            "settings": {k: v for k, v in st.items() if not str(k).startswith("_")},
            "lang": lang, "decision": {"act": False},
            "folded_tools": {}, "folded_params": {}, "folded_skills": {},
            "est": [0, 0, 0, 0, 0],
            "selfcheck_done": False, "error_streak": 0, "suggest_disable_next": False,
            "protocol_warned": False, "spill_seq": 0}


def snapshot_from_plan(session_id: str, api_mode: str, st: dict, plan: dict,
                       model: str = "") -> dict:
    snap = _base_snapshot(session_id, api_mode, st, plan.get("lang", "en"), model)
    snap["decision"] = {"act": bool(plan.get("act"))}
    snap["folded_tools"] = dict(plan.get("folded_tools") or {})
    snap["folded_params"] = {k: dict(v) for k, v in (plan.get("folded_params") or {}).items()}
    snap["folded_skills"] = dict(plan.get("folded_skills") or {})
    snap["est"] = list(plan.get("est") or [0, 0, 0, 0, 0])
    # 告知句整段冻结（含精简库路径），后续请求不重算、不重选语言
    snap["notice_tools"] = plan.get("notice_tools") or ""
    snap["notice_hint"] = plan.get("notice_hint") or ""
    snap["compact_used"] = int(plan.get("compact_used") or 0)
    snap["compact_stale"] = list(plan.get("compact_stale") or [])
    return snap


def inert_snapshot(session_id: str, api_mode: str, st: dict, model: str) -> dict:
    """全开关 false 的首请求也要落一份惰性快照：OFF 状态本身同属冻结对象
    （双向切换纪律——会话中途开开关不得生效）。"""
    return _base_snapshot(session_id, api_mode, st, pick_notice_lang(model), model)


def sanitize_snapshot(snap: Any) -> Optional[dict]:
    """盘读快照的最小校验：坏快照→None（接线层旁路+WARN，不重建新形态）。"""
    if not isinstance(snap, dict):
        return None
    if not isinstance(snap.get("session_id"), str) or not isinstance(snap.get("decision"), dict):
        return None
    for k in ("folded_tools", "folded_skills", "folded_params"):
        if not isinstance(snap.get(k), dict):
            snap[k] = {}
    if not isinstance(snap.get("settings"), dict):
        snap["settings"] = {}
    snap.setdefault("melted", False)
    snap.setdefault("committed", False)
    snap.setdefault("lang", "en")
    # v0.5.4：旧盘上快照没有 notice_hint —— 缺键按空串，冻结形态不因版本升级漂移。
    if not isinstance(snap.get("notice_hint"), str):
        snap["notice_hint"] = ""
    snap.setdefault("spill_seq", 0)
    snap.setdefault("model", "")
    snap.setdefault("model_switches", 0)
    if not isinstance(snap.get("model"), str):
        snap["model"] = ""
    return snap


def apply_snapshot(request: dict, snap: dict, api_mode: str):
    """按快照套用冻结形态。返回 (new_request|None, counts, applied_saved_chars)。
    counts 含 tools/params/skills/notice_chars；protocol_mismatch=True=协议漂移旁路。"""
    counts = {"tools": 0, "params": 0, "skills": 0, "notice_chars": 0,
              "protocol_mismatch": False}
    if snap.get("melted") or not (snap.get("decision") or {}).get("act"):
        return None, counts, 0
    if api_mode != snap.get("api_mode"):
        counts["protocol_mismatch"] = True
        return None, counts, 0
    out = request
    saved = 0
    ft = snap.get("folded_tools") or {}
    fp = snap.get("folded_params") or {}
    if ft or fp:
        new_tools = []
        changed = False
        for td in list(out.get("tools") or []):
            try:
                name = str(_fn_of(td).get("name") or "")
                ntd = td
                if name and name in ft:
                    r = _replace_tool_desc(ntd, api_mode, ft[name])
                    if r is not ntd:
                        saved += len(str(_fn_of(ntd).get("description") or "")) - len(ft[name])
                        ntd = r
                        counts["tools"] += 1
                        changed = True
                pm = fp.get(name) if name else None
                if pm:
                    sch = _schema_of(ntd, api_mode)
                    if sch:
                        new_sch = apply_param_folds(sch, pm)
                        if new_sch is not sch:
                            ntd = _with_schema(ntd, api_mode, new_sch)
                            counts["params"] += len(pm)
                            changed = True
                new_tools.append(ntd)
            except Exception:
                new_tools.append(td)
        if changed:
            out = {**out, "tools": new_tools}

    fs = snap.get("folded_skills") or {}
    skills_notice_done = False
    if fs:
        lang = snap.get("lang", "en")
        if api_mode == "anthropic_messages" and isinstance(out.get("system"), list):
            sysv = out["system"]
            idx_b = next((i for i, b in enumerate(sysv)
                          if isinstance(b, dict) and _SKILLS_HEAD in str(b.get("text", ""))), -1)
            if idx_b >= 0:
                blocks = [dict(b) if isinstance(b, dict) else b for b in sysv]
                new_text, n2 = apply_skill_folds_text(str(blocks[idx_b].get("text", "")), fs)
                if n2:
                    if NOTICES[lang]["skills"].strip() not in new_text:
                        new_text = _join_notice(new_text, NOTICES[lang]["skills"])
                    blocks[idx_b]["text"] = new_text
                    out = {**out, "system": blocks}
                    counts["skills"] = n2
                    skills_notice_done = True
        else:
            text = _system_text_of(out, api_mode)
            new_text, n2 = apply_skill_folds_text(text, fs)
            if n2:
                out = _write_system_text(out, api_mode, new_text)
                counts["skills"] = n2

    if not (counts["tools"] or counts["params"] or counts["skills"]):
        return None, counts, 0  # 事实门：快照有形态但本轮载荷无对应成员 → 不介入不注入

    lang = snap.get("lang", "en")
    notice_tools = snap.get("notice_tools") or NOTICES[lang]["tools"]
    notice_skills = NOTICES[lang]["skills"]
    out, notice_added = system_slot_apply(
        api_mode, out,
        notice_tools if (counts["tools"] or counts["params"]) else None,
        None if skills_notice_done else (notice_skills if counts["skills"] else None),
        snap.get("notice_hint") or None)
    counts["notice_chars"] = notice_added
    return out, counts, saved


# ---------------------------------------------------------------- M5/M6 工具结果处理
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][0-2]")

# F8：只删"帧"形态的行，不删排版。空行是段落间距/列表分块，属于正文结构；
# 旧版把所有空行一起删掉，等于静默压平每轮工具结果的版式。
_BAR_CHARS = frozenset("=#*+-_.·•…|/\\<>~^■□◆◇▣▤▥▦▧▨▩█▓▒░▏▎▍▌▋▊▉")
_BAR_RUN_RE = re.compile(r"[=#*\-_.]{3,}|[█▓▒░▏▎▍▌▋▊▉]{3,}")
_PCT_RE = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")
_TIMER_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?</")
_RATE_RE = re.compile(r"\d+(?:\.\d+)?\s?[kKmMgG]?[bB]/s")
_RATIO_RE = re.compile(r"\d+\s?/\s?\d+")
_SPINNER_RE = re.compile(r"^[\\|/\-]+$")
_BARE_FRAME_RE = re.compile(r"^[\s\d.,:%/|\[\]()<>\-+]*$")


def _is_bare_progress_frame(line: str) -> bool:
    """重绘帧只剩数字/百分比/标点（"100%"、" 45 % "）→ 进度残留，没有内容。"""
    s = line.strip()
    return bool(s) and bool(_PCT_RE.search(s)) and bool(_BARE_FRAME_RE.match(s))


def _is_noise_frame(line: str) -> bool:
    """帧形态判定：纯符号填充 / 百分比进度条 / 旋转帧。空行一律 False（F8）。"""
    s = line.strip()
    if not s:
        return False
    if len(s) >= 6 and all(ch in _BAR_CHARS or ch.isspace() for ch in s):
        return True  # ======-------- / ███████ 这类纯填充分隔
    if len(s) <= 160 and _PCT_RE.search(s):
        # 百分比得配上进度条/耗时/速率/计数才算进度帧；散文里的百分数不动
        if (_BAR_RUN_RE.search(s) or _TIMER_RE.search(s)
                or _RATE_RE.search(s) or _RATIO_RE.search(s)):
            return True
    if len(s) <= 4 and _SPINNER_RE.match(s):
        return True  # | / - \ 旋转帧
    return False


def clean_terminal_noise(text: str) -> Optional[str]:
    """M5b：去 ANSI、进度条中间帧（\\r 重绘只留最后一帧）、纯符号填充、连续重复行。

    F8 修订：只有"帧"形态的行（只含 ANSI 控制符、\\r 回车覆写帧、纯符号填充、
    百分比进度条）才删；正常空行与段落间距原样保留——旧版连空行一起删，
    每轮工具结果的排版就被静默扭曲了。
    无变化或变更后不更短→None（不值得替换）。
    """
    if not isinstance(text, str) or not text:
        return None
    t = text.replace("\r\n", "\n")  # 先归一 CRLF，别把正常行尾当回车覆写
    lines: List[str] = []
    for raw in t.split("\n"):
        ln = _ANSI_RE.sub("", raw)
        if ln != raw and not ln.strip():
            continue  # 只含 ANSI 控制符的行（清行/光标定位帧）
        if "\r" in ln:
            ln = ln.split("\r")[-1]  # 回车覆写：同一行只留最后一帧
            # 帧本身是噪声（空帧/进度条/只剩百分比）→ 整行剔除；有实义文字则留最后一帧
            if not ln.strip() or _is_noise_frame(ln) or _is_bare_progress_frame(ln):
                continue
        elif _is_noise_frame(ln):
            continue
        lines.append(ln)
    out: List[str] = []
    for ln in lines:
        if out and out[-1] == ln:
            continue  # 连续重复行（连续多个空行也只留一个间隔）
        out.append(ln)
    start, end = 0, len(out)
    while start < end and not out[start].strip():
        start += 1  # 首尾空白行只是包裹，不是段落间距
    while end > start and not out[end - 1].strip():
        end -= 1
    out = out[start:end]
    t2 = "\n".join(out)
    if t2 != text and len(t2) < len(text):
        return t2
    return None


def minify_json_result(text: str) -> Optional[str]:
    """M5a：完整可解析的 JSON 结果去格式化空白。非 JSON/无收益→None。"""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t or t[0] not in "[{":
        return None
    try:
        obj = json.loads(t)
    except Exception:
        return None
    mini = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return mini if len(mini) < len(text) else None


def should_spill(text: str, min_chars: int) -> bool:
    return isinstance(text, str) and len(text) > max(1024, int(min_chars or 0))


def safe_filename_piece(s: str, limit: int = 40) -> str:
    """spill 路径组件消毒：只留 [A-Za-z0-9_.-]，防路径穿越。"""
    out = re.sub(r"[^A-Za-z0-9_.-]", "_", str(s or ""))[:limit].strip("._")
    return out or "x"


def spill_render(head: str, tail: str, omitted: int, path: str) -> str:
    return head + "\n" + SPILL_NOTICE_ZH.format(omitted=omitted, path=path) + "\n" + tail


# ---------------------------------------------------------------- M8 忠实摘要库（LLM 离线生成）
COMPACT_VERSION = 1
RULE_MARKERS = ("never", "must", "always", "do not", "don't", "禁止", "必须", "不得", "不要", "切勿", "一律")


def canonical_schema(schema: Any) -> str:
    """参数 schema 的规范化原文（哈希基准，键序稳定）。"""
    try:
        return json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(schema)


def field_sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8", errors="replace")).hexdigest()


def extract_tool_originals(request: dict, api_mode: str) -> dict:
    """从宿主每次组装的原始载荷里收割工具原文（折叠发生在副本上，这里恒为原文）。
    返回 {name: {"description": str, "schema": dict}}。"""
    out: dict[str, dict] = {}
    for td in request.get("tools") or []:
        try:
            fn = _fn_of(td)
            name = str(fn.get("name") or "")
            if not name:
                continue
            desc = fn.get("description")
            sch = _schema_of(td, api_mode) or {}
            out[name] = {"description": desc if isinstance(desc, str) else "",
                         "schema": sch}
        except Exception:
            continue
    return out


def make_compact_entry(original_desc: str, original_schema: Any,
                       compact_desc: str, compact_schema: str) -> dict:
    """JSON 条目：每个字段单独记原文 sha256 + 精简明文 + 原文（回读通道）。"""
    desc_o, schema_o = str(original_desc or ""), canonical_schema(original_schema)
    return {
        "description": {"sha256": field_sha256(desc_o), "compact": compact_desc,
                        "original": desc_o, "chars": len(compact_desc)},
        "schema": {"sha256": field_sha256(schema_o), "compact": compact_schema,
                   "original": schema_o, "chars": len(compact_schema)},
    }


def validate_compact(compact_desc: str, original_desc: str) -> tuple[bool, str]:
    """辅助模型产出的安全闸门：必须更短、非空，且原文里的祈使/禁令规则不得全丢。"""
    c = (compact_desc or "").strip()
    o = str(original_desc or "")
    if not c:
        return False, "empty"
    if len(c) >= len(o):
        return False, "not shorter"
    o_low = o.lower()
    c_low = c.lower()
    had = [m for m in RULE_MARKERS if m.lower() in o_low]
    if had and not any(m.lower() in c_low for m in had):
        return False, "rule markers lost: " + ",".join(had)
    return True, ""


def compact_lookup(store: Optional[dict], name: str, original_desc: str, original_schema: Any):
    """按工具名取精简版。描述原文 sha256 必须匹配；schema 摘要只作软信号。

    返回 {"description": 摘要, "schema": 参数摘要, "schema_ok": bool} 或 None。
    说明：精简库里的 schema 文本从不写回载荷（apply 只换 description、另走参数折叠），
    所以 schema 不匹配不该否决整条记录——载荷 schema 随 api_mode 变形
    （anthropic 去 nullable union、moonshot 清洗），按结构 hash 硬门会让换协议后
    整库失效，正是「全开关不生效」的成因。"""
    tools = (store or {}).get("tools") or {}
    entry = tools.get(name)
    if not isinstance(entry, dict):
        return None
    d, s = entry.get("description") or {}, entry.get("schema") or {}
    if d.get("sha256") != field_sha256(str(original_desc or "")):
        return None
    cd, cs = str(d.get("compact") or ""), str(s.get("compact") or "")
    ok, _why = validate_compact(cd, str(original_desc or ""))
    if not ok:
        return None
    schema_ok = s.get("sha256") == field_sha256(canonical_schema(original_schema))
    return {"description": cd, "schema": cs, "schema_ok": schema_ok}


def schema_digest(schema: Any) -> str:
    """确定性的参数摘要兜底：模型没产出合格 schema 精简版时用这个。"""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    bits = []
    for pname, p in props.items():
        if not isinstance(p, dict):
            continue
        bits.append("%s(%s%s%s)" % (
            pname, str(p.get("type") or "any"),
            ",必填" if pname in required else "",
            ",默认%s" % p.get("default") if "default" in p else ""))
    return "; ".join(bits)


def compact_notice(lang: str, path: str) -> str:
    """提示词：调用工具前先看一眼精简库（完整原文/参数摘要都在里面）；
    tool_search / tool_describe 返回的仍是原文。

    v0.5.3：同一会话里可能有「库里命中」与「缺库回退首句折叠」两种工具，
    只指库文件会让后一类工具的回读指路落空——两条路都写出来。"""
    if lang == "zh":
        return ("工具描述已压缩为忠实摘要或首句（安全与行为规则保留）；需要完整原文时："
                "库里有条目的工具用 read_file 读 %s 里该工具的 original 字段，"
                "库里没有的工具（或需要参数细节）用 tool_describe 读注册表原文。"
                "tool_search / tool_describe 返回的仍是原文。" % path)
    return ("Tool descriptions are compressed to faithful briefs or first sentences "
            "(safety and behavior rules preserved). For the full text: read_file %s and "
            "use that tool's 'original' field when the store has an entry; for tools the "
            "store does not cover (or for parameter details) use tool_describe, which "
            "returns the unmodified originals." % path)


# ---------------------------------------------------------------- M0.5 请求构成账本
def _sum_desc_strings(node: Any) -> int:
    total = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "description" and isinstance(v, str):
                total += len(v)
            elif isinstance(v, (dict, list)):
                total += _sum_desc_strings(v)
    elif isinstance(node, list):
        for x in node:
            total += _sum_desc_strings(x)
    return total


def payload_composition(request: dict, api_mode: str) -> dict:
    """分区字节账（chars，JSON 序列化口径）：system / tools_schema / tools 顶层描述 /
    参数内描述 / 历史消息 / 工具结果消息。只计数不存正文。"""
    comp = {"system_chars": 0, "tools_schema_chars": 0, "tool_desc_chars": 0,
            "param_desc_chars": 0, "history_chars": 0, "tool_result_chars": 0}
    try:
        comp["system_chars"] = len(_system_text_of(request, api_mode))
    except Exception:
        pass
    tools = request.get("tools") or []
    try:
        comp["tools_schema_chars"] = len(json.dumps(tools, ensure_ascii=False, default=str))
    except Exception:
        pass
    for td in tools:
        try:
            fn = _fn_of(td)
            d = fn.get("description")
            if isinstance(d, str):
                comp["tool_desc_chars"] += len(d)
            sch = _schema_of(td, api_mode) or _schema_of(td, "chat_completions") \
                or _schema_of(td, "anthropic_messages")
            if sch:
                comp["param_desc_chars"] += _sum_desc_strings(sch)
        except Exception:
            continue
    msgs = request.get("messages") or []
    sys_skip = 1 if (api_mode == "chat_completions" and msgs and isinstance(msgs[0], dict)
                     and str(msgs[0].get("role")) in ("system", "developer")) else 0
    for m in msgs[sys_skip:]:
        try:
            n = len(json.dumps(m, ensure_ascii=False, default=str))
            comp["history_chars"] += n
            if isinstance(m, dict):
                role = str(m.get("role") or "")
                content = m.get("content")
                is_tool_result = role == "tool" or (
                    isinstance(content, list)
                    and any(isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content))
                if is_tool_result:
                    comp["tool_result_chars"] += n
        except Exception:
            continue
    return comp


# ---------------------------------------------------------------- M4 defer 只读审计
def audit_defer(defer_list, payload_tools: list, heavy_threshold: int = 2000) -> dict:
    """只读裁定：defer 清单里从未在载荷出现的名字（疑似失效或本就延迟，需人工核实）
    + 载荷里描述总量超过阈值却未 defer 的重工具（defer 候选）。不改任何配置。"""
    defer_set = {str(x) for x in (defer_list or [])}
    seen = set()
    heavy = []
    for td in payload_tools or []:
        try:
            fn = _fn_of(td)
            name = str(fn.get("name") or "")
            if not name:
                continue
            seen.add(name)
            if name in defer_set:
                continue
            desc = fn.get("description")
            total = len(desc) if isinstance(desc, str) else 0
            sch = _schema_of(td, "chat_completions") or _schema_of(td, "anthropic_messages")
            if sch:
                total += _sum_desc_strings(sch)
            if total >= heavy_threshold:
                heavy.append({"name": name, "desc_chars": total})
        except Exception:
            continue
    stale = sorted(defer_set - seen)
    heavy.sort(key=lambda x: -x["desc_chars"])
    return {"stale_or_deferred": stale, "heavy_candidates": heavy}


# ---------------------------------------------------------------- 观测账本（M0/M0.5）
OBSERVE_LOG_MAX_BYTES = 10 * 1024 * 1024   # v0.5.5 #2：超限轮转，保留一份 .1


def cleanup_spill_dir(root: str, ttl_hours: int = 24, now: float = None) -> int:
    """v0.5.5 #1：删除 mtime 超龄的 spill 文件，返回删除数。纯函数、不递归创建任何
    东西；删不掉的文件静默跳过（调用方负责节流与日志）。"""
    import os as _os
    import time as _time
    if not _os.path.isdir(root):
        return 0
    cutoff = (now if now is not None else _time.time()) - ttl_hours * 3600
    removed = 0
    for dirpath, _dirs, files in _os.walk(root):
        for fn in files:
            fp = _os.path.join(dirpath, fn)
            try:
                if _os.path.getmtime(fp) < cutoff:
                    _os.remove(fp)
                    removed += 1
            except OSError:
                continue
    return removed


OBSERVE_LOG_KEEP = 1
OBSERVE_LOG_KEEP = 1


class Observer:
    def __init__(self, log_path: str, max_bytes: int = OBSERVE_LOG_MAX_BYTES) -> None:
        self.path = log_path
        # 测试可注小值；生产恒 ≥1MB 下限防手滑配 1024 之类把日志切成碎片
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.last_folded = {"pre_chars": 0, "post_chars": 0, "tools_folded": 0,
                            "skills_folded": 0, "notice_chars": 0, "action": "pass"}

    def _write(self, line: dict) -> None:
        try:
            # v0.5.5 #2：大小轮转（13.4/v0.4.2 门）。超限把旧档挪到 .1（覆盖旧 .1），
            # 新行落回干净文件。观测永不因轮转失败破会话（fail-open，L1）。
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) >= self.max_bytes:
                    rotated = self.path + ".1"
                    if os.path.exists(rotated):
                        os.remove(rotated)
                    os.replace(self.path, rotated)
            except OSError:
                pass  # 轮转失败就继续追加，绝不让观测打断请求
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 观测永不破会话（fail-open，L1）

    def record_request(self, request: dict, *, provider: str, model: str, api_mode: str,
                       session_id: str, action: str, tools_folded: int = 0,
                       skills_folded: int = 0, params_folded: int = 0,
                       notice_chars: int = 0, api_request_id: str = "",
                       composition: Optional[dict] = None,
                       post: Optional[dict] = None) -> None:
        pre = _payload_chars(request)
        line = {
            "kind": "request", "ts": _now(), "session": session_id, "provider": provider,
            "model": model, "api_mode": api_mode, "action": action, "pre_chars": pre,
            "tools_folded": tools_folded, "skills_folded": skills_folded,
            "params_folded": params_folded, "notice_chars": notice_chars,
        }
        if api_request_id:
            line["api_request_id"] = api_request_id
        if composition:
            line["composition"] = composition
        if post is not None:
            line["post_chars"] = _payload_chars(post)
            line["saved_chars"] = pre - line["post_chars"]
        self.last_folded = {"pre_chars": pre, "post_chars": line.get("post_chars", pre),
                            "tools_folded": tools_folded, "skills_folded": skills_folded,
                            "notice_chars": notice_chars, "action": action}
        self._write(line)

    def record_usage(self, api_request_id: str, session_id: str, model: str,
                     usage: Any) -> None:
        """M0.5：post_api_request 的真实 usage 回执，按 api_request_id 与请求行关联。"""
        if not api_request_id or not isinstance(usage, dict):
            return
        line = {"kind": "usage", "ts": _now(), "api_request_id": api_request_id,
                "session": session_id, "model": model,
                "usage": {k: v for k, v in usage.items()
                          if isinstance(v, (int, float, str, bool))}}
        self._write(line)


def _payload_chars(request: dict) -> int:
    try:
        return len(json.dumps(request, ensure_ascii=False, default=str))
    except Exception:
        return -1


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha1_12(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]
