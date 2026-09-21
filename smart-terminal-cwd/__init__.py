"""smart-terminal-cwd — per-surface terminal default working directory.

为什么需要这个插件（机制取证见 README.md，行号基于 v0.21.3）：
``terminal.cwd`` 是 HERMES_HOME 级的全局配置，无法按表面区分。而各表面行为不同：
  - CLI       : local 后端下被 ``cli.py:336`` 强制改写为 ``os.getcwd()``（启动目录），配置值被无视；
  - TUI/桌面  : ``tui_gateway/server.py:596 _default_session_cwd`` 读**配置文件里的** ``terminal.cwd``，
                且优先级在 ``TERMINAL_CWD`` 环境变量**之前** —— 所以 CLI 无视它、TUI 却服从它；
  - Gateway   : ``gateway/run.py:2079`` 显式值直通，占位符解析到家目录/后端默认。

本插件在会话起点（``on_session_start``）为当前表面解析应用的默认目录，并通过
``tools.terminal_tool`` 的两个合法注入口生效：

  1. ``record_session_cwd(key, cwd)``   —— execute_code 子内核 ladder 第一站
     （``code_execution_env.py:262 get_session_cwd``），且 terminal 侧无 overrides 时被采用；
  2. ``overrides = resolve_task_overrides(key)`` 的 ``cwd`` 原位修补 —— terminal 工具
     ``terminal_tool.py:950`` 中 overrides 优先级**高于**会话记录，必须同步。

红线：只动 ``cwd_source == "process"`` 的 overrides（全局兜底值），绝不动
``"session"``（用户在桌面显式挑选的工作区）。后者同时是 docker per-session 隔离下
挂载源的信任标记（``terminal_tool.py:506-507``：``process`` 来源被拒挂载）——
把它伪造成 ``session`` 等于伪造用户意图；本插件只在 local 后端做原位值替换，
且仅在配置的目录真实存在时生效。

配置存放：Hermes 给插件划定的专用字段
``config.yaml -> plugins.entries.smart-terminal-cwd.settings.default_dirs``：

    default_dirs:              # 顶层 = 跨系统通用值
      cli: "."                 # "." / auto / cwd = 进程启动目录
      tui: "."
      gateway: "D:/01-AI/Hermes-Agent/Nest"
      desktop: "D:/01-AI/Hermes-Agent/Nest"
    os:                        # 按当前系统覆盖（win32 / darwin / linux）
      win32:
        gateway: "D:/01-AI/Hermes-Agent/Nest"
      darwin:
        gateway: "~/Hermes/Nest"

值语义：""/缺失 = 不干预（保持 Hermes 原生行为）；"."=启动目录；
"~" 展开；相对路径按启动目录解析；必须是已存在的目录，否则忽略并告警。
表面判据（优先级从高到低）：显式环境变量 SMART_TERMINAL_CWD_SURFACE（测试用）
→ _HERMES_GATEWAY=1 → gateway；HERMES_DESKTOP=1 且无 HERMES_DESKTOP_TERMINAL
→ desktop；平台标识 cli → cli；其余（含 hermes --tui 与桌面内嵌终端面，均报 tui）→ tui。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "smart-terminal-cwd"
_LAUNCH_CWD = os.getcwd()  # 进程启动目录快照（"." 语义的锚点；进程生命周期内稳定）
_PLACEHOLDERS = {".", "auto", "cwd", "launch"}

# key -> 本插件为本会话钉住的目录。pre_tool_call 据此在 agent 重建后（桌面重开会话会
# 用 _register_session_cwd 重新注册 overrides，冲掉我们的值）廉价地重新断言。
_pinned: Dict[str, str] = {}
_pin_lock = threading.Lock()

# 系统提示词与实际默认目录的双通道漂移修正（见 _make_pre_llm_call）：
# _prompt_cwd[key] = on_session_start 时 resolve_agent_cwd() 的读数（= 提示词
# 将写入的 "Current working directory"，同线程同 context 采样，保证同源）；
# _cwd_note_sent[key] = 已为 (key, 钉值) 发过便签，防重复注入。
_prompt_cwd: Dict[str, str] = {}
_cwd_note_sent: Dict[str, str] = {}

# resolve_task_overrides 未命中时返回 None 还是空 dict 属内部细节，两种都安全处理；
# 命中时返回的是注册表内部 dict —— 原位 [key]=value 修改即生效（已核对
# register_task_env_overrides 直接持有同一对象）。_plan_execution 用
# ``overrides.get("cwd") or get_session_cwd(task_id) or config["cwd"]``，
# 所以我们写的 overrides 值同时压过会话记录与 config，成为该会话的“新默认”；
# 会话内的 cd 由 _resolve_command_cwd 在**更后一步**按 session_key 处理，不受影响。


def _backend_is_local(ctx: Any) -> bool:
    """仅 local 后端干预：docker/ssh 等后端里宿主路径不存在（terminal_tool 会消毒或
    报 exit 125），钉了也是错的。ctx.get_config 只见本插件 settings 子树，读不到全局
    terminal.backend；运行时的权威载体是 TERMINAL_ENV 环境变量（config→env 桥接产物）。"""
    be = (os.environ.get("TERMINAL_ENV") or "").strip().lower()
    return not be or be == "local"


def _surface(ctx_platform: str = "", module_hint: str = "") -> str:
    """把 Hermes 会话级 platform 归一为配置表面键 cli | tui | gateway | desktop。

    优先级铁律：platform 参数（SESSION 的 source，conversation_loop.py:768 /
    server.py:2376 逐会话传入）> 模块线索 > 环境变量兜底。
    桌面聊天面与 TUI/Gateway 共用同一个 tui_gateway 进程（同时带 _HERMES_GATEWAY=1
    和 HERMES_DESKTOP=1），进程级变量无法区分表面——只能信 platform。
    """
    # 0. 强制覆写（调试用）
    forced = os.environ.get("SMART_TERMINAL_CWD_SURFACE", "").strip().lower()
    if forced in {"cli", "tui", "gateway", "desktop"}:
        return forced

    # 1. 会话级 platform（权威）
    p = (ctx_platform or "").strip().lower()
    if p in {"cli", "tui", "gateway", "desktop"}:
        return p
    if p:  # telegram / discord / whatsapp / api ... → 远程面，归 gateway 桶
        return "gateway"

    # 2. 模块线索（钩子没给 platform 时）
    if (module_hint or "").startswith("hermes_cli"):
        return "cli"

    # 3. 环境变量兜底（仅对"纯 gateway / 纯 desktop 独立进程"可靠；
    #    共享 tui_gateway 进程里两变量同真，桌面优先，宁误判桌面不误判 gateway）
    desktop_env = (os.environ.get("HERMES_DESKTOP", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    tui_terminal_env = (os.environ.get("HERMES_DESKTOP_TERMINAL", "") or "").strip().lower() in {"1", "true", "yes", "on"}
    if desktop_env and not tui_terminal_env:
        return "desktop"
    if os.environ.get("_HERMES_GATEWAY") == "1":
        return "gateway"
    if tui_terminal_env:
        return "tui"

    logger.warning("%s: 表面判定无依据（platform=%r module=%r），不干预", PLUGIN_ID, ctx_platform, module_hint)
    return ""


def _expand(raw: str) -> Optional[Path]:
    """值语义 → 绝对路径；无法成为有效目录时返回 None（含“不存在”：宁可不干预也不注入死路径）。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw in _PLACEHOLDERS:
        return Path(_LAUNCH_CWD).resolve()
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = Path(_LAUNCH_CWD) / p
    try:
        p = p.resolve()
    except OSError:
        return None
    return p if p.is_dir() else None


