# -*- coding: utf-8 -*-
"""approval_host.py — 审批弹窗宿主桥接（PROMPT §12 / CONTRACT §11，R28-1/R28-2/R28-5）。

官方正门 = approval transport：register() 里 ctx.register_approval_transport("gateway-ui",
present) 注册（惰性，须用户配 security.approval.transport: gateway-ui 才激活）。present
跑在宿主 approval-transport 专属 daemon 线程（invoke_approval_transport 起），**绝不**
在 asyncio 主循环上；阻塞合法。

职责：
- 进程内 pending 表登记/注销（R28-5 await 状态权威源，表寿命=真实等待窗口）；
- 写 approval_req.json（pending 全量快照，宿主单写者）；
- 经本地 IPC（approval_ipc.py，命名管道/AF_UNIX）收 worker 审批决定——五重校验
  （client PID=租约 pid、租约新鲜、stdin 单传握手 token、spawn_token、宿主 digest）
  过验入内存表，present 轮询一次性消费；审批决定**不落共享文件**（旧
  approval_resp.json 同用户进程可伪造放行，结构性缺陷，已根除）；
- 自终止线 timeout-5s / worker 死（tray_lease mtime 陈旧）/ 被取消 / 宿主 stop
  → 返回 no_response decision（choice 不在 allowed_choices → 宿主判 invalid
  fail-closed deny，归因 transport_invalid，**不冒充用户拒绝**）；
- finally 追加 approval_withdrawn.jsonl（worker 收窗信号）；
- R29 平台分流（platform_takeover 默认 false）：消息平台会话（session_key 平台段
  ∉ 本地集）不登记 pending、立即 respond("no_response")——宿主判 invalid 后按
  transport_fallback 顺序回落内置面（配 builtin 时飞书/QQ/微信聊天卡照常弹）；
  不配 builtin 则此类审批被直接拒绝（比截胡更糟，README/deploy 双提醒）。

decision 只准 request.respond(choice) 造——request_id/digest 由宿主绑定，本插件
不回显不改算。command/description 是宿主已强制脱敏副本（redact_approval_display
force=True），原样透传零再加工。字段白名单：绝无消息正文/密钥。
"""
from __future__ import annotations

import json
import hmac
import threading
import time
from pathlib import Path

from . import approval_ipc
from .bus import write_json
from .statemachine import NOTICE_PLATFORMS  # R29 本地判定双保险（desktop/tui/cli）

# 自终止线 = 宿主 timeout - 5s（抢在宿主 deadline 前返回 no_response，归因 invalid
# 而非宿主自判 timeout）；worker 存活判据 = tray_lease.json mtime（worker 每 5s utime，
# TTL 8s；writer_lease 是宿主自己的租约，不能用来判 worker 生死——契约笔误已修正）。
SELF_TERMINATE_MARGIN_S = 5.0
WORKER_LEASE_TTL_S = 8.0
_POLL_S = 0.25
_HINT_CAP = 128

_LOCK = threading.Lock()
_PENDING: dict = {}          # request_id -> entry{request, session_key, cancelled, reason, ts}
_SESSION_HINTS: dict = {}    # request_id -> session_key（pre_approval_request hook O(1) 记）
_REQ_SEQ = 0
_BUS_DIR: Path | None = None
_STOP = None
_LOGGER = None
_IPC_SERVER = None
_IPC_ENDPOINT = ""
_IPC_TOKEN = ""
_RESPONSES: dict = {}       # request_id -> choice，宿主消费一次即删除
# R29 平台分流：present 读 approval 块（platform_takeover 等）的宿主侧配置探针
# （register 注入 cfg_fn；读不到按默认——不接管——处理，安全侧）。
_PRESENT_CFG_FN = None

# 本地会话平台集（session_key 第三段）：`local` = gateway 托管的桌面/TUI/CLI/ACP
# 会话（gateway/session.py build_session_key 布局 <ns>:<platform>:<chat_type>:…）；
# `cli` = 纯 CLI 进程键（无 gateway 时的会话命名）；其余平台值（feishu/qqbot/
# weixin/telegram/discord/…）一律视为远程消息会话。空段（无键/非 agent:ns 布局）
# 保守按本地处理——宁可弹本机窗也不静默拒本地审批。
_LOCAL_SESSION_PLATFORMS = frozenset({"local", "cli"})


def session_platform_of(session_key: str) -> str:
    """agent:<ns>:<platform>:<chat_type>:… → <platform>；非此布局返回 \"\"。"""
    parts = str(session_key or "").split(":")
    if len(parts) >= 3 and parts[0] == "agent":
        return parts[2].strip().lower()
    return ""


def is_local_session(session_key: str) -> bool:
    plat = session_platform_of(session_key)
    # "local" = gateway 托管本地会话键（session_local_recovery.local_source 实证）；
    # NOTICE_PLATFORMS = turn 归因平台名（desktop/tui/cli），双保险覆盖非标准键形。
    return plat == "" or plat in _LOCAL_SESSION_PLATFORMS or plat in NOTICE_PLATFORMS


