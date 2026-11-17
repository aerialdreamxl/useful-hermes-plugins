"""gateway-ui —— 适用于 Hermes Gateway 的 Qt GUI 组件（整体重写）。

架构（PROMPT.md 定稿，宿主侧）：
- hook 回调（asyncio 主循环线程）只做 queue.put，O(1) 返回，绝不阻塞主循环；
- 消费线程跑纯函数状态机（statemachine.py）+ 文件总线（bus.py，§5 姊妹契约，
  字段白名单——绝无消息正文/密钥）；
- 托盘+桌宠 = side-env 独立 worker 进程（worker/，PySide6+Fluent），只读总线，
  崩溃隔离于 Gateway；supervisor.py 心跳判活+永不放弃（A3/A4 根除）；
- 配置三态解析（config.py）：enable-pet 缺键≠false，默认补齐不回写（A1 根除）。

宿主门控（站立判定）：只在 gateway run 进程里活；dashboard/cli 等宿主加载即 noop。
所有路径全量 try/except，失败只降级不抛出。
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from queue import Queue, Empty, Full

from . import config as cfgmod
from . import approval_host
from .bus import Bus, read_json, write_json
from .statemachine import StateMachine, url_label
from .supervisor import Supervisor

_PLUGIN_DIR = Path(__file__).resolve().parent
WORKER_PATH = _PLUGIN_DIR / "worker" / "main.py"
BUS_DIR_NAME = os.path.join("cache", "gateway-ui")

_CTX = None
_Q: "Queue[tuple]" = Queue(maxsize=4096)
_SEQ = 0
_SM = StateMachine()
_STOP = threading.Event()
_T0 = time.time()
_BUS = None
_SUP = None
_BAL = None             # 余额气泡：BalanceWatcher（宿主余额查询线程，register 建）
_VERBOSE_CACHE = {"v": False, "ts": 0.0}


# ---------------------------------------------------------------- 基础设施 ----
def _home() -> Path:
    try:
        from hermes_cli.config import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _repo() -> Path:
    try:
        import hermes_cli
        return Path(hermes_cli.__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def _version() -> str:
    try:
        from hermes_cli.version_info import get_version_info
        v = get_version_info().derived_version
        return str(v) if v and v != "unknown" else _version_fallback()
    except Exception:
        return _version_fallback()


def _version_fallback() -> str:
    try:
        from hermes_cli import __version__ as v
        return str(v)
    except Exception:
        return "?"


def _log(msg: str) -> None:
    with contextlib.suppress(Exception):
        if _CTX is not None:
            _CTX.logger.info("gateway-ui: %s", msg)


def _vlog(msg: str) -> None:
    if _BUS is not None:
        _BUS.vlog(msg, _verbose(), "GW")


def _s(key):
    return cfgmod.read_top(_CTX, key)


def _verbose() -> bool:
    now = time.time()
    if now - _VERBOSE_CACHE["ts"] > 2.0:
        _VERBOSE_CACHE["ts"] = now
        with contextlib.suppress(Exception):
            _VERBOSE_CACHE["v"] = bool(_s("verbose_log"))
    return _VERBOSE_CACHE["v"]


def _tray_enabled() -> bool:
    """每拍直读 config（与 pet watcher 同款实时语义）：改 tray_enabled 无需重启。
    不走 _s 的缺键补齐——补齐只在 register 做一次，避免每秒空转 writer 判定。"""
    try:
        v = _CTX.get_config("tray_enabled", cfgmod.MISSING)
        if v is cfgmod.MISSING:
            return True
        return bool(v)
    except Exception:
        return True


def _pet_runtime() -> dict:
    """pet 块运行时真值（设置界面：config.yaml 唯一事实源，官方 get_config 直读）。
    pet_cfg.json 中转退役；缺键补默认仅进运行时，绝不落 config（A1 铁律）。"""
    explicit = cfgmod.read_pet_explicit(_CTX)
    out = dict(cfgmod.PET_DEFAULTS)
    out.update(explicit)
    out["enable-pet"] = bool(explicit.get("enable-pet", False))
    return out


def _installed_slugs() -> tuple:
    """已安装 petdex 库存（只读枚举 <home>/pets/，与 worker sprites 同口径）。"""
    try:
        d = _home() / "pets"
        if not d.is_dir():
            return ()
        return tuple(sorted(p.name for p in d.iterdir()
                            if (p / "spritesheet.webp").is_file()
                            or (p / "spritesheet.png").is_file()))
    except Exception:
        return ()


# ---------------------------------------------------------------- hooks ----
def _put(kind: str, **fields) -> None:
    try:
        _Q.put_nowait((kind, time.time(), fields))
    except Full:
        pass  # 背压即丢，主循环绝不等待


def _clip(v, n=48):
    return str(v or "")[:n]


def _on_inbound(*, event=None, **kw):
    src = getattr(event, "source", None)
    plat = getattr(src, "value", None) if src is not None else None
    _put("inbound", platform=_clip(plat or getattr(event, "platform", ""), 24),
         session_key=_clip(getattr(event, "session_key", "") or getattr(event, "chat_id", "")))


def _resolve_baseline_endpoint():
    """主配置端点（降级判定：降级以 base_url 判定）。model.provider 形如 custom:<name>
    时查 providers.<name>.base_url；取不到返回 None，消费端退化 first-seen。"""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        prov = ((cfg.get("model") or {}).get("provider")) or ""
        name = prov.split(":", 1)[1] if ":" in prov else prov
        entry = (cfg.get("providers") or {}).get(name) or {}
        return _clip(entry.get("base_url") or "", 100) or None
    except Exception:
        return None


def _on_pre_api(*, session_id="", task_id="", turn_id="", provider="", model="",
                platform="", base_url="", **kw):
    if _SM.baseline_provider is None:
        _SM.baseline_provider = _clip(provider, 40) or None
    if _SM.baseline_base_url is None:
        _SM.baseline_base_url = _resolve_baseline_endpoint() or _clip(base_url, 100) or None
    _put("turn_start", provider=_clip(provider, 40), model=_clip(model, 40),
         base_url=_clip(base_url, 100), session_key=_clip(session_id),
         task_key=_clip(task_id), turn_key=_clip(turn_id),
         platform=_clip(platform, 24))


def _on_post_api(*, provider="", model="", session_id="", turn_id="", base_url="", **kw):
    _put("post_api", provider=_clip(provider, 40), model=_clip(model, 40),
         base_url=_clip(base_url, 100), session_key=_clip(session_id))


def _on_post_llm(*, session_id="", task_id="", turn_id="", model="", platform="", **kw):
    _put("turn_end", model=_clip(model, 40), session_key=_clip(session_id),
         task_key=_clip(task_id), turn_key=_clip(turn_id),
         platform=_clip(platform, 24), ok=True)


def _on_pre_tool(*, tool_name="", session_id="", **kw):
    _put("tool", tool=_clip(tool_name, 40), session_key=_clip(session_id))
    # 事件反应：clarify 工具自身不发 hook（tools/clarify_tool.py 无 hook 调用），
    # "需要人类输入"从 pre_tool_call 截 tool_name=="clarify"。平台归因交给
    # 状态机 _sess_plat 表（turn_start 实锤）；这里只入队，O(1)。
    if tool_name == "clarify":
        _put("await_input", source="clarify", session_key=_clip(session_id))


def _on_api_error(*, provider="", model="", status_code=None, error_type="", session_id="", **kw):
    _put("error", provider=_clip(provider, 40), model=_clip(model, 40),
         session_key=_clip(session_id), tool=_clip(error_type, 24), ok=False,
         status=_clip(status_code, 8))


def _on_loop_stopped(*, session_key="", platform="", reason="", **kw):
    _put("turn_end", session_key=_clip(session_key), platform=_clip(platform, 24),
         ok=False, tool=_clip(reason, 24))
    # R28：turn 打断 → 取消该会话的 pending 审批（present 下一拍释放 transport 槽）。
    _put("cancel_approval", session_key=_clip(session_key))


def _on_session_end(*, session_id="", task_id="", turn_id="", completed=False,
                    failed=False, interrupted=False, turn_exit_reason="",
                    model="", platform="", **kw):
    """事件反应：agent/turn_finalizer.py:772 每轮结束发（名字叫 session 实为 per-turn）。
    结局三标志原样入队，notice 派生（done/failed/stopped + 平台门控 + turn 幂等）
    全在消费线程状态机里做——回调只入队，O(1)。turn_exit_reason 不入总线（白名单）。"""
    _put("turn_end", session_key=_clip(session_id), task_key=_clip(task_id),
         turn_key=_clip(turn_id), platform=_clip(platform, 24),
         completed=bool(completed), failed=bool(failed), interrupted=bool(interrupted))


def _on_pre_approval(*, session_key="", surface="", session_id="", request_id="", **kw):
    """事件反应：tools/approval*.py 多处发 pre_approval_request（observer，不可否决）。
    command/description/pattern_key 等正文一律不入总线（字段白名单铁律）；
    surface 只取 cli/gateway/smart 三值（transport:*/mcp-elicitation 等归一 ""）。
    平台归因在状态机：session_key（gateway 键 agent:ns:platform:…）解析 +
    session_id（_fire_approval_hook 注入，对 _sess_plat 表）双路，宁漏报不误报。
    R28：transport 面（surface=transport:*）带 request_id → O(1) 记会话 hint，
    供 approval_host.cancel_session 按会话取消（turn 打断释放 transport 槽）。"""
    # R29 hint 捕获：present 分流判定要现读 session_key（hook 与 present 同宿主
    # 进程同线程顺序：pre_approval_request 先于 present_fn）。transport 面
    # （surface=transport:*）才会进 present，条件保留。
    if request_id and str(surface).startswith("transport:"):
        approval_host.note_request_session(request_id, session_key or session_id)
    surf = surface if surface in ("cli", "gateway", "smart") else ""
    _put("await_input", source="approval", session_key=_clip(session_key),
         session_id=_clip(session_id), surface=surf)


# ------------------------------------------------------------ 权威活跃源 ----
def _poll_authority() -> None:
    """十五/写者租约：runner._running_agent_items() 全 turn 总登记（含桌面端）。
    连续 3 拍不可达退事件兜底。
    R27 子代理监听（subagent_busy 开才介入）：同进程直读
    delegate_tool_registry.list_active_subagents()——子代理 spawn 注册、
    结束注销，表寿命=真实运行期；后台派发父空闲后子代理仍挂表。
    会话全空闲但子代理在跑 → derive acting / 灯橙（主人裁定：算在工作）。"""
    n_sub = 0
    if _s("subagent_busy"):
        try:
            from tools.delegate_tool_registry import list_active_subagents
            n_sub = len(list_active_subagents())
        except Exception:
            n_sub = 0
    _SM.subagents = n_sub
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        items = runner._running_agent_items() if runner is not None else None
        if items is None:
            raise RuntimeError("no runner")
        prev = _SM.authority
        _SM.set_authority(items, subagents=n_sub)
        if _SM.authority != prev:
            _vlog(f"authority -> {_SM.authority} subs={n_sub}")
    except Exception:
        _SM.authority_unreachable()
        if _SM.authority is None and _SM.authority_streak == 3:
            _vlog("authority unreachable 3x -> FALLBACK(events)")


# ------------------------------------------------------------ 消费线程 ----
def _consumer(stop=None) -> None:
    global _SEQ, _BUS
    stop = stop if stop is not None else _STOP
    try:
        _BUS = Bus(_home() / BUS_DIR_NAME, logger=getattr(_CTX, "logger", None))
    except Exception as exc:
        _log(f"bus dir failed, consumer dead: {exc}")
        return
    last_beat = 0.0
    last_lease = 0.0
    last_state_sig = None
    my_pid = os.getpid()
    while not stop.wait(0.05):
        try:
            now_l = time.time()
            if now_l - last_lease >= 2.0:   # 硬件友好写盘：2s utime 心跳（读侧 TTL 4s）
                last_lease = now_l
                _BUS.renew_writer_lease(my_pid)
        except Exception:
            pass
        dirty = False
        try:
            while True:
                kind, ts, fl = _Q.get_nowait()
                if kind == "cancel_approval":
                    # R28 内部控制信号（不进总线/不计数）：取消该会话 pending 审批。
                    approval_host.cancel_session(fl.get("session_key", ""))
                    continue
                dirty = True
                _SEQ += 1
                row = {"seq": _SEQ, "ts": round(ts, 3), "kind": kind}
                row.update({k: v for k, v in fl.items() if v})
                deg_before = _SM.last_degrade
                _SM.apply(kind, ts, fl)
                bus_on = bool(_s("bus_enabled"))
                if _SM.last_degrade is not deg_before and bus_on:
                    _SEQ += 1
                    d = _SM.last_degrade
                    _BUS.append_event({"seq": _SEQ, "ts": round(ts, 3), "kind": "degrade",
                                       "model": f"{url_label(d['from'])}->{url_label(d['to'])}"})
                if bus_on:
                    _BUS.append_event(row)
        except Empty:
            pass
        except Exception as exc:
            _log(f"consumer loop error: {exc}")
        now = time.time()
        _SM.gc(now)
        deg_before_auth = _SM.last_degrade
        _poll_authority()
        # R28-5：pending 审批表实时读数（权威源，表寿命=真实等待窗口）。
        _SM.pending_approvals = approval_host.pending_count()
        # 权威源读内部降级标志新记的降级 → 补一条 degrade 事件（与事件启发式同格式）
        d = _SM.last_degrade
        if d is not None and d is not deg_before_auth and bool(_s("bus_enabled")):
            _SEQ += 1
            _BUS.append_event({"seq": _SEQ, "ts": round(now, 3), "kind": "degrade",
                               "model": f"{url_label(d['from'])}->{url_label(d['to'])}"})
            dirty = True
        prev_ps = _SM.pet_state
        _SM.pet_state = _SM.derive(now)
        if _SM.pet_state != prev_ps:
            dirty = True
            _vlog(f"pet_state {prev_ps} -> {_SM.pet_state} "
                  f"(n={_SM.active_count()} src={'auth' if _SM.authority is not None else 'ev'})")
        window = float(_s("degrade_window_s") or 300)
        if dirty or now - last_beat > 5.0:
            last_beat = now
            tier, light = _SM.tier_and_light(now, window)
            deg = _SM.last_degrade
            st = {
                # §5 姊妹契约字段（白名单，无正文/密钥）
                "pet_state": _SM.pet_state,
                "active_sessions": _SM.active_count(),
                "active_source": "authority" if _SM.authority is not None else "events",
                # R28-5：等待人类批准数（纯增量；dashboard 忽略未知字段不受影响）。
                "pending_approvals": _SM.pending_approvals,
                "verbose": _verbose(),
                "writer_pid": my_pid,
                # 硬件友好写盘：uptime 改 boot 锚（常量），消费侧自算 now-boot_ts；
                # updated_ts 死字段移除（新鲜度读侧看 mtime）。
                "boot_ts": round(_T0, 3),
                "last_event_seq": _SEQ,
                # 事件反应 notice（纯增量；dashboard 忽略未知字段不受影响）：
                # 最后一个 notice {seq,kind,ts,platform}，无则 null。
                "notice": _SM.notice,
                "tray": {
                    "light": light, "tier": tier,
                    "degrade": None if not deg else
                    {"from": url_label(deg["from"]), "to": url_label(deg["to"]),
                     "from_url": deg["from"], "to_url": deg["to"],
                     "to_provider": deg.get("provider", ""),
                     "ts": round(deg["ts"], 1)},
                    "degrade_window_s": int(window),
                    "version": _version(),
                    "update_available": _SM.update_available,
                    "bus": bool(_s("bus_enabled")),
                },
            }
            # 硬件友好写盘：内容无变不重写 JSON，只 utime 维持读侧 mtime TTL
            sig = json.dumps(st, ensure_ascii=False, sort_keys=True)
            if sig != last_state_sig:
                last_state_sig = sig
                _BUS.write_state(st)
            else:
                _BUS.touch_state()
            _write_view()   # 设置界面：settings 快照与 state 同拍（官方 get_config 读）


# ------------------------------------------------------------ settings 回写 ----
# 设置界面 F7：settings_req.json → 官方 writer → settings_ack.json（CONTRACT §6）。
# config.yaml 只有官方 ctx.set_config 碰；本 watcher 只做校验+转发+回执。
_REQ_DONE = []          # 最近 64 个已处理请求 id（幂等去重）
_VIEW_LAST = {"sig": None}
# F5 总线鉴权：register 生成随机 token 写 <bus>/.bus_token，settings_req 必须携带。
# 无鉴权时任何同机进程都能发 settings_req → 宿主代写 config（providers_dir 代执行）。
_BUS_TOKEN = {"v": ""}
TOKEN_NAME = ".bus_token"


def _write_bus_token(bus: Path) -> str:
    """生成并落盘总线 token（secrets.token_hex(16)，尽力 chmod 0600）。
    写失败返回空串 → watcher 走"接受但留证"兼容层（旧 worker 不发 token）。"""
    try:
        tok = secrets.token_hex(16)
        bus.mkdir(parents=True, exist_ok=True)
        p = bus / TOKEN_NAME
        p.write_text(tok, encoding="utf-8")
        with contextlib.suppress(Exception):
            os.chmod(p, 0o600)   # POSIX 生效；NTFS 忽略（同用户 ACL 已隔其他用户）
        return tok
    except Exception as exc:
        _log(f"bus token write failed (settings bus degraded): {exc}")
        return ""


def _ack(path: Path, req_id: str, ok: bool, key: str, value, error: str = "",
         token_state: str = "ok") -> None:
    with contextlib.suppress(Exception):
        write_json(path, {"id": req_id, "ok": bool(ok), "key": key,
                          "value": value, "error": error, "ts": round(time.time(), 3),
                          # token 状态标记：ok=已校验；missing=宿主持久化失败/旧 worker
                          # 兼容层的降级放行（供取证，不是工作流依赖）
                          "token": token_state})


def _apply_setting(key: str, value) -> None:
    """官方 writer 落盘。pet.* 合并成完整 pet 块整体写（与既有整块语义一致，
    保留块内其他键）；顶层键直写。config.yaml 只有官方 ctx.set_config 碰。"""
    if key.startswith("pet."):
        leaf = key[4:]
        # N22：整块合并必须基于 read_pet_raw（原样块）——用过滤后的显式块写回会
        # 静默丢掉用户手加的白名单外键（pet 块里作者自己的扩展字段）。
        block = dict(cfgmod.read_pet_raw(_CTX))
        block[leaf] = value
        _CTX.set_config("pet", block)
    elif key.startswith("balance."):
        # 余额气泡：balance.* 同 pet.* 整块合并语义（保留块内其他键，不写点号顶层键）
        leaf = key[8:]
        block = dict(cfgmod.read_balance_explicit(_CTX))
        block[leaf] = value
        _CTX.set_config("balance", block)
    elif key.startswith("approval."):
        # 审批弹窗：approval.* 整块合并（基于 raw 保白名单外用户手加键，N22 同款）
        leaf = key[len("approval."):]
        block = dict(cfgmod.read_approval_raw(_CTX))
        block[leaf] = value
        _CTX.set_config("approval", block)
    else:
        _CTX.set_config(key, value)


def _settings_watcher(stop=None) -> None:
    """0.25s 轮询 settings_req.json mtime → validate_request → 官方 writer → 回执。
    同 id 幂等（最近 64 个）；非法请求拒写但照常回执 error。绝不自己碰 config 文件。
    stop=本轮 register 的独立 token（旧轮线程持旧 token 永久退出，不随新 register 复活）。"""
    stop = stop if stop is not None else _STOP
    bus = _home() / BUS_DIR_NAME
    req_p = bus / "settings_req.json"
    ack_p = bus / "settings_ack.json"
    # 启动基线：register 之前遗留的请求文件视为陈旧，首轮不处理（防重启/重 register
    # 把上一轮的旧请求当新请求重放）。只认启动后新写入（mtime 变化）的请求。
    last = req_p.stat().st_mtime if req_p.exists() else None
    last_sig = req_p.read_text(encoding="utf-8") if req_p.exists() else None
    first = True
    tok_warned = False
    while not stop.wait(0.01 if first else 0.25):
        first = False
        try:
            if not req_p.exists():
                continue
            mtime = req_p.stat().st_mtime
            raw = req_p.read_text(encoding="utf-8")
            # mtime + 内容签名双判（NTFS mtime 精度有限，快速连续写可能同 tick）
            if mtime == last and raw == last_sig:
                continue
            last = mtime
            last_sig = raw
            req = read_json(req_p, None)
            if not isinstance(req, dict):
                continue
            req_id = str(req.get("id") or "")
            if req_id and req_id in _REQ_DONE:
                continue  # 幂等：已处理过的 id 不重复落盘
            # F5：总线鉴权。只认本进程 register 时写下的 token——伪造请求直接在
            # 这里挡掉，不进幂等表、不碰官方 writer、只留一条拒绝回执。
            expected = _BUS_TOKEN.get("v") or ""
            token_state = "ok"
            if expected:
                if str(req.get("token") or "") != expected:
                    _ack(ack_p, req_id, False, str(req.get("key") or ""), None,
                         "bad bus token (request not from this host's worker)")
                    _vlog(f"settings REJECTED (bad token) key={req.get('key')!r}")
                    continue
            else:
                # 兼容层：token 文件写失败/旧版 worker 不发 token → 接受但留证
                # （ack.token="missing"），绝不静默。
                token_state = "missing"
                if not tok_warned:
                    tok_warned = True
                    _log("settings bus token missing -> accepting unauthenticated "
                         "requests (compat layer)")
            ok, key, value, error = cfgmod.validate_request(req, _installed_slugs())
            if ok:
                try:
                    _apply_setting(key, value)   # 官方 writer：锁+保注释+原子写
                except Exception as exc:
                    ok, error = False, f"writer failed: {exc}"
            if req_id:
                _REQ_DONE.append(req_id)
                if len(_REQ_DONE) > 64:
                    del _REQ_DONE[:len(_REQ_DONE) - 64]
            _ack(ack_p, req_id, ok, key, value, error, token_state)
            if ok:
                _vlog(f"settings -> config.yaml: {key}={value!r}")
            else:
                _vlog(f"settings REJECTED {key!r}: {error}")
        except Exception:
            pass


def _write_view() -> None:
    """settings_view.json 快照（官方 get_config 读取）；仅内容有变才落盘，不空转。"""
    try:
        view = cfgmod.build_view(_CTX, _installed_slugs())
        sig = json.dumps(view, ensure_ascii=False, sort_keys=True)
        if sig == _VIEW_LAST["sig"]:
            return
        _VIEW_LAST["sig"] = sig
        view["ts"] = round(time.time(), 3)
        write_json(_home() / BUS_DIR_NAME / "settings_view.json", view)
    except Exception:
        pass
    # 余额气泡：balance 配置变更同拍进 view（watcher 周期循环自己也写，签名去重无重复落盘）
    if _BAL is not None:
        with contextlib.suppress(Exception):
            _BAL.write_view()


# ------------------------------------------------------------ 更新检查 ----
def _update_checker(stop=None) -> None:
    """官方 source_check.check_for_updates(passive=True)（banner 同源，带缓存，
    尊重 updates.check=false）。900s 一次；fail-safe 查不到=不亮。"""
    stop = stop if stop is not None else _STOP
    while not stop.wait(900):
        try:
            from hermes_cli.source_check import check_for_updates
            r = check_for_updates(passive=True)
            behind = r.get("behind")
            _SM.update_available = bool(isinstance(behind, int) and behind > 0)
        except Exception:
            _SM.update_available = False


# ------------------------------------------------------------ 宿主门控 ----
def _is_gateway_host() -> bool:
    """站立判定：只许在 gateway run 进程里活（dashboard 双写者震荡根因）。
    判定双保险：argv 含 gateway+run/start，或 gateway.run 已导入本进程。"""
    try:
        argv = [str(a).lower() for a in sys.argv]
        if "gateway" in argv and any(a in ("run", "start") for a in argv):
            return True
        return "gateway.run" in sys.modules
    except Exception:
        return False


# ------------------------------------------------------------ 入口 ----
_GEN_STOP = None        # 本轮 register 的独立 stop token（旧轮线程持旧 token 永久退出）


def register(ctx) -> None:
    global _CTX, _SUP, _STOP, _GEN_STOP
    if not _is_gateway_host():
        with contextlib.suppress(Exception):
            ctx.logger.info(
                "gateway-ui: loaded in a non-gateway process (%s); "
                "plugin stays inert here (bus/tray/hooks are gateway-run only)",
                " ".join(str(a) for a in sys.argv[1:])[:100])
        return
    # 设置界面：每轮 register 发独立 stop token——旧轮线程持旧 token，_STOP 重建后
    # 旧线程永久退出，绝不随新 register 复活（多次 register 线程累积竞态根除）。
    if _GEN_STOP is not None:
        _GEN_STOP.set()
    _GEN_STOP = threading.Event()
    _STOP = _GEN_STOP
    _REQ_DONE.clear()
    _VIEW_LAST["sig"] = None
    _CTX = ctx
    for k in cfgmod.TOP_DEFAULTS:
        _s(k)  # 顶层显示类逐键补齐
    # pet 块：显示类缺键补齐；enable-pet 三态（缺键不落 false，A1 根除）
    cfgmod.fill_pet_display(ctx, cfgmod.read_pet_explicit(ctx))
    # 审批弹窗：popup_sounds 显示类缺键补齐；popup_enabled 修改类缺失保持缺失（A1）
    cfgmod.fill_approval_display(ctx)
    # F5：settings 总线 token（每轮 register 换新；worker 每次发请求前现读现带）
    _BUS_TOKEN["v"] = _write_bus_token(_home() / BUS_DIR_NAME)
    # 设置界面：pet_cfg.json 播种退役——config.yaml 唯一事实源，settings_view 由
    # 消费线程每拍从官方 get_config 出快照；worker 首启回落 meta.pet（supervisor 写）。
    ctx.register_hook("pre_gateway_dispatch", _on_inbound)
    ctx.register_hook("pre_api_request", _on_pre_api)
    ctx.register_hook("post_api_request", _on_post_api)
    ctx.register_hook("post_llm_call", _on_post_llm)
    ctx.register_hook("pre_tool_call", _on_pre_tool)
    ctx.register_hook("api_request_error", _on_api_error)
    ctx.register_hook("agent_loop_stopped", _on_loop_stopped)
    ctx.register_hook("on_session_end", _on_session_end)          # 事件反应：per-turn 结局
    ctx.register_hook("pre_approval_request", _on_pre_approval)   # 事件反应：等人类审批
    # 审批弹窗 R28-1：注册 approval transport（惰性——须用户配 security.approval.
    # transport: gateway-ui 才激活；注册本身零副作用）。present 跑宿主 daemon 线程。
    # 旧宿主无此 API → 静默降级（审批仍走内置面）。
    approval_host.init(_home() / BUS_DIR_NAME, stop_event=_STOP,
                       logger=getattr(ctx, "logger", None),
                       cfg_fn=lambda: cfgmod.read_approval_explicit(ctx))
    try:
        ctx.register_approval_transport("gateway-ui", approval_host.present)
        _log("approval transport registered (inactive until security.approval.transport selects it)")
    except Exception as exc:
        _log(f"approval transport unavailable (degraded to builtin): {exc}")
    _SUP = Supervisor(
        bus_dir=_home() / BUS_DIR_NAME, worker_path=WORKER_PATH,
        home=_home(), repo=_repo(), version_fn=_version,
        pet_cfg_fn=_pet_runtime, tray_enabled_fn=_tray_enabled,
        index_url_fn=lambda: _s("package_index_url"),
        logger=getattr(ctx, "logger", None), vlog=_vlog, stop_event=_STOP)
    threading.Thread(target=_consumer, args=(_STOP,), name="gui-bus", daemon=True).start()
    threading.Thread(target=_update_checker, args=(_STOP,), name="gui-update", daemon=True).start()
    threading.Thread(target=_settings_watcher, args=(_STOP,), name="gui-settings", daemon=True).start()
    threading.Thread(target=_SUP.run, name="gui-tray", daemon=True).start()
    # 余额气泡：宿主余额查询线程（balance.enabled=false 时内部空转，不拉不写）。
    global _BAL
    try:
        from .balance_host import BalanceWatcher
        _BAL = BalanceWatcher(
            bus_dir=_home() / BUS_DIR_NAME,
            providers_dir=str(_home() / "balance-providers"),
            get_cfg=lambda: cfgmod.read_balance_explicit(_CTX),
            python_exe=sys.executable,
            logger=getattr(ctx, "logger", None))
        threading.Thread(target=_BAL.run, args=(_STOP,),
                         name="gui-balance", daemon=True).start()
    except Exception as exc:
        _BAL = None
        _log(f"balance watcher start failed (degraded): {exc}")
    atexit.register(_shutdown)
    ctx.on_unload(_shutdown)
    _log("registered (bus + tray supervisor + settings watcher + balance)")


def _shutdown(*a) -> None:
    _STOP.set()
    if _SUP is not None:
        with contextlib.suppress(Exception):
            _SUP.shutdown()