def _surface_default_dir(ctx: Any, surface: str) -> Optional[Path]:
    """解析某表面应钉到的目录。优先级：os.<sys.platform>.<surface> > default_dirs.<surface>。

    读取失败（配置未写/托管安装等）一律 None = 不干预，插件静默让位给 Hermes 原生行为。
    """
    try:
        osv = ctx.get_config("os", default=None) or {}
        per = ctx.get_config("default_dirs", default=None) or {}
    except Exception as exc:  # noqa: BLE001 - fail-open 是本插件的生存底线
        logger.debug("%s: settings unreadable (%s); no-op", PLUGIN_ID, exc)
        return None
    raw = ""
    if isinstance(osv, dict):
        sect = osv.get(sys.platform)
        if isinstance(sect, dict) and str(sect.get(surface) or "").strip():
            raw = str(sect[surface])
        else:
            raw = ""
    if not raw and isinstance(per, dict):
        raw = str(per.get(surface) or "")
    p = _expand(raw)
    if p is None and raw.strip():
        logger.info("%s: surface=%s configured %r unusable (missing dir?); leaving Hermes default",
                    PLUGIN_ID, surface, raw)
    return p


def _session_record_for(key: str) -> Optional[dict]:
    """按会话键查同进程 tui_gateway 的活会话记录（桌面/TUI 才有；CLI 进程返回 None）。
    overrides 注册表按 session["session_key"] 注册，raw 与 collapse（session:<id>）
    两形态都试一遍。"""
    try:
        from tui_gateway import server as _srv  # noqa: PLC0415 - 进程内才有
        sessions = getattr(_srv, "_sessions", None)
        if not isinstance(sessions, dict):
            return None
        bare = key.split(":", 1)[1] if key.startswith("session:") else key
        rec = sessions.get(bare) or sessions.get(key)
        if rec is not None:
            return rec
        for s in sessions.values():
            sk = str(s.get("session_key") or "")
            if sk in (key, bare):
                return s
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_explicit_workspace(key: str) -> bool:
    """该会话的工作区是否"用户/agent 显式选定"——唯一可信信号是会话记录的
    ``explicit_cwd`` 标志（UI 挑选/恢复时置真，见 methods_session.py:526、
    agent_callbacks.py:144、session_workdir.py:174/472）。

    不能用 overrides 的 cwd_source=="session" 判断：local 后端下
    _terminal_task_cwd_with_source（session_workdir.py:67-68）对**一切**带跟踪
    cwd 的会话（含纯启动目录）都返回 "session"，它只影响 docker 挂载信任，
    与用户意图无关。查不到会话记录（CLI/外部）→ 无显式证据 → False。
    """
    rec = _session_record_for(key)
    return bool(rec and rec.get("explicit_cwd"))