def _present_cfg() -> dict:
    try:
        if _PRESENT_CFG_FN is not None:
            v = _PRESENT_CFG_FN()
            if isinstance(v, dict):
                return v
    except Exception:
        pass
    return {}


def _log(msg: str) -> None:
    try:
        if _LOGGER is not None:
            _LOGGER.info("gateway-ui: approval %s", msg)
    except Exception:
        pass


def init(bus_dir, stop_event=None, logger=None, cfg_fn=None) -> None:
    """register() 调用：绑定总线目录/stop/日志/approval 块读探针，清空上一轮残留。
    cfg_fn 供 R29 分流现读 platform_takeover（每次 present 现读，设置页改完即生效）。"""
    global _BUS_DIR, _STOP, _LOGGER, _PRESENT_CFG_FN
    global _IPC_SERVER, _IPC_ENDPOINT, _IPC_TOKEN
    if _IPC_SERVER is not None:
        try:
            _IPC_SERVER.close()
        except Exception:
            pass
        _IPC_SERVER = None
    _BUS_DIR = Path(bus_dir)
    _STOP = stop_event
    _LOGGER = logger
    _PRESENT_CFG_FN = cfg_fn
    with _LOCK:
        _PENDING.clear()
        _SESSION_HINTS.clear()
        _RESPONSES.clear()
    try:
        _IPC_ENDPOINT = approval_ipc.new_endpoint(_BUS_DIR)
        _IPC_TOKEN = approval_ipc.new_handshake()
        _IPC_SERVER = approval_ipc.ApprovalResponseServer(
            _IPC_ENDPOINT, _receive_ipc, logger=logger)
        _IPC_SERVER.start()
    except Exception as exc:
        _IPC_ENDPOINT = ""
        _IPC_TOKEN = ""
        _log(f"approval IPC unavailable (fail-closed): {type(exc).__name__}")


def shutdown() -> None:
    """停止本轮 IPC 监听；旧 worker 的响应不再进入新一轮宿主。"""
    global _IPC_SERVER, _IPC_ENDPOINT, _IPC_TOKEN
    server = _IPC_SERVER
    _IPC_SERVER = None
    _IPC_ENDPOINT = ""
    _IPC_TOKEN = ""
    with _LOCK:
        _RESPONSES.clear()
    if server is not None:
        try:
            server.close()
        except Exception:
            pass


def worker_meta() -> dict:
    """给 side-env worker 的公开端点；握手 token 只走 Supervisor 的 stdin。"""
    return {"approval_endpoint": _IPC_ENDPOINT} if _IPC_ENDPOINT else {}


def worker_handshake() -> str:
    """返回当前 worker 启动握手；调用方只能通过 stdin 传递。"""
    return _IPC_TOKEN


# ------------------------------------------------------------ 权威源 ----
def pending_count() -> int:
    """R28-5：当前等待人类批准的请求数（消费线程每拍读，进 state.json）。"""
    with _LOCK:
        return len(_PENDING)


def note_request_session(request_id, session_key) -> None:
    """pre_approval_request hook（主循环线程，O(1)）记 request_id→session_key，
    供 cancel_session 按会话取消。仅 transport 面（surface=transport:*）调用。"""
    rid = str(request_id or "")
    if not rid:
        return
    _SESSION_HINTS[rid] = str(session_key or "")[:48]
    if len(_SESSION_HINTS) > _HINT_CAP:
        for k in list(_SESSION_HINTS)[:len(_SESSION_HINTS) - _HINT_CAP]:
            _SESSION_HINTS.pop(k, None)


def cancel_session(session_key) -> None:
    """turn 被打断（agent_loop_stopped）→ 标记该会话的 pending 为 cancelled，
    present 下一拍即返回 no_response 释放 transport worker 槽（≤8）。"""
    sk = str(session_key or "")
    if not sk:
        return
    with _LOCK:
        for e in _PENDING.values():
            if e.get("session_key") == sk:
                e["cancelled"] = True


# ------------------------------------------------------------ 总线 ----
def _write_req_locked() -> None:
    """pending 全量快照 → approval_req.json（调用方持 _LOCK）。"""
    global _REQ_SEQ
    if _BUS_DIR is None:
        return
    _REQ_SEQ += 1
    reqs = []
    for rid, e in _PENDING.items():
        r = e["request"]
        try:
            timeout = float(r.timeout_seconds or 300)
        except (TypeError, ValueError):
            timeout = 300.0
        reqs.append({
            "request_id": rid,
            "digest": r.digest,
            "command": r.command,            # 宿主已脱敏副本，原样透传
            "description": r.description,
            "surface": r.surface,
            "session_key": str(e.get("session_key") or "")[:16],
            "pattern_key": r.pattern_key,
            "allowed_choices": list(r.allowed_choices),
            "timeout_seconds": r.timeout_seconds,
            "expires_ts": round(e["ts"] + timeout, 3),
        })
    try:
        write_json(_BUS_DIR / "approval_req.json",
                   {"seq": _REQ_SEQ, "ts": round(time.time(), 3), "requests": reqs})
    except Exception as exc:
        _log(f"req write failed: {exc}")


