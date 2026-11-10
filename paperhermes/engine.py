"""paperhermes engine — 外发载荷折叠核心（纯 stdlib，PROMPT.md v0.4）。

职责分层（可离线单测，不 import 核心）：
  * fold_description(text)        首句折叠，幂等
  * pick_notice_lang(model)       9.7 有序关键词先匹配（小写子串）
  * fold_tools(tools, mode)       tools 槽折叠 + 字节账
  * fold_skills_block(system_text)## Skills 区块降档 + 字节账
  * freeze(key, evaluator)        (会话×模型) 首请求定型、冻结到切换（5.2 缓存铁律）
  * net_savings_gate(...)         净益门（9.1）
  * Observer                      pre/post 记账本（M0）

回程安全依据（3.2/审查件）：intake 校验读 agent.valid_tool_names、dispatch 读 registry、
tool_describe 读 registry 全量 —— 三者都不读外发 payload 的 description，删描述不伤可调用性。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Optional

# ---------------------------------------------------------------- 语言规则（9.7/D10）
# 有序列表非 dict：保“从左到右先匹配者生效”语义。
ZH_KEYWORDS = ("qwen", "deepseek", "glm", "ernie", "step", "minimax", "kimi", "spark")
EN_KEYWORDS = ("gpt", "claude", "gemini", "grok", "mistral")

NOTICES: dict[str, dict[str, str]] = {
    "en": {
        "tools": (
            "Some tool descriptions are shortened to save context; search tool names with "
            "tool_search and read the full schema with tool_describe."
        ),
        "skills": (
            "\n(Skill descriptions are folded here because the list is long — every name is "
            "listed; search with skills_list and load with skill_view(name) as usual.)"
        ),
    },
    "zh": {
        "tools": (
            "部分工具描述已折叠以节省上下文；需要完整说明时用 tool_search 搜索、tool_describe 读取。"
        ),
        "skills": (
            "\n（技能列表较长，此处仅列名称、描述已折叠——按需自行搜索：用 skills_list 查找，"
            "用 skill_view(名字) 加载，功能不受影响。）"
        ),
    },
}


def pick_notice_lang(model: str) -> str:
    m = (model or "").lower()
    for kw in ZH_KEYWORDS:
        if kw in m:
            return "zh"
    for kw in EN_KEYWORDS:
        if kw in m:
            return "en"
    return "en"


# ---------------------------------------------------------------- 折叠原语
_SENT_SPLIT = re.compile(r"(?<=[。！？!?.])\s|\n")


def fold_description(text: str, max_chars: int = 120) -> str:
    """首句折叠：≤max_chars 原样，超长截断+省略号。纯函数、幂等（折叠结果再折叠=自身）。"""
    if not text:
        return text
    first = _SENT_SPLIT.split(text, 1)[0].strip()
    if len(first) <= max_chars:
        return first
    return first[: max_chars - 1].rstrip() + "…"


# ---------------------------------------------------------------- 净益门 + 会话×模型冻结
class Freezer:
    """(session×model) 粒度冻结：首请求评估一次，之后原样返回（5.2 缓存铁律）。

    键含 model —— 切模型=新键，重新定型（验收 4）。评估异常→冻结为 “不介入”，
    本键存活期内不再重试（L2 熔断语义，不回写 settings）。
    """

    def __init__(self) -> None:
        self._frozen: dict[str, Any] = {}

    def frozen(self, key: str) -> bool:
        return key in self._frozen

    def get(self, key: str) -> Any:
        return self._frozen.get(key)

    def decide(self, key: str, evaluator: Callable[[], Any]) -> Any:
        """首请求评估并定型；已有冻结值直接复用。评估异常→定型为 None（不介入）。"""
        if key in self._frozen:
            return self._frozen[key]
        try:
            value = evaluator()
        except Exception:
            value = None  # None 恒判不介入；会话存活期内不再重试
        self._frozen[key] = value
        return value

    def drop(self, key_prefix: str) -> None:
        for k in [k for k in self._frozen if k.startswith(key_prefix)]:
            del self._frozen[k]

    def keys(self):
        return tuple(self._frozen)


def net_gate(gross_savings: int, notice_bytes: int, min_savings: int) -> bool:
    """裁定 9.1：本轮预估回收 − 告知行字节 < 门限 → 完全不介入。"""
    return (gross_savings - notice_bytes) >= min_savings


# ---------------------------------------------------------------- tools 槽折叠
BUILTIN_KEEP = frozenset({"clarify", "tool_search", "tool_describe", "tool_call",
                          "manage_catalog"})  # doctor 排障/自愈入口保持可发现

def keep_name(name: str, user_keep: tuple) -> bool:
    """豁免集：内置关键名 + 用户点名。其余一律可折（MCP/plugin 工具同理——回程三闸门
    不读 payload description，可调用性不受影响）。"""
    if name in BUILTIN_KEEP or name in set(user_keep or ()):
        return True
    return False


def _fn_of(td: dict) -> dict:
    f = td.get("function")
    return f if isinstance(f, dict) else (td if td.get("name") else {})


def fold_tools(tools: list, mode: str, max_chars: int, user_keep: tuple,
               registry_names: frozenset = None) -> tuple[list, int, int]:
    """返回 (new_tools, n_folded, gross_saved_chars)。原列表元素不原地改。

    豁免集 = BUILTIN_KEEP ∪ 用户 keep_eager。tools 数组本身就是本会话准入面
    （host 组装，成员必可 dispatch），不拿 registry 当参照——实测 registry 覆盖面
    因进程/时机而异（built-in 未必注册），拿它 fail-closed 会误伤核心工具。
    可调用性与描述无关（回程三闸门不读 payload description，PROMPT 3.2）。
    registry_names 形参保留仅作签名兼容，不再使用。
    """
    saved = 0
    folded_n = 0
    out = []
    for td in tools:
        try:
            fn = _fn_of(td)
            name = str(fn.get("name") or "")
            desc = fn.get("description")
            if not name or not isinstance(desc, str) or len(desc) < 40:
                out.append(td)
                continue
            if keep_name(name, user_keep):
                # 豁免（内置关键名 / 用户点名）：原样
                out.append(td)
                continue
            folded = fold_description(desc, max_chars)
            if len(folded) >= 0.8 * len(desc):  # 80% 不值得回退（5.1 步骤2）
                out.append(td)
                continue
            if mode == "chat_completions" and isinstance(td.get("function"), dict):
                new_td = dict(td)
                new_td["function"] = {**td["function"], "description": folded}
            elif mode == "anthropic_messages" and isinstance(td, dict) and "input_schema" in td:
                new_td = {**td, "description": folded}
            else:
                out.append(td)  # 协议形状不认识：不动、不计数
                continue
            saved += len(desc) - len(folded)
            folded_n += 1
            out.append(new_td)
        except Exception:
            out.append(td)
    return out, folded_n, saved


# ---------------------------------------------------------------- skills 区块降档
_SKILLS_HEAD = "## Skills"
_ROW_RE = re.compile(r"^(\s*)- ([^:\n]+): (.*)$")


def fold_skills_block(system_text: str, keep_full: frozenset) -> tuple[str, int, int]:
    """在 ## Skills 区块内把 `- name: desc` 行降为 `- name`，除非 name ∈ keep_full。
    返回 (new_text, n_folded, saved_chars)。定位失败/无行可折→原样。纯文本操作、幂等。"""
    start = system_text.find(_SKILLS_HEAD)
    if start < 0:
        return system_text, 0, 0
    m = re.search(r"\n#{1,3} ", system_text[start + len(_SKILLS_HEAD):])
    end = start + len(_SKILLS_HEAD) + m.start() + 1 if m else len(system_text)
    block = system_text[start:end]
    saved = 0
    n = 0
    out_lines = []
    for line in block.split("\n"):
        mm = _ROW_RE.match(line)
        if mm:
            indent, name, desc = mm.group(1), mm.group(2).strip(), mm.group(3)
            if name not in keep_full and len(desc) > 8:
                out_lines.append(f"{indent}- {name}")
                saved += len(desc) + 2
                n += 1
                continue
        out_lines.append(line)
    if n == 0:
        return system_text, 0, 0
    new_block = "\n".join(out_lines)
    return system_text[:start] + new_block + system_text[end:], n, saved


def system_slot_apply(mode: str, request: dict, notice_tools: Optional[str],
                      notice_skills: Optional[str]) -> tuple[dict, int]:
    """告知行注入（4.2）。返回 (request', injected_chars)。哨兵=文本已含该句则跳过。"""
    added = 0
    req = dict(request)
    if mode == "chat_completions":
        msgs = list(req.get("messages") or [])
        if msgs and str(msgs[0].get("role")) in ("system", "developer"):
            m0 = dict(msgs[0])
            content = m0.get("content")
            if isinstance(content, str):
                text = content
                if notice_tools and notice_tools not in text:
                    text = text + " " + notice_tools
                    added += len(notice_tools) + 1
                if notice_skills and "(Skill descriptions are folded" not in text and "描述已折叠" not in text:
                    text = text + notice_skills
                    added += len(notice_skills)
                if added:
                    m0["content"] = text
                    msgs[0] = m0
                    req["messages"] = msgs
        return req, added
    # anthropic_messages
    sysv = req.get("system")
    text_parts: list = []
    if isinstance(sysv, str):
        if not sysv:
            return req, 0
        text_parts = [sysv]
    elif isinstance(sysv, list):
        text_parts = list(sysv)
    else:
        return req, 0
    joined = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in text_parts)
    inject = []
    if notice_tools and notice_tools not in joined:
        inject.append(notice_tools)
    if notice_skills and notice_skills.strip() not in joined:
        inject.append(notice_skills.strip())
    if not inject:
        return req, 0
    tail = " ".join(inject)
    added = len(tail)
    if isinstance(sysv, str):
        req["system"] = sysv + " " + tail
    else:
        # 4.2 规则④：块列表形态落“最后一个纯文本、无 cache_control 的块”；没有就…不动（保守 fail-open）。
        idx = -1
        for i, b in enumerate(text_parts):
            if isinstance(b, dict) and b.get("type") == "text" and "cache_control" not in b:
                idx = i
        if idx < 0:
            return request, 0
        nb = dict(text_parts[idx])
        nb["text"] = nb.get("text", "") + " " + tail
        text_parts[idx] = nb
        req["system"] = text_parts
    return req, added