def _apply(keys, cwd: Path) -> None:
    """把这些会话键的默认目录钉到 *cwd*。双载体（理由见模块 docstring）：
    - ``record_session_cwd``：terminal 的 ``get_session_cwd(task_id)`` 档 +
      execute_code 子内核 ladder 第一站（code_execution_env.py:262）；
    - overrides 原位修补：terminal ``_plan_execution:950`` 中 overrides 压过会话记录，
      且 ``_resolve_command_cwd`` 的 ``default_cwd=plan.cwd`` 就是它——所以桌面
      _register_session_cwd 冲值后必须连 overrides 一起补，只写会话记录会被无视。

    注意 resolve_task_overrides 未命中返回**新建空 dict**（写它无效），命中返回内部对象
    （原位改即生效）；key 需 raw 与 collapse 两形态各查一次（:447 docstring）。
    显式工作区保护：会话记录 explicit_cwd 为真 → 整键放弃（见 _is_explicit_workspace）。
    """
    if isinstance(keys, str):
        keys = {keys}
    keys = {str(k) for k in keys if str(k or "").strip()}
    if not keys or not cwd.is_dir():
        return
    target = str(cwd)
    from tools.terminal_tool import record_session_cwd, resolve_task_overrides

    try:
        from tools.terminal_tool import _resolve_container_task_id
    except Exception:  # noqa: BLE001
        def _resolve_container_task_id(k):  # pragma: no cover - 老版本兜底
            return k

    for key in keys:
        protected = False
        for lk in {key, str(_resolve_container_task_id(key))}:
            if _is_explicit_workspace(lk):
                protected = True  # 显式工作区：神圣，整键让位
                break
            try:
                ov = resolve_task_overrides(lk)
            except Exception:  # noqa: BLE001
                continue
            if not ov:  # 空 dict = 无注册记录：写它无效，仅靠 record_session_cwd
                continue
            if "cwd" in ov:
                # 原位值替换：保留原 cwd_source（"process"/"session" 都可能有），
                # docker per-session 挂载的拒绝语义不变。
                ov["cwd"] = target
        if protected:
            logger.debug("%s: key %s owns an explicit session workspace; leaving it alone", PLUGIN_ID, key)
            continue
        record_session_cwd(key, target)
        with _pin_lock:
            _pinned[key] = target