def _append_withdrawn(rid: str, reason: str) -> None:
    if _BUS_DIR is None:
        return
    try:
        p = _BUS_DIR / "approval_withdrawn.jsonl"
        with _LOCK:
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"request_id": rid, "reason": reason,
                                    "ts": round(time.time(), 3)},
                                   ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _worker_identity():
    """读取当前租约的真实 worker PID 和 spawn token。"""
    if _BUS_DIR is None:
        return 0, ""
    try:
        p = _BUS_DIR / "tray_lease.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return int(data.get("pid") or 0), str(data.get("spawn_token") or "")
    except Exception:
        return 0, ""


def _receive_ipc(payload: dict, peer_pid: int) -> dict:
    """验收 side-env 的一次审批决定；失败全部拒绝且不改变 pending。"""
    if not isinstance(payload, dict):
        return {"ok": False}
    rid = str(payload.get("request_id") or "")
    digest = str(payload.get("digest") or "")
    choice = str(payload.get("choice") or "")
    token = str(payload.get("token") or "")
    sent_spawn = str(payload.get("spawn_token") or "")
    with _LOCK:
        entry = _PENDING.get(rid)
        if entry is None:
            return {"ok": False}
        expected_pid, expected_spawn = _worker_identity()
        request = entry["request"]
        if not peer_pid or peer_pid != expected_pid or not _worker_alive():
            return {"ok": False}
        if not _IPC_TOKEN or not hmac.compare_digest(token, _IPC_TOKEN):
            return {"ok": False}
        if not hmac.compare_digest(sent_spawn, expected_spawn):
            return {"ok": False}
        if not hmac.compare_digest(digest, str(request.digest or "")):
            return {"ok": False}
        if choice not in tuple(request.allowed_choices):
            return {"ok": False}
        if rid in _RESPONSES:
            return {"ok": False}
        _RESPONSES[rid] = choice
    return {"ok": True}


def _read_resp_choice(rid: str):
    """一次性消费内存中的 IPC 决定；不再读取 approval_resp.json。"""
    with _LOCK:
        return _RESPONSES.pop(rid, None)


def _worker_alive() -> bool:
    """worker 存活 = tray_lease.json mtime 新鲜（<8s）。文件缺失=worker 未起/已死。"""
    if _BUS_DIR is None:
        return False
    try:
        p = _BUS_DIR / "tray_lease.json"
        if not p.is_file():
            return False
        return (time.time() - p.stat().st_mtime) < WORKER_LEASE_TTL_S
    except Exception:
        return False


# ------------------------------------------------------------ present ----
def _wait_decision(request, entry):
    """轮询直到拿到 choice 或触发自终止。返回 choice(str) 或 None（fail-closed）。"""
    rid = request.request_id
    try:
        timeout = float(request.timeout_seconds)
    except (TypeError, ValueError):
        timeout = 300.0
    deadline = time.time() + max(1.0, timeout - SELF_TERMINATE_MARGIN_S)
    while True:
        if _STOP is not None and _STOP.is_set():
            entry["reason"] = "withdrawn"
            return None
        if entry.get("cancelled"):
            entry["reason"] = "withdrawn"
            return None
        choice = _read_resp_choice(rid)
        if choice is not None:
            return choice
        if not _worker_alive():
            entry["reason"] = "expired"
            return None
        if time.time() >= deadline:
            entry["reason"] = "expired"
            return None
        time.sleep(_POLL_S)


def present(request):
    """approval transport present_fn（宿主 daemon 线程内阻塞）。返回 ApprovalDecision。
    合法 choice → respond(choice)；其余一律 respond("no_response")（宿主判 invalid
    fail-closed，归因 transport_invalid，不冒充用户拒绝）。
    R29 平台分流：platform_takeover=false（默认）时消息平台会话不接管——立即
    no_response，交宿主按 transport_fallback 顺序回落到原生聊天卡；不登记 pending、
    不写 req、不弹窗，await 权威源也因此不被远程会话点亮。"""
    rid = request.request_id
    session_key = _SESSION_HINTS.get(rid, "")
    takeover = bool(_present_cfg().get("platform_takeover", False))
    if not takeover and not is_local_session(session_key):
        _SESSION_HINTS.pop(rid, None)   # 交还即释放 hint，不随远程会话攒垃圾
        _log(f"present {rid[:8]} deferred to native surface "
             f"(platform={session_platform_of(session_key) or '?'}, takeover=false)")
        return request.respond("no_response")
    entry = {"request": request, "session_key": session_key,
             "cancelled": False, "reason": "completed", "ts": time.time()}
    with _LOCK:
        _PENDING[rid] = entry
        _write_req_locked()
    _log(f"present {rid[:8]} surface={request.surface} choices={request.allowed_choices}")
    try:
        choice = _wait_decision(request, entry)
        if choice is not None and choice in tuple(request.allowed_choices):
            entry["reason"] = "completed"
            return request.respond(choice)
        return request.respond("no_response")
    finally:
        with _LOCK:
            _PENDING.pop(rid, None)
            _SESSION_HINTS.pop(rid, None)
            _write_req_locked()
        _append_withdrawn(rid, entry.get("reason", "completed"))