# ---------------------------------------------------------------- 观测账本（M0）
class Observer:
    def __init__(self, log_path: str) -> None:
        self.path = log_path
        self.last_folded = {"pre_chars": 0, "post_chars": 0, "tools_folded": 0,
                           "skills_folded": 0, "notice_chars": 0, "action": "pass"}

    def record_request(self, request: dict, *, provider: str, model: str, api_mode: str,
                       session_id: str, action: str, tools_folded: int = 0,
                       skills_folded: int = 0, notice_chars: int = 0,
                       est: Optional[dict] = None, post: Optional[dict] = None) -> None:
        pre = _payload_chars(request)
        line = {
            "ts": _now(), "session": session_id, "provider": provider, "model": model,
            "api_mode": api_mode, "action": action, "pre_chars": pre,
            "tools_folded": tools_folded, "skills_folded": skills_folded,
            "notice_chars": notice_chars,
        }
        if post is not None:
            line["post_chars"] = _payload_chars(post)
            line["saved_chars"] = pre - line["post_chars"]
        if est:
            line["est"] = est
        self.last_folded = {"pre_chars": pre, "post_chars": line.get("post_chars", pre),
                            "tools_folded": tools_folded, "skills_folded": skills_folded,
                            "notice_chars": notice_chars, "action": action}
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 观测永不破会话（fail-open，L1）

    def record_outcome(self, new_request: dict) -> None:
        self.last_folded["post_chars"] = _payload_chars(new_request)


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