def _cli_ref_session_key() -> str:
    """CLI 主线程在轮与轮之间（斜杠命令时机）审批 contextvar 已解绑，gateway 侧也不写
    os.environ；但插件管理器持有 CLI 实例（cli_tui_mixin 设置 _cli_ref），它的 session_id
    与 chat 轮内 terminal_tool 用的 key 同源。gateway/desktop 进程 _cli_ref 为 None → 空串。"""
    try:
        ctx = _ctx_ref[0] if _ctx_ref else None
        cli = getattr(getattr(ctx, "_manager", None), "_cli_ref", None) if ctx else None
        return str(getattr(cli, "session_id", "") or "") if cli else ""
    except Exception:  # noqa: BLE001
        return ""


def _session_key(task_id: str = "") -> str:
    """与 terminal_tool 同款 key 阶梯（terminal_tool.py:1229），外加 CLI 斜杠时机补洞。"""
    try:
        from tools.approval import get_current_session_key
        k = get_current_session_key(default="") or (task_id or "")
    except Exception:  # noqa: BLE001
        k = task_id or ""
    k = str(k or "") or _cli_ref_session_key()
    return k or "default"


# ---------------------------------------------------------------- hooks


def _make_on_session_start(ctx: Any) -> Callable[..., None]:
    def _on_session_start(session_id: str = "", platform: str = "", task_id: str = "", **_: Any) -> None:
        try:
            if not _backend_is_local(ctx):
                return  # docker/ssh 等远端后端：宿主路径注进去只会触发消毒/报错，不干预
            surface = _surface(platform)
            target = _surface_default_dir(ctx, surface)
            if target is None:
                return  # 该表面未配置/配置无效：不干预
            # terminal 有两条消费键：_plan_execution 用 task_id、_resolve_command_cwd
            # 用审批 contextvar（terminal_tool.py:1229）。轮内两者通常同源，但不赌——都钉。
            keys = {_session_key(session_id), str(task_id or session_id or "")}
            _apply(keys, target)
            logger.info("%s: pinned %s (%s) cwd -> %s", PLUGIN_ID, sorted(keys), surface, target)
        except Exception as exc:  # noqa: BLE001 - 绝不让钩子砸穿会话
            logger.warning("%s: on_session_start failed: %s", PLUGIN_ID, exc)
    return _on_session_start


def _make_pre_tool_call(ctx: Any) -> Callable[..., Any]:
    """廉价的重断言守卫。

    桌面/TUI 在 agent 重建、提示词重编译等路径会用 _register_session_cwd 重新
    register_task_env_overrides，把我们 on_session_start 钉的值冲回“进程级默认”。
    这里在每次 terminal/execute_code 前查一次 dict（O(1)），发现值漂移就补写。
    只补 _pinned 里存在的 key —— 未被本插件管理的会话零开销直通。
    """
    def _guard(tool_name: str = "", task_id: str = "", **_: Any) -> None:
        if tool_name not in {"terminal", "execute_code"}:
            return None
        try:
            with _pin_lock:
                if not _pinned:
                    return None
                key = _session_key(task_id)
                want = _pinned.get(key)
            if not want:
                return None
            from tools.terminal_tool import get_session_cwd, resolve_task_overrides
            if get_session_cwd(key) == want:
                try:
                    ov = resolve_task_overrides(key)
                except Exception:  # noqa: BLE001
                    ov = None
                # 只看值：桌面 _register_session_cwd 冲值后 overrides 带着
                # 启动目录回来（无论 source 是 "process" 还是 "session"），
                # 只要 cwd 不等于 want 就构成压制，必须补写。
                if isinstance(ov, dict) and ov.get("cwd") == want:
                    return None  # 记录在位且无压制 → 无需动作
            _apply(key, Path(want))
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: pre_tool_call re-assert failed: %s", PLUGIN_ID, exc)
        return None
    return _guard


