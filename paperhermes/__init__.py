"""paperhermes — 外发载荷瘦身插件（PROMPT.md v0.5 接线层）。

register(ctx) 全部 fail-open：任何异常只 WARN，不影响会话。行为由
plugins.entries.paperhermes.settings 开关门控（D8 v0.5 逐键补齐）：每次启动只补
settings 里不存在的键，显式 false/0/空字符串/空列表永不覆盖。

分层：engine.py = 纯字符串/字典/快照逻辑（离线可测）；本文件 = Hermes 接缝、
配置读取、快照持久化（$HERMES_HOME/paperhermes/session_freezes.json）。

v0.5.1 冻结契约（13.2）：首请求生成 pending 快照并先落盘，宿主首次 post_api_request
提交为 frozen；此后 apply 只读快照。冻结键只有 session_id——换协议、热重载、
进程重启都不重冻；**模型切换视作新会话**（前缀缓存本已失效，替换该键的快照并
按当前配置重定型）。会话中途例外：用户命令 /paperhermes meltdown（急停，
回到原始形态，本会话不可撤销，跨模型切换仍粘滞）与 rearm（连续两次）。
盘外落点：observe.log / session_hints.json / session_freezes.json / spill/。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Optional

from . import engine

logger = logging.getLogger("paperhermes")

PLUGIN_ID = "paperhermes"
VERSION = "0.5.5"
DEFAULTS: dict[str, Any] = {
    "observe": True,                    # 只读观测项默认 true（D11/裁定#6）
    "observe_composition": False,       # M0.5 请求构成账（只读但量大，默认关）
    "fold_tool_desc": False,            # M1
    "fold_tool_param_desc": False,      # M3
    "fold_skills_index": False,         # M2
    "fold_skills_dynamic": False,       # M2 增强（依赖 fold_skills_index）
    "fold_skills_trigger_only": False,  # M7（依赖 fold_skills_index）
    "keep_eager": [],
    "keep_param_desc": [],
    "fold_first_sentence_max_chars": 120,
    "min_savings_chars": 2048,
    "audit_defer": False,                       # M4 只读审计
    "minify_json_tool_results": False,          # M5a
    "strip_terminal_progress_noise": False,     # M5b
    "spill_large_tool_results": False,          # M6
    # M6 阈值：宿主 terminal 输出上限是 50,000（tool_output.max_bytes 默认值），
    # 结果到 50K 已被宿主截断，阈值设 50,000 等于永不触发。20,000 才落在可达区间。
    "spill_min_chars": 20000,
    "spill_head_chars": 4000,
    "spill_tail_chars": 4000,
    "spill_exclude_tools": [],
    "use_compact_store": False,         # M8：用辅助模型生成的忠实摘要替换工具描述
    "inject_skill_search_hint": False,  # v0.5.4：介入时注入技能主动发现指令（不改任何折叠）
}
# 修改类开关：任一为 true 才需要真实定型快照；audit/observe 是只读项不算。
MOD_SWITCHES = ("fold_tool_desc", "fold_tool_param_desc", "fold_skills_index",
                "minify_json_tool_results", "strip_terminal_progress_noise",
                "spill_large_tool_results", "use_compact_store")
AUX_TASK_KEY = "paperhermes_compact"
REARM_WINDOW_SECONDS = 120
FOLDABLE_MODES = frozenset({"anthropic_messages", "chat_completions"})
SIGNAL_TTL_DAYS = 30
# v0.5.5 #1：spill 惰性清理（13.7「24 小时后台清理」）。不带独立开关——
# 这是资源卫生而非修改类行为；文件超龄即删，删不掉只 debug 记一笔。
SPILL_TTL_HOURS = 24
_spill_cleanup_at = 0.0
ERROR_BREAKER_THRESHOLD = 5
_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off", ""}

_observer: Optional[engine.Observer] = None
_snapshots: dict[str, dict] = {}     # session_id -> pending/frozen 快照
_snapshot_bad: set = set()           # 盘上快照损坏的会话（旁路，不重建新形态）
_session_signals: dict[str, set] = {}
_seen_tool_names: set = set()        # M4：本进程观测到的载荷工具名
_last_payload_tools: list = []       # M4：最近一次载荷 tools 引用（审计重工具用）
_freezes_loaded = False
_tool_originals: dict = {}           # 田间收割：name -> 原文（description + schema）
_compact_store: Optional[dict] = None  # M8 精简库（懒加载，盘上 tool_compact.json）
_compact_loaded = False
_rearm_pending: dict = {}            # sid -> 首次 rearm 的时间戳（连续两次才执行）
_switch_counts: dict = {}            # sid -> 本会话累计模型切换次数（面板可见）


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", "")


def _dir() -> str:
    base = _home()
    d = os.path.join(base, "paperhermes") if base else os.path.join(tempfile.gettempdir(),
                                                                    "paperhermes")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.join(tempfile.gettempdir(), "paperhermes")
        os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 设置（D8 v0.5 逐键补齐）
def _settings_keys_present():
    """读 settings 现有键集合；探测失败→None（保守不写）。"""
    try:
        from hermes_cli.config import load_config_readonly
        conf = load_config_readonly() or {}
        entries = (conf.get("plugins") or {}).get("entries") or {}
        for key, val in entries.items():
            if key == PLUGIN_ID or str(key).endswith("/" + PLUGIN_ID):
                st = (val or {}).get("settings") if isinstance(val, dict) else None
                return set(st) if isinstance(st, dict) else set()
        return set()
    except Exception as exc:
        logger.debug("paperhermes: settings probe failed (%s)", exc)
        return None


def _backfill_defaults(ctx) -> None:
    """逐键补齐：只写不存在的键；显式 false/0/空字符串/空列表永不覆盖。"""
    try:
        present = _settings_keys_present()
        if present is None:
            return
        missing = [k for k in DEFAULTS if k not in present]
        for k in missing:
            ctx.set_config(k, DEFAULTS[k])
        if missing:
            _ctx_log(ctx, "INFO", "paperhermes: backfilled %d default setting key(s): %s"
                    % (len(missing), ", ".join(missing)))
    except Exception as exc:
        logger.warning("paperhermes: backfill failed (%s) — running on code defaults", exc)


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


def _ctx_log(ctx, level: str, msg: str) -> None:
    """写一行宿主日志。v0.23.0 的 PluginContext 没有 .log 方法，直接调用会让
    register() 抛 AttributeError 被记成 "registration degraded"；缺失时退回 logger。"""
    fn = getattr(ctx, "log", None)
    if callable(fn):
        try:
            fn(level, msg)
            return
        except Exception:
            pass
    lv = {"WARN": logging.WARNING, "ERROR": logging.ERROR}.get(str(level).upper(), logging.INFO)
    try:
        logger.log(lv, msg)
    except Exception:
        pass


def _read_settings(ctx) -> dict:
    return {k: _get(ctx, k) for k in DEFAULTS}


# ---------------------------------------------------------------- 冻结快照持久化（13.2）
def _freezes_path() -> str:
    return os.path.join(_dir(), "session_freezes.json")


def _atomic_json(path: str, data: dict) -> bool:
    try:
        fd, tmp = tempfile.mkstemp(dir=_dir(), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        logger.warning("paperhermes: atomic json write failed for %s (%s)", path, exc)
        return False


def _originals_path() -> str:
    return os.path.join(_dir(), "tool_originals.json")


def _compact_path() -> str:
    return os.path.join(_dir(), "tool_compact.json")


def _harvest_originals(request: dict, api_mode: str) -> None:
    """每次请求都在折叠之前收割工具原文（宿主组装的原始载荷），供 CLI 生成精简库。
    只在内容变化时落盘，避免每请求一次写。"""
    global _tool_originals
    try:
        fresh = engine.extract_tool_originals(request, api_mode)
    except Exception:
        return
    changed = False
    for name, val in fresh.items():
        if _tool_originals.get(name) != val:
            _tool_originals[name] = val
            changed = True
    if changed:
        try:
            with open(_originals_path(), encoding="utf-8") as f:
                disk = json.load(f)
        except Exception:
            disk = {}
        tools = _originals_map(disk)
        tools.update(fresh)
        _atomic_json(_originals_path(), {"version": 1, "tools": tools})


def _originals_map(data: Any) -> dict:
    """把盘上 originals 解成 {tool_name: {description, schema}}。

    旧版合并逻辑写坏过：`disk.update(fresh)` 之后又整体塞进 "tools"，产生
    {"tools": {"tools": {...}, ...}} 的嵌套。这里把嵌套层展开，坏文件下次收割即自愈。"""
    if not isinstance(data, dict):
        return {}
    tools = data.get("tools")
    if not isinstance(tools, dict):
        return {}
    out = {k: v for k, v in tools.items() if isinstance(v, dict)}
    inner = out.pop("tools", None)
    if isinstance(inner, dict):
        for k, v in inner.items():
            if isinstance(v, dict):
                out.setdefault(k, v)
    return out


def _load_originals() -> dict:
    global _tool_originals
    try:
        with open(_originals_path(), encoding="utf-8") as f:
            data = json.load(f)
        tools = _originals_map(data)
        if tools:
            _tool_originals = tools
    except Exception:
        pass
    return _tool_originals


def _load_compact_store(reload: bool = False) -> Optional[dict]:
    global _compact_store, _compact_loaded
    if _compact_loaded and not reload:
        return _compact_store
    _compact_loaded = True
    try:
        with open(_compact_path(), encoding="utf-8") as f:
            data = json.load(f)
        _compact_store = data if isinstance(data, dict) and isinstance(data.get("tools"), dict) \
            else None
    except Exception:
        _compact_store = None
    return _compact_store


def _registry_originals() -> dict:
    """CLI 侧的兜底来源：公开 tools.registry 接口（覆盖注册在册的工具；
    payload 收割不到的内置/桥接工具可在此补齐）。"""
    out = {}
    try:
        from tools.registry import registry
        names = registry.get_all_tool_names()
        defs = registry.get_definitions(set(names), quiet=True)
        for td in defs:
            fn = td.get("function") if isinstance(td.get("function"), dict) else td
            name = str(fn.get("name") or "")
            if not name:
                continue
            out[name] = {"description": str(fn.get("description") or ""),
                         "schema": fn.get("parameters") or td.get("input_schema") or {}}
    except Exception as exc:
        logger.debug("paperhermes: registry scan skipped (%s)", exc)
    return out


def _load_freezes() -> None:
    """热重载/重启后按 session_id 恢复冻结快照；损坏条目不重建，只旁路+WARN。"""
    global _freezes_loaded
    if _freezes_loaded:
        return
    _freezes_loaded = True
    try:
        with open(_freezes_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.warning("paperhermes: session_freezes.json unreadable (%s) — all frozen "
                       "sessions will bypass (no rebuild)", exc)
        return
    if not isinstance(data, dict):
        return
    for sid, snap in (data.get("sessions") or {}).items():
        s = engine.sanitize_snapshot(snap)
        if s is None:
            _snapshot_bad.add(str(sid))
            logger.warning("paperhermes: corrupt freeze snapshot for session %s — bypass", sid)
        else:
            _snapshots[str(sid)] = s


def _persist_freezes() -> bool:
    """原子写全部快照。首请求修改载荷前必须先落盘成功（13.2 步骤4）。"""
    try:
        payload = {"version": engine.SNAPSHOT_VERSION,
                   "sessions": dict(_snapshots)}
        fd, tmp = tempfile.mkstemp(dir=_dir(), suffix=".freezes")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _freezes_path())
        return True
    except Exception as exc:
        logger.warning("paperhermes: freeze persist failed (%s)", exc)
        return False


# ---------------------------------------------------------------- hint 文件（动态升档，30 天 TTL）
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


# ---------------------------------------------------------------- 技能 keep-full 集（5.2 静态规则）
# 镜像 agent/coding_context._NON_CODING_SKILL_CATEGORIES（v0.23.0 抄录）。
# 不 import 的原因：该模块不在 plugin_validate_private_imports._PUBLIC 允许面。
MIRROR_NON_CODING_ROOTS = frozenset({
    "apple", "communication", "cooking", "creative", "email", "finance", "gaming", "gifs",
    "health", "media", "music", "note-taking", "productivity", "shopping", "smart-home",
    "social-media", "travel", "yuanbao",
})


def _keep_full_skill_names(text: str, dynamic_on: bool) -> frozenset:
    """编码类目全保留；dynamic_on=true 才读 hints。名字永不删，只有描述可折。
    v0.5 修复：不再并入其他会话的 signal 集（跨会话串扰缺陷）。"""
    keep: set = set(_hint_names()) if dynamic_on else set()
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


# ---------------------------------------------------------------- 请求处理（13.2 冻结流转）
def _compact_for_plan(ctx, st: dict):
    """M8：决定本次定型用不用精简库。
    返回 None=用旧 M1 首句折果；dict=精简库（空 dict 表示库缺失/损坏 → 精简路径不启用，
    有 fold_tool_desc 的工具仍退回 M1 首句折叠（13.12「不许一刀不砍」），
    不会静默回到会砍规则的正则版）。"""
    if not bool(st.get("use_compact_store")):
        return None
    store = _load_compact_store()
    if store is None:
        logger.warning("paperhermes: use_compact_store=true but %s is missing/broken — "
                       "tool descriptions stay original this session; run "
                       "'hermes paperhermes compact-tools'", _compact_path())
        return {}
    return store


def _record_error(session_id: str, exc: Exception) -> None:
    """熔断纪律：单次异常 fail-open；连续超阈只记"下一会话建议停用"，
    本会话形态永不变（13.2 第 10 点）。"""
    snap = _snapshots.get(session_id)
    if snap is None:
        return
    try:
        snap["error_streak"] = int(snap.get("error_streak") or 0) + 1
        if snap["error_streak"] >= ERROR_BREAKER_THRESHOLD \
                and not snap.get("suggest_disable_next"):
            snap["suggest_disable_next"] = True
            logger.warning("paperhermes: session %s hit %d consecutive errors — 建议下一会话"
                           "关闭修改类开关（本会话形态不变）", session_id, snap["error_streak"])
            _persist_freezes()
    except Exception:
        pass


def _handle_request(request: dict, ctx, api_mode: str, session_id: str, model: str):
    """返回 (new_request|None, counts, action)。异常由中间件外层兜底成 fail-open。"""
    if api_mode not in FOLDABLE_MODES:
        return None, {}, "bypass-mode"
    if not session_id:
        # 退化场景（宿主没给 session_id）：不落盘不冻结，按当前配置即折即发。
        st = _read_settings(ctx)
        if not any(bool(st[k]) for k in MOD_SWITCHES):
            return None, {}, "switches-off"
        _harvest_originals(request, api_mode)
        cstore = _compact_for_plan(ctx, st)
        keep_full = _keep_full_skill_names(
            engine._system_text_of(request, api_mode),
            bool(st.get("fold_skills_dynamic"))) if st.get("fold_skills_index") else frozenset()
        plan = engine.plan_session(request, api_mode, st, model, keep_full,
                                   cstore, _compact_path())
        snap = engine.snapshot_from_plan("", api_mode, st, plan, model)
        new_req, counts, _ = engine.apply_snapshot(request, snap, api_mode)
        return (new_req, counts, "fold" if new_req else "pass")
    _harvest_originals(request, api_mode)
    _load_freezes()
    if session_id in _snapshot_bad:
        return None, {}, "snapshot-corrupt"
    snap = _snapshots.get(session_id)
    if snap is not None and not snap.get("melted"):
        stored_model = str(snap.get("model") or "")
        if not stored_model:
            # 旧版快照/命令创建的占位快照没有 model 字段：补记一次，不重冻、不换形态
            snap["model"] = model
            _persist_freezes()
        elif stored_model != model:
            # 模型切换 = 前缀缓存失效 = 视为新会话（主人 2026-11-10 裁定）。
            # 重冻零成本，且让改过的配置/精简库在这里自然生效。
            logger.info("[PAPERHERMES] session=%s model switched (%s -> %s) — "
                        "re-freezing as a new session", session_id, stored_model, model)
            _switch_counts[session_id] = int(snap.get("model_switches") or 0) + 1
            _snapshots.pop(session_id, None)
            snap = None
    if snap is not None and snap.get("melted"):
        return None, {}, "melted"
    if snap is None:
        st = _read_settings(ctx)
        if not any(bool(st[k]) for k in MOD_SWITCHES):
            # 惰性快照冻结 OFF 状态本身（双向切换纪律：会话中途开开关不得生效）。
            snap = engine.inert_snapshot(session_id, api_mode, st, model)
            snap["model_switches"] = _switch_counts.get(session_id, 0)
            _snapshots[session_id] = snap
            _persist_freezes()
            # 实机踩坑（2026-11-10 另一个会话）：手改磁盘 config.yaml 不热生效——插件宿主
            # 进程持启动时注入的 config snapshot；首个请求又把 OFF 态锁进快照，导致
            # "明明改了开关却一刀没砍"。把解法写进日志与面板，别让用户再去猜。
            try:
                _ctx_log(ctx, "WARN", "[PAPERHERMES] session=%s frozen OFF: 若刚改过 config.yaml，"
                        "请重启 Hermes 桌面端（插件宿主需重新载入配置）并开【新会话】；"
                        "已冻结的 OFF 快照不会自行解冻。" % session_id)
            except Exception:
                pass
            return None, {}, "switches-off"
        cstore = _compact_for_plan(ctx, st)
        keep_full = _keep_full_skill_names(
            engine._system_text_of(request, api_mode),
            bool(st.get("fold_skills_dynamic"))) if st.get("fold_skills_index") else frozenset()
        plan = engine.plan_session(request, api_mode, st, model, keep_full,
                                   cstore, _compact_path())
        snap = engine.snapshot_from_plan(session_id, api_mode, st, plan, model)
        snap["model_switches"] = _switch_counts.get(session_id, 0)
        if plan.get("compact_stale"):
            logger.warning("paperhermes: compact store stale/missing for %d tools (%s) — "
                           "fell back to first-sentence folding; run "
                           "'hermes paperhermes compact-tools' to refresh",
                           len(plan["compact_stale"]), ", ".join(plan["compact_stale"][:8]))
        _snapshots[session_id] = snap
        if plan["act"] and not _persist_freezes():
            # 先持久化才允许修改载荷：落盘失败 → fail-open，本轮原样发送
            del _snapshots[session_id]
            logger.warning("paperhermes: freeze snapshot persist failed — request untouched")
            return None, {}, "persist-failed-failopen"
        if not plan["act"]:
            _persist_freezes()
            return None, {}, "gate-closed"
    new_req, counts, _saved = engine.apply_snapshot(request, snap, api_mode)
    if counts.get("protocol_mismatch"):
        if not snap.get("protocol_warned"):
            snap["protocol_warned"] = True
            logger.warning("paperhermes: session %s api_mode switched (%s -> %s) — bypassing, "
                           "frozen shape kept", session_id, snap.get("api_mode"), api_mode)
        return None, counts, "protocol-mismatch"
    if new_req is None:
        return None, counts, "pass"
    if not snap.get("selfcheck_done"):
        snap["selfcheck_done"] = True
        _ctx_log(ctx, "INFO", "[PAPERHERMES] session=%s committed=%s tools↓%d params↓%d skills↓%d"
                % (session_id, snap.get("committed"), counts.get("tools", 0),
                   counts.get("params", 0), counts.get("skills", 0)))
    return new_req, counts, "fold"


# ---------------------------------------------------------------- M6 外置（spill）
def _spill(snap: dict, sid: str, tool_name: str, text: str) -> str:
    st = snap.get("settings") or {}
    head = max(0, int(st.get("spill_head_chars") or 4000))
    tail = max(0, int(st.get("spill_tail_chars") or 4000))
    if head + tail >= len(text):
        return text
    seq = int(snap.get("spill_seq") or 0) + 1
    snap["spill_seq"] = seq
    d = os.path.join(_dir(), "spill", engine.safe_filename_piece(sid, 48))
    os.makedirs(d, exist_ok=True)
    fname = "%d-%04d-%s-%d.txt" % (int(time.time()), seq,
                                   engine.safe_filename_piece(tool_name),
                                   time.perf_counter_ns() % 1000000)
    path = os.path.abspath(os.path.join(d, fname))
    # 防路径穿越：最终路径必须仍在 spill 目录内
    if not path.startswith(os.path.abspath(os.path.join(_dir(), "spill")) + os.sep):
        return text
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    omitted = len(text) - head - tail
    return engine.spill_render(text[:head], text[-tail:] if tail else "", omitted, path)


# ---------------------------------------------------------------- M4 defer 只读审计
def _read_defer_config() -> list:
    try:
        from hermes_cli.config import load_config_readonly
        conf = load_config_readonly() or {}
        dl = ((conf.get("tools") or {}).get("tool_search") or {}).get("defer")
        return [str(x) for x in dl] if isinstance(dl, (list, tuple)) else []
    except Exception:
        return []


# ---------------------------------------------------------------- 命令：面板 / stats / meltdown
def _norm(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _meltdown(ctx, sid: str) -> str:
    """用户急停（13.2 第 13 点）：只能由命令触发；熔断态写入快照持久化；
    不写 settings、不影响其他会话、本会话不可撤销。"""
    if not sid:
        return "paperhermes: meltdown 需要在会话内使用（拿不到 session_id）。"
    _load_freezes()
    snap = _snapshots.get(sid)
    if snap is None:
        snap = engine.inert_snapshot(sid, "", _read_settings(ctx), "")
        _snapshots[sid] = snap
    if snap.get("melted"):
        return "paperhermes: 本会话已处于 meltdown 熔断态（急停不可撤销，新会话自动恢复）。"
    snap["melted"] = True
    ok = _persist_freezes()
    msg = "[PAPERHERMES] meltdown engaged (session=%s)" % sid
    _ctx_log(ctx, "INFO", msg)
    logger.info(msg)
    out = ("paperhermes: 本会话已急停——此后所有修改类行为（折叠/清理/外置/告知行）原样透传。"
           "新会话按配置正常生效。")
    if not ok:
        out += "（警告：熔断状态落盘失败，进程重启后需重新急停）"
    return out


def _stats() -> str:
    path = os.path.join(_dir(), "observe.log")
    if not os.path.exists(path):
        return "paperhermes: observe.log 还不存在（observe=%s 时才记账）。" % (
            _observer is not None)
    n = folds = saved_total = usage_lines = 0
    saved_list = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("kind") == "usage":
                    usage_lines += 1
                    continue
                n += 1
                if rec.get("action") == "fold":
                    folds += 1
                sc = rec.get("saved_chars")
                if isinstance(sc, int):
                    saved_total += sc
                    saved_list.append(sc)
    except Exception as exc:
        return "paperhermes: stats read failed: %s" % exc
    saved_list.sort()
    med = saved_list[len(saved_list) // 2] if saved_list else 0
    return ("paperhermes 账本（observe.log）\n"
            "  请求行=%d（fold=%d） usage 回执行=%d\n"
            "  累计回收=%d chars（≈%d tok，按 4 chars/tok 粗估） 中位单次=%d chars"
            % (n, folds, usage_lines, saved_total, saved_total // 4, med))


def _panel(ctx, sid: str) -> str:
    st = _read_settings(ctx)
    _load_freezes()
    lines = ["paperhermes v%s —— 状态面板（stats 看账本；compact-tools 刷新摘要库；"
             "meltdown 急停；rearm 连续两次恢复）" % VERSION]
    lines.append("  observe=%s composition=%s audit_defer=%s use_compact_store=%s"
                 % (st["observe"], st["observe_composition"], st["audit_defer"],
                    st["use_compact_store"]))
    lines.append("  fold_tool_desc=%s fold_tool_param_desc=%s fold_skills_index=%s "
                 "dynamic=%s trigger_only=%s"
                 % (st["fold_tool_desc"], st["fold_tool_param_desc"], st["fold_skills_index"],
                    st["fold_skills_dynamic"], st["fold_skills_trigger_only"]))
    lines.append("  minify_json=%s strip_noise=%s spill=%s(>%s chars) "
                 "max_chars=%s min_savings=%s"
                 % (st["minify_json_tool_results"], st["strip_terminal_progress_noise"],
                    st["spill_large_tool_results"], st["spill_min_chars"],
                    st["fold_first_sentence_max_chars"], st["min_savings_chars"]))
    lines.append("  keep_eager=%s keep_param_desc=%s spill_exclude=%s"
                 % (list(st["keep_eager"]), list(st["keep_param_desc"]),
                    list(st["spill_exclude_tools"])))
    lines.append("  冻结快照=%d（committed=%d melted=%d 损坏旁路=%d） 信号会话=%d"
                 % (len(_snapshots),
                    sum(1 for s in _snapshots.values() if s.get("committed")),
                    sum(1 for s in _snapshots.values() if s.get("melted")),
                    len(_snapshot_bad), len(_session_signals)))
    if sid and sid in _snapshots:
        snap = _snapshots[sid]
        lines.append("  本会话：act=%s committed=%s melted=%s suggest_disable_next=%s "
                     "compact_used=%s model=%s 模型切换=%s 次"
                     % (snap.get("decision", {}).get("act"), snap.get("committed"),
                        snap.get("melted"), snap.get("suggest_disable_next"),
                        snap.get("compact_used", 0), snap.get("model") or "?",
                        snap.get("model_switches", 0)))
        if not snap.get("decision", {}).get("act") and not snap.get("melted"):
            lines.append("  ⚠ 本会话为冻结 OFF 态：改过 config.yaml 也不会在本会话生效——"
                         "重启 Hermes 并开【新会话】、切换一次模型（缓存已失效，自动重冻），"
                         "或连续两次 /paperhermes rearm 重定型。")
        frozen_st = snap.get("settings") or {}
        diff = sorted(k for k in DEFAULTS
                      if k in frozen_st and _norm(st[k]) != _norm(frozen_st[k]))
        if diff:
            lines.append("  pending_next_session（本会话已冻结，以下改动只在新会话生效）: %s"
                         % ", ".join(diff))
    lf = getattr(_observer, "last_folded", None)
    if lf:
        lines.append("  最近请求 pre=%s chars action=%s (tools↓%s params↓%s skills↓%s)"
                     % (lf.get("pre_chars"), lf.get("action"), lf.get("tools_folded"),
                        lf.get("params_folded", 0), lf.get("skills_folded")))
    cstore = _load_compact_store()
    lines.append("  精简库=%s 条目=%s 原文收割=%d" %
                 ("已加载" if cstore else "缺失/未生成",
                  len((cstore or {}).get("tools") or {}), len(_tool_originals)))
    if sid in _rearm_pending:
        lines.append("  ⏳ rearm 待确认：%d 秒内再输入一次 /paperhermes rearm" %
                     REARM_WINDOW_SECONDS)
    if st["audit_defer"]:
        rep = engine.audit_defer(_read_defer_config(), _last_payload_tools)
        lines.append("  defer 审计：疑似失效/延迟项=%s"
                     % (", ".join(rep["stale_or_deferred"]) or "无"))
        lines.append("  defer 候选（描述最重未 defer）=%s"
                     % (", ".join("%s(%d)" % (h["name"], h["desc_chars"])
                                  for h in rep["heavy_candidates"][:5]) or "无"))
    lines.append("  状态目录=%s" % _dir())
    return "\n".join(lines)


# ---------------------------------------------------------------- M8 CLI：辅助模型生成忠实摘要库
COMPACT_BATCH = 5


def _compact_prompt(items: list) -> list:
    """items: [(name, original_desc, canonical_schema)]。要求严格 JSON 输出。"""
    payload = {name: {"description": desc, "schema": schema}
               for name, desc, schema in items}
    system = (
        "你是工具描述压缩器。把每个工具的 description 压缩成简短但忠实的中文（或原文语言）摘要，"
        "同时把 parameters schema 压成一行参数摘要。\n"
        "硬性规则：\n"
        "1. 所有含 never/always/must/do not/禁止/必须/不得/不要/切勿/一律 的句子必须保留语义，"
        "可以改写但绝不能删除；安全铁律、调用纪律、格式约定、数量限制一律不许丢。\n"
        "2. description 摘要必须明显短于原文，但须说明工具做什么、何时用、关键约束。\n"
        "3. schema 摘要保留参数名、类型、必填/默认值/枚举上限等关键信息，一行内。\n"
        "4. 只输出严格 JSON 对象，键=工具名，值={\"description\": \"...\", \"schema\": \"...\"}。"
        "不要输出解释、Markdown 或代码块。"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _parse_compact_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        data = json.loads(t)
    except Exception:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(t[start:end + 1])
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _compact_one_batch(ctx, items: list) -> dict:
    llm = getattr(ctx, "llm", None)
    if llm is None:
        return {}
    messages = _compact_prompt(items)
    try:
        res = llm.complete(messages=messages, task=AUX_TASK_KEY,
                           max_tokens=1600, timeout=180,
                           purpose="paperhermes compact-tools")
    except Exception:
        # 辅助槽没注册/被信任策略拒绝 → 退回宿主默认辅助模型，不让命令卡死
        try:
            res = llm.complete(messages=messages, max_tokens=1600, timeout=180,
                               purpose="paperhermes compact-tools")
        except Exception as exc:
            logger.warning("paperhermes: auxiliary LLM call failed (%s)", exc)
            return {}
    return _parse_compact_json(getattr(res, "text", ""))


def _decode_schema(text: str) -> dict:
    try:
        obj = json.loads(text) if text else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _live_tool_originals() -> dict:
    """现场构建当前工具面，再抽 name/description/schema。

    与宿主组装载荷同源（`model_tools.get_tool_definitions`，含 toolset 选择与
    tool_search 折叠），所以拿到的就是这一版实际会发出去的描述与 schema，
    不依赖上一次会话有没有收割过文件。"""
    out: dict = {}
    try:
        import model_tools
        # 默认工具面只是「当前平台默认选中的一小撮」（实测 CLI 进程仅 12 个），
        # 会漏掉桌面会话里的插件工具。要的是全目录：把所有已知 toolset 打开、
        # 且跳过 tool_search 折叠，才能覆盖任意会话可能发出去的每个工具。
        try:
            avail = list(model_tools.get_available_toolsets() or {})
        except Exception:
            avail = []
        defs = model_tools.get_tool_definitions(enabled_toolsets=avail or None,
                                                quiet_mode=True,
                                                skip_tool_search_assembly=True)
    except Exception as exc:
        logger.debug("paperhermes: live tool list unavailable (%s)", exc)
        return out
    for td in defs or []:
        try:
            if not isinstance(td, dict):
                continue
            fn = td.get("function") if isinstance(td.get("function"), dict) else td
            name = str(fn.get("name") or "")
            if not name:
                continue
            out[name] = {"description": str(fn.get("description") or ""),
                         "schema": fn.get("parameters") or td.get("input_schema") or {}}
        except Exception:
            continue
    return out


def _run_compact_tools(ctx, dry_run: bool = False, only: str = "") -> str:
    live = _live_tool_originals()
    harvested = _load_originals()
    if live and harvested:
        # 现场目录 + 会话收割互补：lcm_*/minimem_* 这类由 agent 运行时追加的工具
        # 不在 get_tool_definitions 里，只能从真实载荷收割补齐；同名以现场为准。
        merged = dict(harvested)
        merged.update(live)
        source = "live+harvested"
    elif live:
        merged, source = live, "live"
    elif harvested:
        merged, source = dict(harvested), "harvested"
    else:
        merged, source = dict(_registry_originals()), "registry"
    wanted = {x.strip() for x in (only or "").split(",") if x.strip()}
    items = []
    for name, val in sorted(merged.items()):
        if not isinstance(val, dict) or name in engine.BUILTIN_KEEP:
            continue
        if wanted and name not in wanted:
            continue
        desc = str(val.get("description") or "")
        if len(desc) < engine._DESC_MIN:
            continue
        items.append((name, desc, engine.canonical_schema(val.get("schema") or {})))
    if not items:
        msg = ("paperhermes compact-tools: 没有可处理的工具原文（来源=%s）。"
               "确认 --only 名字正确，或先跑一次会话。" % source)
        print(msg, flush=True)
        return msg
    batches = (len(items) + COMPACT_BATCH - 1) // COMPACT_BATCH
    print("[paperhermes] 工具清单 %d 个（来源=%s），分 %d 批、每批 %d 个调用 %s"
          % (len(items), source, batches, COMPACT_BATCH, AUX_TASK_KEY), flush=True)
    entries, rejected = {}, []
    for bi, i in enumerate(range(0, len(items), COMPACT_BATCH), start=1):
        batch = items[i:i + COMPACT_BATCH]
        print("[paperhermes] 进度 %d/%d 批：%s" % (bi, batches, ", ".join(b[0] for b in batch)),
              flush=True)
        got = _compact_one_batch(ctx, batch)
        for name, desc, schema_o in batch:
            cand = got.get(name) if isinstance(got, dict) else None
            cd = str((cand or {}).get("description") or "").strip()
            cs = str((cand or {}).get("schema") or "").strip()
            ok, why = engine.validate_compact(cd, desc)
            if not ok:
                rejected.append("%s(%s)" % (name, why))
                continue
            if not cs or len(cs) >= len(schema_o):
                cs = engine.schema_digest(_decode_schema(schema_o))
            entries[name] = engine.make_compact_entry(desc, _decode_schema(schema_o), cd, cs)
        print("[paperhermes] 进度 %d/%d 批完成，合格 %d 条、拒绝 %d 条"
              % (bi, batches, len(entries), len(rejected)), flush=True)
    if not entries:
        msg = ("paperhermes compact-tools: 辅助模型没有产出任何合格摘要（全部被安全闸门拒绝）。\n"
               "拒绝明细: " + ", ".join(rejected[:12]))
        print(msg, flush=True)
        return msg
    store = {"version": engine.COMPACT_VERSION, "generated_at": engine._now(),
             "aux_task": AUX_TASK_KEY, "source": source, "tools": entries}
    if dry_run:
        msg = ("paperhermes compact-tools [dry-run]: 合格 %d/%d 条，未写盘。\n"
               "拒绝: %s" % (len(entries), len(items), ", ".join(rejected[:12]) or "无"))
        print(msg, flush=True)
        return msg
    if not _atomic_json(_compact_path(), store):
        msg = "paperhermes compact-tools: 写盘失败（见日志），精简库未更新。"
        print(msg, flush=True)
        return msg
    global _compact_store, _compact_loaded
    _compact_store, _compact_loaded = store, True
    msg = ("paperhermes compact-tools: 已写入 %s\n"
           "  来源=%s 合格 %d/%d 条；被安全闸门拒绝 %d 条（原文保留）"
           % (_compact_path(), source, len(entries), len(items), len(rejected)))
    print(msg, flush=True)
    return msg


# ---------------------------------------------------------------- 恢复命令（连续两次）
def _rearm(ctx, sid: str) -> str:
    if not sid:
        return "paperhermes: rearm 需要在会话内使用（拿不到 session_id）。"
    now = time.time()
    prev = _rearm_pending.get(sid)
    if prev is None or now - float(prev) > REARM_WINDOW_SECONDS:
        _rearm_pending[sid] = now
        return ("⚠ /paperhermes rearm 需要【连续输入两次】才会执行（%d 秒内有效）。\n"
                "它会删掉本会话的冻结快照，下一次请求按当前配置重新定型——会断一次缓存前缀，"
                "且中途 ON→OFF / OFF→ON 都允许。再输入一次 /paperhermes rearm 确认。"
                % REARM_WINDOW_SECONDS)
    _rearm_pending.pop(sid, None)
    _load_freezes()
    _snapshots.pop(sid, None)
    _snapshot_bad.discard(sid)
    _switch_counts.pop(sid, None)
    ok = _persist_freezes()
    msg = "[PAPERHERMES] rearm engaged (session=%s)" % sid
    _ctx_log(ctx, "INFO", msg)
    logger.info(msg)
    return ("paperhermes: 本会话冻结快照已删除，下一次请求按当前配置重新定型。"
            "（重启后同样生效；其他会话不受影响）"
            if ok else "paperhermes: 内存已重置但快照文件写入失败（见日志）。")


# ---------------------------------------------------------------- register
def _lazy_cleanup() -> None:
    """v0.5.5：spill/hints 惰性清理，节流 1 小时。绝不删活跃 spill——只按文件 mtime
    判断超龄（24h TTL，13.7）；清理失败仅 debug 记录。"""
    global _spill_cleanup_at
    now = time.time()
    if now - _spill_cleanup_at < 3600:
        return
    _spill_cleanup_at = now
    root = os.path.join(_dir(), "spill")
    if not os.path.isdir(root):
        return
    cutoff = now - SPILL_TTL_HOURS * 3600
    removed = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
                    removed += 1
            except OSError as exc:
                logger.debug("paperhermes: spill cleanup skipped %s (%s)", fp, exc)
    # 空的会话子目录顺手清掉
    try:
        for d in os.listdir(root):
            sub_d = os.path.join(root, d)
            if os.path.isdir(sub_d) and not os.listdir(sub_d):
                os.rmdir(sub_d)
    except OSError:
        pass
    if removed:
        logger.info("paperhermes: spill TTL cleanup removed %d file(s) older than %dh",
                    removed, SPILL_TTL_HOURS)
    # hints 超龄清除（TTL 本来就有，之前只在读取时惰性过滤，盘上文件无限长）
    try:
        hints = _load_hints()
        fresh = {n: ts for n, ts in hints.items() if now - float(ts) < SIGNAL_TTL_DAYS * 86400}
        if len(fresh) != len(hints):
            _save_hints(fresh)
    except Exception:
        pass


def register(ctx) -> None:
    try:
        _backfill_defaults(ctx)
        _load_freezes()
        # v0.5.5 #6：依赖键缺前置开关时 WARN 一次（4.0）。读的是补齐后的 settings，
        # 只在真开了依赖键而前置没开时提醒，一次性、不阻塞注册。
        try:
            st_all = _read_settings(ctx)
            if not st_all["fold_skills_index"]:
                for dep in ("fold_skills_dynamic", "fold_skills_trigger_only"):
                    if st_all[dep]:
                        logger.warning("paperhermes: %s=true 依赖 fold_skills_index=true，"
                                       "当前不生效——开 fold_skills_index 或关掉该键。", dep)
        except Exception:
            pass
        global _observer
        if bool(_get(ctx, "observe")):
            _observer = engine.Observer(os.path.join(_dir(), "observe.log"))

        # M8：注册自己的辅助模型槽（继承 compression = 主人说的 "compact 辅助模型"）
        try:
            if hasattr(ctx, "register_auxiliary_task"):
                ctx.register_auxiliary_task(
                    AUX_TASK_KEY, display_name="PaperHermes 工具摘要",
                    description="把工具 description 与参数 schema 压成忠实摘要（规则句必须保留）",
                    inherit_from="compression")
        except Exception as exc:
            logger.debug("paperhermes: auxiliary task registration skipped (%s)", exc)

        def _cli_handler(args=None):
            a = args
            # v0.5.5 事故修复（2026-11-11）：无子参数时宿主仍会调 handler_fn，原先
            # 直落 _run_compact_tools —— 一次手滑的 `hermes paperhermes` 白跑 21 批
            # 辅助模型调用并整库重写了 tool_compact.json。显式子命令之外一律只给用法。
            if str(getattr(a, "ph_cmd", "") or "") != "compact-tools":
                print("paperhermes: 需要子命令。用法:\n"
                      "  hermes paperhermes compact-tools [--dry-run] [--only a,b]  生成/刷新摘要库\n"
                      "  hermes paperhermes stats                                    读 observe.log 账本")
                return 0
            dry = bool(getattr(a, "dry_run", False))
            only = str(getattr(a, "only", "") or "")
            return _run_compact_tools(ctx, dry_run=dry, only=only)

        def _cli_setup(p):
            sub = p.add_subparsers(dest="ph_cmd")
            c = sub.add_parser("compact-tools",
                               help="用 compact 辅助模型生成/刷新忠实摘要库")
            c.add_argument("--dry-run", action="store_true", help="只算不写盘")
            c.add_argument("--only", default="",
                           help="逗号分隔的工具名，只处理这些（默认全部）")
            c.set_defaults(func=_cli_handler)
            # v0.5.5 #3：13.9.3 的第二个 CLI 面——stats 直接复用聊天命令的 _stats()。
            # 宿主只把返回值当退出码（main.py:3443 rc=args.func(args)），字符串会被
            # 静默丢弃——compact-tools 是自己 print 的，这里也必须自己 print。
            s = sub.add_parser("stats", help="读 observe.log 账本（请求/回收统计）")
            s.set_defaults(func=lambda args=None: print(_stats()) and 0)

        try:
            ctx.register_cli_command(
                "paperhermes", help="PaperHermes 工具摘要库管理",
                setup_fn=_cli_setup, handler_fn=_cli_handler,
                description="hermes paperhermes compact-tools")
        except Exception as exc:
            logger.debug("paperhermes: CLI command registration skipped (%s)", exc)

        def _llm_middleware(kind=None, **kw):
            try:
                request = kw.get("request")
                if not isinstance(request, dict):
                    return None
                api_mode = str(kw.get("api_mode") or "")
                session_id = str(kw.get("session_id") or "")
                model = str(kw.get("model") or "")
                provider = str(kw.get("provider") or "")
                arid = str(kw.get("api_request_id") or "")
                composition = None
                if _observer is not None and bool(_get(ctx, "observe_composition")):
                    composition = engine.payload_composition(request, api_mode)
                if bool(_get(ctx, "audit_defer")):
                    tools = list(request.get("tools") or [])
                    _last_payload_tools[:] = tools
                    for td in tools:
                        n = str(engine._fn_of(td).get("name") or "")
                        if n:
                            _seen_tool_names.add(n)
                new_req, counts, action = _handle_request(request, ctx, api_mode,
                                                          session_id, model)
                # v0.5.5 #7：一次成功请求清零 error_streak——「连续异常」的连续
                # 语义回来了；5 次散布在各天的偶发异常不再误报 suggest_disable。
                if action not in ("bypass-mode", "snapshot-corrupt"):
                    try:
                        snap = _snapshots.get(session_id)
                        if snap is not None and snap.get("error_streak"):
                            snap["error_streak"] = 0
                    except Exception:
                        pass
                if _observer is not None:
                    _observer.record_request(
                        request, provider=provider, model=model, api_mode=api_mode,
                        session_id=session_id, action=action,
                        tools_folded=counts.get("tools", 0),
                        skills_folded=counts.get("skills", 0),
                        params_folded=counts.get("params", 0),
                        notice_chars=counts.get("notice_chars", 0),
                        api_request_id=arid, composition=composition,
                        post=(new_req if new_req is not None else None))
                if new_req is None:
                    return None
                return {"request": new_req}
            except Exception as exc:
                _record_error(str(kw.get("session_id") or ""), exc)
                logger.warning("paperhermes: middleware error (%s) — request untouched",
                               exc, exc_info=True)
                return None

        ctx.register_middleware("llm_request", _llm_middleware)

        def _on_post_api(_hook=None, **kw):
            """提交点（13.2 步骤3）：首次 post_api_request = 首次 LLM 调用结束。
            只翻 committed 标志，不重算任何折叠内容。M0.5 顺带记 usage 回执。"""
            try:
                sid = str(kw.get("session_id") or "")
                _load_freezes()
                snap = _snapshots.get(sid)
                if snap is not None and not snap.get("committed"):
                    snap["committed"] = True
                    _persist_freezes()
                if _observer is not None and bool(_get(ctx, "observe_composition")):
                    _observer.record_usage(str(kw.get("api_request_id") or ""), sid,
                                           str(kw.get("model") or ""), kw.get("usage"))
            except Exception:
                pass

        def _on_transform_result(_hook=None, **kw):
            """M5/M6：按快照里冻结的规则处理新工具结果（13.2 第 8 点）。
            terminal 内建排除（不二次处理终端输出）；melted/无快照→原样。"""
            try:
                result = kw.get("result")
                if not isinstance(result, str) or not result:
                    return None
                sid = str(kw.get("session_id") or "")
                name = str(kw.get("tool_name") or "")
                _load_freezes()
                snap = _snapshots.get(sid)
                if snap is None or snap.get("melted"):
                    return None
                # 注意：这里**不**看 decision.act。act 只反映折叠面的净益门，
                # M5/M6 有自己的开关（minify/strip/spill_*）；把结果处理挂到 act 上
                # 会让"折叠不划算"连带关掉用户明确打开的结果清理，那是更糟的耦合。
                st = snap.get("settings") or {}
                if name == "terminal":
                    return None
                txt = result
                if bool(st.get("strip_terminal_progress_noise")):
                    txt = engine.clean_terminal_noise(txt) or txt
                if bool(st.get("minify_json_tool_results")):
                    txt = engine.minify_json_result(txt) or txt
                if bool(st.get("spill_large_tool_results")):
                    excl = {str(x) for x in (st.get("spill_exclude_tools") or [])}
                    if name not in excl \
                            and engine.should_spill(txt, st.get("spill_min_chars")):
                        txt = _spill(snap, sid, name, txt)
                return txt if txt != result else None
            except Exception as exc:
                logger.warning("paperhermes: transform_tool_result error (%s) — result "
                               "untouched", exc)
                return None

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
            """信号会话末合并进 hints 供新会话使用；冻结快照不在此删除（13.2 第 12 点）。
            v0.5.5：顺带做两件惰性清理（节流 1 小时一次）：超龄 spill 文件删除（13.7）、
            超龄 hints 清除。observe.log 的轮转在 Observer._write 内联做，不在这。"""
            try:
                _lazy_cleanup()
            except Exception:
                pass
            try:
                sid = str(kw.get("session_id") or "")
                sig = _session_signals.pop(sid, set())
                if not sig:
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
        ctx.register_hook("post_api_request", _on_post_api)
        ctx.register_hook("transform_tool_result", _on_transform_result)

        def _cmd(raw_args="", session_id="", **kw):
            sid = str(session_id or "")
            arg = (raw_args or "").strip().lower()
            if arg != "rearm":
                _rearm_pending.pop(sid, None)  # 只有连续两次 rearm 才有效
            try:
                if arg == "rearm":
                    return _rearm(ctx, sid)
                if arg == "meltdown":
                    return _meltdown(ctx, sid)
                if arg.startswith("stats"):
                    return _stats()
                return _panel(ctx, sid)
            except Exception as exc:
                return "paperhermes command error: %s" % exc

        try:
            ctx.register_command("paperhermes", handler=_cmd,
                                 description="paperhermes 面板/账本/急停/恢复（stats|meltdown|rearm）",
                                 args_hint="[stats|meltdown|rearm]")
        except Exception as exc:
            logger.debug("paperhermes: register_command skipped (%s)", exc)
        _ctx_log(ctx, "INFO", "paperhermes v%s registered (observe=%s, frozen_sessions=%d)"
                % (VERSION, _observer is not None, len(_snapshots)))
    except Exception as exc:
        logger.warning("paperhermes: registration degraded: %s", exc)
