# -*- coding: utf-8 -*-
"""gateway-ui worker 入口（CONTRACT §3/§4/§7）—— side-env 独立 Qt 进程。

生产 spawn 命令行：<env_python> -I -X utf8=1 <plugin>/worker/main.py --meta <meta_path>
python -I 隐含 -P：脚本目录不进 sys.path（桌宠落地事故根因），顶部显式 insert，
之后 worker 各模块平级导入（import pet_window）。worker 不 import 宿主侧模块。

生命周期纪律：
- main() 第一行（Qt import 之前）trace boot；每关键阶段打点；CRASH 全 traceback。
- 租约启动自检（A3 根除）：新鲜(<8s)+pid 活+非自己 → 无条件 self-exit，
  不区分 owner（旧版"同宿主陈旧租约自家接手"放行了双 worker）；陈旧 → 接手覆写。
- QApplication 建立后、tray.show() 前立即写一次租约；tick 每 1s 续租（带 spawn_token）。
- 命锁双线程：stdin readline()=="" → os._exit(0)；owner OpenProcess 失败(2s 轮询)
  → os._exit(0)。Windows select 不支持管道，用阻塞线程。
- 看门狗线程（A4 根除）：tick 每拍更新 LAST_TICK_TS；每 5s 检查，
  now-last_tick > 20s → trace("WATCHDOG: event loop wedged") + os._exit(3)。
- 左键单击托盘 = toggle 余额气泡（托盘左键改绑，与桌宠左键同款；桌宠可见锚
  头顶、不可见锚托盘图标；纯运行时，绝不写 enable-pet，A2 根除）；
  enable-pet=false 且 balance.enabled=false 时左键无操作。开关权只属于菜单勾选与作者手改 config。
- stderr 由宿主重定向到 worker_stderr.log，本进程不管。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

# python -I 隐含 -P：脚本目录不进 sys.path。显式 insert 后平级导入。
sys.path.insert(0, str(Path(__file__).resolve().parent))

META = {}
STATE = {"data": None, "ts": 0.0}
LAST = {"light": None, "pet_state": None, "verbose": False}
PET = {"win": None, "cfg": {}, "cfg_mtime": 0.0, "menu": None,
       "hidden": False, "last_enable": None,
       # 余额气泡：余额气泡（balance.enabled 门控，左键单击本体 toggle）
       "bubble": None, "balance_cfg": {},
       # 审批弹窗：ApprovalPopup（approval.popup_enabled 门控，宿主 req 驱动）
       "approval_mgr": None, "approval_cfg": {}}
LIFECYCLE_PENDING = {"until": 0.0}
OWNER_ALIVE = True

# 事件反应收口：三事件反应管线（宿主 notice → NoticeTracker → 桌宠动作+托盘闪动）。
# notices 纯 stdlib 无 Qt，顶部导入安全；TRAY_FLASH 在 _main_inner 建好托盘后赋值。
from notices import NoticeTracker
NOTICE_TRACKER = NoticeTracker()
TRAY_FLASH = None

STATE_TTL = 8.0          # state 文件 mtime > 8s → 灯灰（失联）
WRITER_LEASE_TTL = 4.0   # writer_lease.json mtime < 4s 才接受（写者租约）
LEASE_FRESH_S = 8.0      # tray_lease 新鲜阈值（A3）
LEASE_TOUCH_S = 5.0      # 硬件友好写盘：心跳=os.utime 间隔（<LEASE_FRESH_S）
LEASE_VERIFY_S = 0.05    # N20：写租约后回读校验前的落盘安定窗（收窄并发覆盖窗口）
_LAST_LEASE_TOUCH = 0.0
_LAST_BEAT = 0.0
WATCHDOG_TIMEOUT_S = 20.0
WATCHDOG_PERIOD_S = 5.0
LAST_TICK_TS = time.time()

_LAST_PET_ERR = 0.0
_LAST_SHEET_WARN = 0.0
_LAST_SYNC_ERR = 0.0
# N19：pet_error.log 自带大小闸（与 debug.log/pet_debug.log 同款）——异常通道
# 只限频不滚动的老写法会把盘写爆。
PET_ERROR_MAX_BYTES = 1_000_000
PET_ERROR_KEEP_LINES = 2000

tray = None  # _main_inner 里 import 并接线（Qt import 必须在 boot trace 之后）
TRAY_ICON = None  # 托盘左键改绑：桌宠不可见时气泡锚托盘图标几何


# ------------------------------------------------------------- logging ----
def trace(msg: str) -> None:
    """worker_trace.log 常开全阶段打点：盘上日志是唯一证词（A4 取证通道）。"""
    try:
        bus = META.get("bus_dir") or "."
        with open(Path(bus) / "worker_trace.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def vlog(msg: str) -> None:
    """verbose 从 state.json.verbose 读；true 时 WK 前缀写 bus/debug.log，
    false 零写入。桌宠物理 PET 前缀另写 pet_debug.log（pet_window 侧）。"""
    try:
        if not LAST["verbose"]:
            return
        f = Path(META["bus_dir"]) / "debug.log"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} WK {msg}\n")
    except Exception:
        pass


def pet_error_log(msg: str) -> None:
    """pet_error.log 追加（桌宠/设置窗口异常通道取证）：>1MB 截断保留尾部
    PET_ERROR_KEEP_LINES 行（tmp+replace 原子换，N19：老写法无上限会写爆盘）。"""
    try:
        f = Path(META["bus_dir"]) / "pet_error.log"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + str(msg).rstrip() + "\n")
        if f.stat().st_size > PET_ERROR_MAX_BYTES:
            keep = f.read_text(encoding="utf-8", errors="replace").splitlines()[
                -PET_ERROR_KEEP_LINES:]
            tmp = f.with_name(f"pet_error.log.{os.getpid()}.tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, f)
    except Exception:
        pass


def read_meta() -> dict:
    ap = sys.argv[sys.argv.index("--meta") + 1] if "--meta" in sys.argv else None
    return json.loads(Path(ap).read_text(encoding="utf-8")) if ap else {}


# --------------------------------------------------------------- lease ----
def pid_alive(pid: int) -> bool:
    """OpenProcess 0x1000 (PROCESS_QUERY_LIMITED_INFORMATION) 判活。"""
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def lease_decision(lease, now: float, self_pid: int, alive_fn, mtime: float = 0.0) -> str:
    """单实例协议判定（A3，纯函数可测）：
    - 无租约/无 pid → "pass"（直接启动）
    - pid == 自己 → "pass"（放行）
    - 新鲜(now-心跳 < 8s) 且 pid 存活 且非自己 → "yield"（无条件 self-exit，
      不区分 owner——旧版同宿主规则放行了双 worker）
    - 陈旧(>=8s) 或 pid 已死 → "takeover"（持有者已死/卡死，接手覆写）
    心跳源现优先文件 mtime（内容静态无 ts）；mtime 缺省回落旧版 ts 字段（过渡兼容）。"""
    if not lease:
        return "pass"
    try:
        pid = int(lease.get("pid") or 0)
    except (TypeError, ValueError):
        return "pass"
    if not pid or pid == self_pid:
        return "pass"
    if mtime:
        ts = float(mtime)
    else:
        try:
            ts = float(lease.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
    if now - ts >= LEASE_FRESH_S:
        return "takeover"
    if alive_fn(pid):
        return "yield"
    return "takeover"


def lease_self_check() -> str:
    """启动自检：读 tray_lease.json 并判定。现心跳看文件 mtime（内容静态）。"""
    try:
        lp = Path(META["bus_dir"]) / "tray_lease.json"
        lease = json.loads(lp.read_text(encoding="utf-8")) if lp.is_file() else None
        mt = lp.stat().st_mtime if lp.is_file() else 0.0
    except Exception:
        lease, mt = None, 0.0
    return lease_decision(lease, time.time(), os.getpid(), pid_alive, mtime=mt)


def write_lease() -> bool:
    """spawn 时写一次静态租约（pid/owner_pid/spawn_token，**无 ts**，硬件友好写盘：
    心跳改由 touch_lease 的 os.utime 承担，JSON 不再每秒重写）。

    N20：lease_self_check 的判定与这里的写入之间隔着 Qt 初始化，check-then-write
    没有原子性——两个实例可能都判 pass 再各写一次。修法：tmp 名带 pid 的原子
    replace + 回读校验 pid；被对方覆盖返回 False，调用方优雅退出。
    写盘异常仍返回 True（fail-open：不把磁盘问题升级成"退让"）。"""
    global _LAST_LEASE_TOUCH
    try:
        p = Path(META["bus_dir"]) / "tray_lease.json"
        tmp = p.with_name(f"tray_lease.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "owner_pid": int(META.get("gateway_pid") or 0),
            "spawn_token": META.get("spawn_token", ""),
        }), encoding="utf-8")
        os.replace(tmp, p)
        _LAST_LEASE_TOUCH = time.time()
        time.sleep(LEASE_VERIFY_S)     # 让并发写落盘：收窄同步竞态窗口
        back = json.loads(p.read_text(encoding="utf-8"))
        return int(back.get("pid") or 0) == os.getpid()
    except Exception:
        return True


def touch_lease(now=None) -> None:
    """心跳续租（硬件友好写盘）：os.utime 只动 inode mtime，不写数据页/不做
    tmp+rename 目录日志；每 LEASE_TOUCH_S 一次。内容静态只写一次（write_lease）。"""
    global _LAST_LEASE_TOUCH
    now = time.time() if now is None else now
    if now - _LAST_LEASE_TOUCH < LEASE_TOUCH_S:
        return
    _LAST_LEASE_TOUCH = now
    try:
        os.utime(Path(META["bus_dir"]) / "tray_lease.json", (now, now))
    except Exception:
        pass


# ------------------------------------------------------------ watchdog ----
def is_wedged(now=None) -> bool:
    """卡死判定（A4，纯谓词可测）：>20s 无 tick。"""
    now = time.time() if now is None else now
    return now - LAST_TICK_TS > WATCHDOG_TIMEOUT_S


def watchdog_check(now=None) -> bool:
    """卡死自毁：trace + os._exit(3)，supervisor 心跳判活后重拉（A4 根除）。"""
    if is_wedged(now):
        trace("WATCHDOG: event loop wedged")
        os._exit(3)
        return True
    return False


def _watchdog_loop():
    while True:
        time.sleep(WATCHDOG_PERIOD_S)
        watchdog_check()


# ------------------------------------------------------------- pet cfg ----
# 设置界面 F7：config.yaml 唯一事实源，worker 绝不打开它。写=发请求文件→宿主官方
# writer 落盘→回执；读=宿主 settings_view.json 快照（CONTRACT §6）。
SETTINGS = {"view_mtime": 0.0, "view_sig": None, "ack_mtime": 0.0, "ack_sig": None,
            "pending": {}, "last_ack": None}


def pet_cfg() -> dict:
    return PET["cfg"]


def bus_token() -> str:
    """F5：settings 总线鉴权 token —— 宿主 register 时写 <bus>/.bus_token，本进程
    每次发请求现读现带（宿主重 register 会换 token，长活 worker 必须跟新，不缓存）。
    文件缺失返回空串：宿主侧降级放行并在 ack.token 标 "missing"（兼容旧宿主）。"""
    try:
        p = Path(META["bus_dir"]) / ".bus_token"
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def request_setting(key, value, source="petmenu"):
    """发配置修改请求（settings_req.json）→ 宿主官方 writer 落盘。
    乐观更新内存 cfg（菜单即时响应），记 pending 等回执确认/回滚。
    返回请求 id。worker 绝不自己写 config.yaml。"""
    rid = uuid.uuid4().hex[:8]
    if key.startswith("pet."):
        PET["cfg"][key[4:]] = value       # 乐观更新（pet 菜单/窗口即时生效）
    SETTINGS["pending"][rid] = {"key": key, "value": value, "ts": time.time()}
    try:
        p = Path(META["bus_dir"]) / "settings_req.json"
        tmp = p.with_name(f"settings_req.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"id": rid, "ts": time.time(), "key": key,
                                   "value": value, "source": source,
                                   # F5：总线鉴权（宿主校验，伪造请求会被拒）
                                   "token": bus_token()},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        vlog(f"request_setting {key}={value!r} id={rid}")
    except Exception as exc:
        trace(f"request_setting write failed: {exc}")
    return rid


def set_pet_cfg(key, value):
    """菜单/桌宠改配置回调（设置界面改道）：pet 键 → request_setting。
    左键显示/隐藏绝不走这里（A2）。"""
    return request_setting("pet." + key, value, source="petmenu")


def read_ack():
    """读 settings_ack.json（mtime 驱动）：匹配 pending → ok 确认 / fail 留证。
    回执供设置窗口做"已保存/保存失败"反馈（SETTINGS['last_ack']）。"""
    try:
        p = Path(META["bus_dir"]) / "settings_ack.json"
        if not p.is_file():
            return
        mt = p.stat().st_mtime
        raw = p.read_text(encoding="utf-8")
        # mtime + 内容签名双判：NTFS mtime 精度有限，快速连续写入可能同 tick，
        # 内容变了仍须处理（防回执被去重吞掉）。
        if mt == SETTINGS["ack_mtime"] and raw == SETTINGS["ack_sig"]:
            return
        SETTINGS["ack_mtime"] = mt
        SETTINGS["ack_sig"] = raw
        ack = json.loads(raw)
        SETTINGS["last_ack"] = ack
        rid = str(ack.get("id") or "")
        pend = SETTINGS["pending"].pop(rid, None)
        if pend is None:
            return
        if ack.get("ok"):
            vlog(f"ack ok {pend['key']}={ack.get('value')!r}")
        else:
            vlog(f"ack FAIL {pend['key']}: {ack.get('error')}")
            trace(f"setting rejected {pend['key']}: {ack.get('error')}")
    except Exception:
        pass


def reload_settings():
    """读 settings_view.json 快照（mtime 驱动）= pet 运行时真值（config 唯一事实源）。
    快照缺失（首启）回落 meta.pet。不再读 pet_cfg.json（已退役）。"""
    try:
        p = Path(META["bus_dir"]) / "settings_view.json"
        if p.is_file():
            mt = p.stat().st_mtime
            raw = p.read_text(encoding="utf-8")
            if mt != SETTINGS["view_mtime"] or raw != SETTINGS["view_sig"]:
                SETTINGS["view_mtime"] = mt
                SETTINGS["view_sig"] = raw
                view = json.loads(raw)
                pet = view.get("pet")
                if isinstance(pet, dict):
                    PET["cfg"] = dict(pet)
                # 余额气泡：balance 块进 worker（缺块=功能关，A1 缺键不落盘语义由宿主保证）
                bal = view.get("balance")
                PET["balance_cfg"] = dict(bal) if isinstance(bal, dict) else {}
                # 审批弹窗：approval 块进 worker（缺块=popup_enabled false，门控关）
                appr = view.get("approval")
                PET["approval_cfg"] = dict(appr) if isinstance(appr, dict) else {}
        elif not PET["cfg"]:
            PET["cfg"] = dict(META.get("pet") or {})
            vlog(f"settings_view absent -> seeded from tray_meta: {PET['cfg']}")
    except Exception:
        pass


def on_tray_activated(reason, menu=None):
    """托盘点击路由：左键=toggle 余额气泡（托盘左键改绑，与桌宠左键同款，
    纯运行时绝不写 enable-pet，A2 根除）；右键=弹 Fluent 菜单（左下角对准鼠标）。
    独立函数便于 A2 断言测试。"""
    from PySide6.QtWidgets import QSystemTrayIcon
    if reason == QSystemTrayIcon.Trigger:
        _toggle_bubble()
    elif reason == QSystemTrayIcon.Context:
        if menu is not None and tray is not None:
            tray.popup_bottom_left(menu)


def toggle_pet_visibility() -> bool:
    """显示/隐藏浮窗（纯运行时，绝不写 enable-pet）。托盘左键改绑起托盘左键改绑
    气泡 toggle，本函数保留为独立入口（测试/未来菜单复用）。返回是否执行了切换。"""
    win = PET.get("win")
    if win is None:
        return False
    if win.isVisible():
        win.hide()
        PET["hidden"] = True
    else:
        win.show()
        PET["hidden"] = False
    return True


def ensure_pet_menu():
    """托盘"桌宠"槽位与桌宠右键共享唯一 PetMenu（惰性建，parent=None）。"""
    if PET.get("menu") is None:
        try:
            from pet_menu import PetMenu
            PET["menu"] = PetMenu(pet_cfg, set_pet_cfg,
                                  META.get("hermes_home", ""), on_change=_reload_win)
        except Exception as exc:
            PET["menu"] = None
            trace(f"PetMenu create failed: {type(exc).__name__}: {exc}")
    return PET["menu"]


# ------------------------------------------------------- settings window ----
# 设置界面 F7：设置窗口单实例（worker 进程内，与托盘/桌宠共用 QApplication）。
SETTINGS_WIN = {"win": None}


def open_settings():
    """托盘"设置"项：打开 SettingsWindow，已开则激活置前（单实例）。
    惰性 import settings_window（并行任务产出）；构造失败留证不炸托盘。"""
    win = SETTINGS_WIN.get("win")
    try:
        if win is not None and win.isVisible():
            win.raise_()
            win.activateWindow()
            return win
        from settings_window import SettingsWindow
        win = SettingsWindow(META, STATE, request_setting,
                             home=META.get("hermes_home", ""))
        # 审批弹窗：样例窗口钩子（设置页「弹出样例窗口」按钮 → 纯预览，A2）
        win.show_sample_approval = _show_sample_approval
        SETTINGS_WIN["win"] = win
        win.show()
        win.raise_()
        win.activateWindow()
        trace("settings window opened")
        return win
    except Exception as exc:
        trace(f"SettingsWindow create failed: {type(exc).__name__}: {exc}")
        pet_error_log("SettingsWindow: " + traceback.format_exc())
        return None


def _reload_win():
    win = PET.get("win")
    if win is not None:
        try:
            win.reload()
        except Exception:
            pass


def _notice_reactions():
    """事件反应收口：三事件反应（完成/失败/等人类输入）。
    NoticeTracker 判新 notice（seq 去重/首轮不回放/过期忽略）→ 托盘闪动 +
    桌宠 play_reaction。开关 pet.event_reactions（缺省 true，作者点名）。
    stopped（用户 /stop）只闪托盘不做桌宠动作——主动停止不是值得庆祝/紧张的事。
    整段 try 包裹：反应失败绝不拖垮 tick。"""
    if TRAY_FLASH is None:
        return
    if not bool(PET["cfg"].get("event_reactions", True)):
        return
    n = NOTICE_TRACKER.feed(STATE["data"] or {})
    if not n:
        return
    vlog(f"notice reaction kind={n['kind']} seq={n['seq']} platform={n['platform']}")
    try:
        TRAY_FLASH.start_flash(n["kind"])
    except Exception as exc:
        vlog(f"tray flash RAISED: {exc}")
    if n["kind"] == "stopped":
        return
    win = PET.get("win")
    if win is not None:
        try:
            win.play_reaction(n["kind"])
        except Exception as exc:
            vlog(f"play_reaction RAISED: {exc}")


def _balance_enabled() -> bool:
    """余额气泡：余额气泡门控。balance.enabled 缺省 false（DIY 修改类默认关）。"""
    return bool(PET.get("balance_cfg", {}).get("enabled", False))


def _ensure_bubble():
    """惰性建 BalanceBubble（单例，worker 进程内）。失败留证不炸 tick。"""
    if PET.get("bubble") is not None:
        return PET["bubble"]
    try:
        from balance_bubble import BalanceBubble
        b = BalanceBubble(META, META["bus_dir"])
        PET["bubble"] = b
        vlog("BalanceBubble created")
        return b
    except Exception as exc:
        trace(f"BalanceBubble create failed: {type(exc).__name__}: {exc}")
        return None


def _pet_rect():
    """当前桌宠窗口几何（QRect）；无窗口返回 None。"""
    win = PET.get("win")
    if win is None:
        return None
    from PySide6.QtCore import QRect
    return QRect(win.x(), win.y(), win.width(), win.height())


def _bubble_anchor():
    """气泡锚定矩形：桌宠可见=头顶（既有语义）；桌宠不可见（enable-pet off 或
    隐藏）=托盘图标几何（托盘左键改绑：托盘左键也要能弹泡）。都无返回 None。"""
    win = PET.get("win")
    if win is not None and win.isVisible():
        return _pet_rect()
    icon = TRAY_ICON
    if icon is not None:
        try:
            r = icon.geometry()
            if r is not None and not r.isNull() and r.width() > 0:
                return r
        except Exception:
            pass
    return None


def _toggle_bubble():
    """左键单击桌宠本体 / 托盘左键（托盘左键改绑同款）= toggle 余额气泡。
    balance.enabled=false 时无操作（入口禁用）。气泡开着再点=收起。
    纯运行时，绝不发 settings_req（A2：点击≠指令）。"""
    if not _balance_enabled():
        return
    b = _ensure_bubble()
    if b is None:
        return
    try:
        if b.isVisible():
            b.hide_bubble()
            _set_bubble_focus(False)   # 收起即恢复游走（即时，不等下一拍）
        else:
            r = _bubble_anchor()
            if r is not None:
                b.show_for(r)
                _set_bubble_focus(True)   # 弹出即钉住演思考动画（即时）
    except Exception as exc:
        vlog(f"bubble toggle RAISED: {exc}")


def _on_pet_click():
    """桌宠本体左键单击回调（pet_window 位移阈值内）= 托盘同款气泡 toggle。"""
    _toggle_bubble()


def _set_bubble_focus(on: bool):
    """气泡聚焦增强：气泡聚焦态 → 桌宠掐断游走钉在原地演 review（同模型思考动画）。
    气泡开=聚焦，关/隐藏=恢复。纯运行时标志，绝不写配置（A2）。"""
    win = PET.get("win")
    if win is not None:
        try:
            win.bubble_focus = bool(on)
        except Exception:
            pass


def _sync_bubble():
    """每拍维护气泡：门控关/桌宠隐藏 → 必收；开着则刷新快照+跟随桌宠。
    气泡可见性同步驱动桌宠聚焦态（开=钉住演思考动画，关=恢复游走）。
    整段 try 包裹：气泡失败绝不拖垮 tick。"""
    b = PET.get("bubble")
    if b is None:
        _set_bubble_focus(False)
        return
    try:
        if not _balance_enabled():
            if b.isVisible():
                b.hide_bubble()
            _set_bubble_focus(False)
            return
        win = PET.get("win")
        pet_visible = win is not None and win.isVisible()
        if b.isVisible():
            # 桌宠不可见时锚托盘图标（托盘左键弹的泡不因桌宠隐藏被误收）
            r = _pet_rect() if pet_visible else _bubble_anchor()
            if r is None:
                b.hide_bubble()
                _set_bubble_focus(False)
                return
            b.refresh_view()
            b.follow(r)
            _set_bubble_focus(pet_visible)
        else:
            if pet_visible:
                b.refresh_view()   # 关着也预读快照，点开即有数据
            _set_bubble_focus(False)
    except Exception as exc:
        vlog(f"sync_bubble RAISED: {exc}")


# ------------------------------------------------------- approval popup ----
# 审批弹窗 R28：宿主 approval_req.json 驱动，approval.popup_enabled 门控。
# 点击注入 approval decision（写 approval_resp.json），零 settings_req（A2）。
def _approval_enabled() -> bool:
    """审批弹窗门控：approval.popup_enabled 缺省 false（DIY 修改类默认关）。"""
    return bool(PET.get("approval_cfg", {}).get("popup_enabled", False))


def _ensure_approval_mgr():
    """惰性建 ApprovalPopupManager（单例）。失败留证不炸 tick。"""
    if PET.get("approval_mgr") is not None:
        return PET["approval_mgr"]
    try:
        from approval_popup import ApprovalPopupManager
        mgr = ApprovalPopupManager(META, META["bus_dir"],
                                   get_cfg=lambda: PET.get("approval_cfg", {}))
        PET["approval_mgr"] = mgr
        vlog("ApprovalPopupManager created")
        return mgr
    except Exception as exc:
        trace(f"ApprovalPopupManager create failed: {type(exc).__name__}: {exc}")
        return None


def _sync_approval():
    """每拍维护审批弹窗：读 req/withdrawn、门控、堆叠、倒计时。失败不拖垮 tick。"""
    mgr = _ensure_approval_mgr()
    if mgr is None:
        return
    try:
        mgr.sync()
    except Exception as exc:
        vlog(f"sync_approval RAISED: {exc}")


def _show_sample_approval():
    """设置页「弹出样例窗口」回调：纯预览，不注入真信号（A2）。"""
    mgr = _ensure_approval_mgr()
    if mgr is None:
        return None
    try:
        return mgr.show_sample()
    except Exception as exc:
        vlog(f"show_sample RAISED: {exc}")
        return None


def sync_pet(app):
    """按 pet 配置块维护浮窗：enable-pet 开=建/显，关=隐；配置变更热重载。
    构造/sheet 缺失异常通道全部 vlog+限频 pet_error.log 留证（播种对账，不静默）。"""
    global _LAST_PET_ERR, _LAST_SHEET_WARN
    cfg = PET["cfg"]
    want = bool(cfg.get("enable-pet", False))
    if PET["last_enable"] is not None and want != PET["last_enable"]:
        PET["hidden"] = False  # 菜单开关切换 → 重置左键隐藏态
    PET["last_enable"] = want
    win = PET["win"]
    if want and win is None:
        try:
            from pet_window import PetWindow
            win = PetWindow(META, META["bus_dir"], pet_cfg, set_pet_cfg,
                            pet_menu=ensure_pet_menu(), on_click=_on_pet_click)
            PET["win"] = win
            PET["hidden"] = False
            vlog(f"PetWindow created slug={win.slug!r} sheet={'OK' if win.sheet else 'NONE'}")
        except Exception as exc:
            PET["win"] = None
            now2 = time.time()
            if now2 - _LAST_PET_ERR > 10.0:  # 限频留证，不再静默
                _LAST_PET_ERR = now2
                vlog(f"PetWindow CREATE FAILED: {type(exc).__name__}: {exc}")
                pet_error_log(traceback.format_exc())
            return
    if win is None:
        return
    if want:
        win.reload()
        if getattr(win, "sheet", None) is None and time.time() - _LAST_SHEET_WARN > 10:
            _LAST_SHEET_WARN = time.time()
            vlog(f"WARN: pet window alive but spritesheet MISSING "
                 f"(slug={PET['cfg'].get('slug')!r}) — 窗口透明不可见；"
                 f"检查 {META.get('hermes_home')}\\pets\\ 素材")
        ps = (STATE["data"] or {}).get("pet_state", "idle")
        at_rest = (STATE["data"] or {}).get("active_sessions", 0) == 0
        if ps != LAST["pet_state"]:
            vlog(f"pet host_state {LAST['pet_state']} -> {ps} (at_rest={at_rest})")
            LAST["pet_state"] = ps
        try:
            import pet_window
            pet_window.VERBOSE = LAST["verbose"]
        except Exception:
            pass
        win.set_host(ps, at_rest)
        if not win.isVisible() and not PET["hidden"]:
            win.show()
    elif win.isVisible():
        win.hide()


# ---------------------------------------------------------------- tick ----
def read_state(now: float):
    """读 state.json + 写者租约校验（写者租约规则）：state.writer_pid 必须匹配
    writer_lease.json（租约文件 mtime < 4s）才接受；不匹配=幽灵写者 → 丢弃+REJECT。"""
    try:
        p = Path(META["state_path"])
        if not (p.exists() and now - p.stat().st_mtime < STATE_TTL):
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        wp = data.get("writer_pid")
        ok = False
        try:
            lp = Path(META["bus_dir"]) / "writer_lease.json"
            if wp is not None and lp.is_file() and now - lp.stat().st_mtime < WRITER_LEASE_TTL:
                lease = json.loads(lp.read_text(encoding="utf-8"))
                ok = int(lease.get("pid") or 0) == int(wp)
        except Exception:
            ok = False
        if ok:
            STATE["data"] = data
            STATE["ts"] = now
        elif wp is not None and (STATE["data"] or {}).get("writer_pid") != wp:
            vlog(f"REJECT state writer_pid={wp} (lease mismatch, ghost writer?)")
    except Exception:
        pass


def tick(app, tray_icon, menu):
    global LAST_TICK_TS
    LAST_TICK_TS = time.time()  # 看门狗心跳（A4）：每拍更新，异常也算活着
    try:
        now = time.time()
        reload_settings()
        read_ack()
        read_state(now)
        touch_lease(now)   # 硬件友好写盘：mtime 心跳，JSON 静态不重写
        LAST["verbose"] = bool((STATE["data"] or {}).get("verbose"))
        light = tray.state_light()
        if light != LAST["light"]:
            vlog(f"light {LAST['light']} -> {light} "
                 f"(sessions={(STATE['data'] or {}).get('active_sessions')} "
                 f"src={(STATE['data'] or {}).get('active_source')})")
            LAST["light"] = light
        from PySide6.QtGui import QIcon
        # 事件反应：闪动期间 TrayFlash 自驱动图标（300ms QTimer），tick 不得覆盖，
        # 否则每秒一帧闪动被正常图标碾掉。flash 结束 stop() 自动恢复正常图标。
        if not (TRAY_FLASH is not None and TRAY_FLASH.active):
            tray_icon.setIcon(QIcon(tray.tray_pixmap(light)))
        tray_icon.setToolTip(tray.tooltip())
        amap = getattr(menu, "_actions_map", {})
        if "status" in amap:
            tier = ((STATE["data"] or {}).get("tray") or {}).get("tier", "暂无异常")
            amap["status"].setText(f"状态监控: {tier}")
        try:
            sync_pet(app)
        except Exception as exc:
            global _LAST_SYNC_ERR
            now3 = time.time()
            if now3 - _LAST_SYNC_ERR > 10.0:
                _LAST_SYNC_ERR = now3
                vlog(f"sync_pet RAISED: {type(exc).__name__}: {exc}")
        try:
            _notice_reactions()
        except Exception as exc:
            vlog(f"notice reactions RAISED: {exc}")
        try:
            _sync_bubble()   # 余额气泡：余额气泡门控/刷新/跟随（失败不拖垮 tick）
        except Exception as exc:
            vlog(f"sync_bubble RAISED: {exc}")
        try:
            _sync_approval()   # 审批弹窗：req 驱动/门控/堆叠（失败不拖垮 tick）
        except Exception as exc:
            vlog(f"sync_approval RAISED: {exc}")
        if LIFECYCLE_PENDING["until"] and now > LIFECYCLE_PENDING["until"]:
            app.quit()
        if not OWNER_ALIVE:
            app.quit()
    except Exception:
        pass  # tick 永不炸


# ---------------------------------------------------------------- main ----
def main():
    global META
    # 第一行（Qt import 之前）：boot trace。read_meta 纯 stdlib json，先取 bus_dir。
    META = read_meta()
    trace(f"boot pid={os.getpid()} argv={' '.join(sys.argv[1:3])[:80]}")
    try:
        _main_inner()
    except SystemExit as exc:
        trace(f"exit SystemExit({exc.code})")
    except BaseException:
        trace("CRASH:\n" + traceback.format_exc())
        raise


def _main_inner():
    global tray, TRAY_ICON
    trace("meta ok")
    decision = lease_self_check()
    trace(f"lease check -> {decision}")
    if decision == "yield":
        # A3：新鲜租约+活 pid+非自己 → 无条件 self-exit，不区分 owner。
        trace("lease held by live worker, unconditional self-exit")
        return
    if decision == "takeover":
        trace("stale/dead lease -> takeover")
    PET["cfg"] = dict(META.get("pet") or {})

    # Qt import 全部在 boot trace 之后（启动期卡死必须留证，A4）。
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QIcon, QGuiApplication
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    from qfluentwidgets import setTheme, Theme
    import tray as tray_mod
    tray = tray_mod
    tray.META = META
    tray.STATE = STATE

    # 事件反应高 DPI 修复（唯一启动逻辑改动）：小数缩放精确透传——Qt6 默认
    # Round 会把 150% 圆整成 2.0 再位图缩放（糊）；PassThrough 下 Windows
    # 不做位图缩放，Qt 按真实 dpr 自处理。必须在 QApplication 创建前设置。
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    trace("app created")
    try:
        setTheme(Theme.DARK)
    except Exception:
        pass
    # 启动后立即写一次租约（QApplication 建立后、tray.show() 前）。
    # N20：写后回读校验——租约被判给别的实例就优雅退出（不弹窗、不报错、rc=0）。
    if not write_lease():
        trace("lease taken by another instance after write -> graceful exit")
        return
    tray_icon = QSystemTrayIcon(QIcon(tray.tray_pixmap("green")))
    TRAY_ICON = tray_icon   # 托盘左键改绑：桌宠不可见时气泡锚托盘图标
    # 事件反应收口：托盘闪动控制器（light_fn 用当前灯色，闪动帧=灯色+事件高亮）。
    global TRAY_FLASH
    TRAY_FLASH = tray.TrayFlash(tray_icon, light_fn=tray.state_light)
    menu = tray.build_fluent_menu(app, tray_icon, pet_menu=ensure_pet_menu(),
                                  on_settings=open_settings)

    tray_icon.activated.connect(lambda reason: on_tray_activated(reason, menu))
    tray_icon.setToolTip("gateway-ui · 启动中")
    tray_icon.show()
    trace("tray shown")

    # 命锁双线程：Windows select 不支持 stdin 管道，阻塞线程 + os._exit。
    owner = int(META.get("gateway_pid") or 0)

    def _die(reason):
        trace(f"lifeline: {reason} -> exit")
        os._exit(0)

    def _watch_stdin():
        try:
            while True:
                if sys.stdin.readline() == "":
                    _die("stdin EOF")
        except Exception:
            _die("stdin error")

    def _watch_owner():
        while True:
            time.sleep(2.0)
            if owner and owner != os.getpid():
                if not pid_alive(owner):
                    _die("owner gone")

    if owner:
        threading.Thread(target=_watch_owner, daemon=True).start()
    threading.Thread(target=_watch_stdin, daemon=True).start()
    # 看门狗（A4）：事件循环卡死自毁，supervisor 心跳判活后重拉。
    threading.Thread(target=_watchdog_loop, daemon=True).start()

    timer = QTimer()
    timer.timeout.connect(lambda: tick(app, tray_icon, menu))
    timer.start(1000)
    trace("tick start")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