def _make_pre_llm_call(ctx: Any) -> Callable[..., Any]:
    """修正系统提示词与实际默认目录的双通道漂移（桌面端实测）。

    背景：``agent/runtime_cwd.resolve_agent_cwd()``（系统提示词 "Current working
    directory" 的数据源）读 会话contextvar/进程env 的 TERMINAL_CWD；终端工具执行
    时读的是 session_workdir 注册表（本插件 on_session_start 钉的值）。桌面网关
    给这两条通道喂不同值 → 提示词说家目录、命令实际跑在钉值目录。修在核心里
    越权且炸 prefix 缓存（提示词按字节稳定设计，见 agent/surface_switch.py），
    所以按同款哲学处理：漂移时用 pre_llm_call 便签（注入 user message，一次性）
    告知模型以钉值为准。本钩子跑在轮次调用栈内，resolve_agent_cwd() 与提示词
    构建同线程同 context，读数即提示词将写的值 —— 无需猜测。
    """
    def _note(*, task_id: str = "", conversation_history: Any = None, **_: Any) -> Any:
        try:
            with _pin_lock:
                want = _pinned.get(_session_key(task_id))
            if not want:
                return None  # 本插件未接管该会话 → 无漂移可言
            from agent.runtime_cwd import resolve_agent_cwd
            prompt_cwd = resolve_agent_cwd()
            if not prompt_cwd or _same_cwd(prompt_cwd, want):
                return None  # 通道已一致（核心版修复生效后此路静默直通）
            key = _session_key(task_id)
            cur_len = len(conversation_history) if isinstance(conversation_history, list) else None
            with _pin_lock:
                sig = _cwd_note_sent.get(key)
            # 签名 (钉值, 轮次计数, 上轮历史长度)：钉值变 → 必重发；历史变短
            # （压缩/轮转把便签卷掉）→ 必重发；否则每 10 轮补发一次兜底。
            if isinstance(sig, tuple) and sig[0] == want:
                n = sig[1]
                shrunk = (cur_len is not None and sig[2] is not None
                          and cur_len < sig[2])
                if not shrunk and n % 10 != 0:
                    with _pin_lock:
                        _cwd_note_sent[key] = (want, n + 1, cur_len)
                    return None
            with _pin_lock:
                _cwd_note_sent[key] = (want, 1, cur_len)
            return {
                "context": (
                    f"[System: 系统提示词中的 “Current working directory” ({prompt_cwd}) 已过时。"
                    f"本会话 terminal / execute_code 工具的实际默认工作目录是 {want}"
                    "（由 smart-terminal-cwd 插件按表面配置钉定）。除非用户明确指定其他目录，"
                    "命令与文件操作均以该目录为准。]"
                )
            }
        except Exception as exc:  # noqa: BLE001 - 钩子绝不允许砸穿本轮请求
            logger.debug("%s: pre_llm_call cwd-drift note failed: %s", PLUGIN_ID, exc)
            return None
    return _note


def _same_cwd(a: str, b: str) -> bool:
    """尽力而为的路径等价比较（大小写/斜杠/尾部分隔符差异在 Windows 上是常态）。"""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a.rstrip("\\/").lower() == b.rstrip("\\/").lower()


# ------------------------------------------------------- 共享动作逻辑


def _effective_cwd_report(key: str, surface: str) -> str:
    from tools.terminal_tool import get_session_cwd, resolve_task_overrides
    parts = [f"会话 {key}（表面: {surface}）"]
    rec = get_session_cwd(key)
    ov = None
    try:
        ov = resolve_task_overrides(key)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(ov, dict) and ov.get("cwd"):
        parts.append(f"生效默认目录: {ov['cwd']}（overrides, source={ov.get('cwd_source') or '?'}）")
    elif rec:
        parts.append(f"生效默认目录: {rec}（会话记录）")
    else:
        cfg = ""
        try:
            cfg = os.environ.get("TERMINAL_CWD", "").strip()
        except Exception:  # noqa: BLE001
            pass
        parts.append(f"生效默认目录: {cfg or _LAUNCH_CWD}（{'TERMINAL_CWD' if cfg else '启动目录'}）")
    if rec and (not isinstance(ov, dict) or ov.get("cwd") != rec):
        parts.append(f"会话内 cd 状态: {rec}")
    pinned = _pinned.get(key)
    parts.append(f"smart-terminal-cwd 钉值: {pinned or '无'}")
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        pcwd = str(resolve_agent_cwd())
        parts.append(f"系统提示词通道 (resolve_agent_cwd): {pcwd}")
        if pinned and not _same_cwd(pcwd, pinned):
            parts.append(
                "⚠ 双通道漂移：提示词读到的 cwd ≠ 终端生效目录。"
                "pre_llm_call 便签会自动向模型更正；提示词本身不改写"
                "（字节稳定 = prefix 缓存契约，见 agent/surface_switch.py）。")
    except Exception:  # noqa: BLE001
        pass
    try:
        surf_cfg = _surface_default_dir(_ctx_ref[0], surface) if _ctx_ref and _ctx_ref[0] else None
    except Exception:  # noqa: BLE001
        surf_cfg = None
    parts.append(f"表面配置解析: {surf_cfg or '未配置/不适用'}")
    return "\n".join(f"  {p}" for p in parts)


def _set_session_cwd(raw_path: str, task_id: str = "") -> str:
    if not raw_path.strip():
        return f"[{PLUGIN_ID}] 用法: /terminal-cwd <路径>（'.'=启动目录 {_LAUNCH_CWD}，'default'=清除钉值）"
    p = _expand(raw_path)
    if p is None:
        return f"[{PLUGIN_ID}] 拒绝：目录不存在或无法解析 → {raw_path!r}（本工具不创建目录，请用真实存在的路径）"
    key = _session_key(task_id)
    _apply(key, p)
    return f"[{PLUGIN_ID}] 会话 {key} 的默认终端目录已切换 → {p}"


def _clear_session_cwd(task_id: str = "") -> str:
    key = _session_key(task_id)
    with _pin_lock:
        had = _pinned.pop(key, None)
    if had is None:
        return f"[{PLUGIN_ID}] 会话 {key} 没有本插件的钉值，无需清除（已生效的会话记录/overrides 保持原样）。"
    return (f"[{PLUGIN_ID}] 已停止接管会话 {key}（原钉值 {had}）。"
            "注意：本会话内已写入的会话记录不会回退；新开会话将按表面配置重新接管。")


# ---------------------------------------------------------------- 命令 / 工具

_ctx_ref: list = []  # [ctx] 简单引用槽（register 时填充）

_HELP = (
    f"[{PLUGIN_ID}] /terminal-cwd — 查看或更换当前会话的终端默认目录\n"
    "  /terminal-cwd                 显示当前生效状态\n"
    "  /terminal-cwd <路径>          把本会话默认目录钉到 <路径>（'.'=启动目录）\n"
    "  /terminal-cwd default         清除本插件对该会话的接管\n"
    "  提示：这只是本会话的默认值；持久化默认值在 "
    "config.yaml -> plugins.entries.smart-terminal-cwd.settings（见插件 README）。"
)


def _handle_slash(raw_args: str) -> str:
    arg = (raw_args or "").strip()
    if not arg or arg in {"pwd", "status", "info"}:
        return _effective_cwd_report(_session_key(), _surface())
    if arg in {"-h", "--help", "help"}:
        return _HELP
    if arg == "default":
        return _clear_session_cwd()
    return _set_session_cwd(arg)


TOOL_SCHEMA = {
    "name": "terminal_cwd",
    "description": (
        "查看或更换当前会话的终端默认工作目录（等价于 /terminal-cwd）。"
        "不带参数=查询状态；path=切换（'.' 表示进程启动目录，'~' 会展开）；action='reset' 清除接管。"
        "只影响后续命令的默认目录，不影响会话内已 cd 的状态，也不改 config.yaml。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标目录（绝对、~/ 开头、或 '.'）。省略=查询。"},
            "action": {"type": "string", "enum": ["reset"], "description": "reset=清除本插件对该会话的接管。"},
        },
        "required": [],
    },
}


def _tool_handler(args: Dict[str, Any], task_id: str = "", **_: Any) -> str:
    import json
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    path = str(args.get("path") or "").strip()
    try:
        if action == "reset":
            msg = _clear_session_cwd(task_id)
        elif not path:
            msg = _effective_cwd_report(_session_key(task_id), _surface())
        else:
            msg = _set_session_cwd(path, task_id)
        return json.dumps({"ok": True, "detail": msg}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - 工具层错误以结构化返回，不抛出
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


# ---------------------------------------------------------------- entry


def register(ctx) -> None:
    _ctx_ref.append(ctx)
    ctx.register_hook("on_session_start", _make_on_session_start(ctx))
    ctx.register_hook("pre_tool_call", _make_pre_tool_call(ctx))
    ctx.register_hook("pre_llm_call", _make_pre_llm_call(ctx))
    ctx.register_command(
        "terminal-cwd", handler=_handle_slash, args_hint="[<路径>|default]",
        description="查看/更换当前会话的终端默认目录（smart-terminal-cwd）。",
    )
    ctx.register_tool(
        name="terminal_cwd", toolset="terminal", schema=TOOL_SCHEMA, handler=_tool_handler,
        description="查看或更换当前会话的终端默认工作目录。", emoji="📂",
    )
