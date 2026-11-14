# -*- coding: utf-8 -*-
"""test_worker_lease.py — 单实例租约协议（A3）+ 左键不写配置（A2）+ 看门狗（A4）。

side-env python 跑（offscreen Qt）。全部用 scratch 总线目录，绝不碰真实总线。
- 租约自检三态：新鲜他者→让位(yield)、陈旧→接手(takeover)、自己→放行(pass)。
- 左键单击托盘=显示/隐藏（纯运行时），fake cfg setter 断言写入次数为 0。
- 看门狗谓词：>20s 无 tick 判卡死（只测谓词，不触发 os._exit）。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication, QSystemTrayIcon
app = QApplication([])

import main as wk

from _env import scratch  # noqa: E402
BUS = scratch("gui22-lease")
BUS.mkdir(parents=True, exist_ok=True)
wk.META = {"bus_dir": str(BUS), "gateway_pid": os.getppid(),
           "spawn_token": "testtoken123", "state_path": str(BUS / "state.json")}

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

NOW = time.time()
ME = os.getpid()
OTHER_LIVE = os.getppid()   # 父进程（shell）必然存活，且 != 自己；只读 OpenProcess

# ============ A3：租约自检三态（纯函数 lease_decision，fake alive）============
fresh_other = {"pid": 999999, "owner_pid": 1, "spawn_token": "x", "ts": NOW - 1}
ck("fresh live other -> yield",
   wk.lease_decision(fresh_other, NOW, ME, lambda p: True) == "yield")
ck("fresh live other -> yield EVEN same owner (A3: no owner distinction)",
   wk.lease_decision(dict(fresh_other, owner_pid=wk.META["gateway_pid"]),
                     NOW, ME, lambda p: True) == "yield")
stale = {"pid": 999999, "owner_pid": 1, "spawn_token": "x", "ts": NOW - 30}
ck("stale (>=8s) -> takeover", wk.lease_decision(stale, NOW, ME, lambda p: True) == "takeover")
ck("fresh but dead pid -> takeover",
   wk.lease_decision(fresh_other, NOW, ME, lambda p: False) == "takeover")
ck("self pid -> pass", wk.lease_decision(dict(fresh_other, pid=ME), NOW, ME, lambda p: True) == "pass")
ck("no lease -> pass", wk.lease_decision(None, NOW, ME, lambda p: True) == "pass")
ck("empty pid -> pass", wk.lease_decision({"pid": 0, "ts": NOW}, NOW, ME, lambda p: True) == "pass")
ck("boundary exactly 8s -> takeover (>=)",
   wk.lease_decision({"pid": 999999, "ts": NOW - 8.0}, NOW, ME, lambda p: True) == "takeover")
ck("just under 8s fresh live -> yield",
   wk.lease_decision({"pid": 999999, "ts": NOW - 7.9}, NOW, ME, lambda p: True) == "yield")

# ============ A3：真实 lease_self_check 读文件三态 ============
lp = BUS / "tray_lease.json"
lp.write_text(json.dumps({"pid": ME, "owner_pid": 1, "spawn_token": "x", "ts": NOW}), encoding="utf-8")
ck("self_check: own lease -> pass", wk.lease_self_check() == "pass")
lp.write_text(json.dumps({"pid": 999999, "owner_pid": 1, "spawn_token": "x"}), encoding="utf-8")
os.utime(lp, (NOW - 60, NOW - 60))   # 硬件友好写盘：心跳看 mtime，无 ts 字段
ck("self_check: stale lease -> takeover", wk.lease_self_check() == "takeover")
lp.write_text(json.dumps({"pid": OTHER_LIVE, "owner_pid": 1, "spawn_token": "x"}), encoding="utf-8")
os.utime(lp, (time.time(), time.time()))
ck("self_check: fresh live other (parent pid) -> yield", wk.lease_self_check() == "yield")
lp.unlink()
ck("self_check: no lease file -> pass", wk.lease_self_check() == "pass")

# ============ 续租写盘：静态 pid/owner_pid/spawn_token，无 ts（硬件友好写盘）====
wk.write_lease()
d = json.loads(lp.read_text(encoding="utf-8"))
ck("write_lease pid==self", d["pid"] == ME)
ck("write_lease carries spawn_token", d["spawn_token"] == "testtoken123")
ck("write_lease owner_pid from meta", d["owner_pid"] == wk.META["gateway_pid"])
ck("write_lease static no ts", "ts" not in d)
ck("write_lease mtime fresh", abs(lp.stat().st_mtime - time.time()) < 5)
# 内容不重写：inode mtime 手动设旧后 touch_lease 只动 mtime 不动内容
raw_before = lp.read_text(encoding="utf-8")
os.utime(lp, (time.time() - 30, time.time() - 30))
wk._LAST_LEASE_TOUCH = 0.0
wk.touch_lease()
ck("touch_lease refreshes mtime", abs(lp.stat().st_mtime - time.time()) < 5)
ck("touch_lease content unchanged", lp.read_text(encoding="utf-8") == raw_before)
# 节流：5s 内再调 touch_lease 不 utime（mtime 保持被设旧的值）
wk._LAST_LEASE_TOUCH = time.time()
old_mt = time.time() - 30
os.utime(lp, (old_mt, old_mt))
wk.touch_lease()
ck("touch_lease throttled within 5s", abs(lp.stat().st_mtime - old_mt) < 0.01)
# mtime 优先于 ts 的判定（过渡兼容反向验证）
ck("lease_decision mtime wins over ts",
   wk.lease_decision({"pid": 999999, "ts": NOW - 60}, NOW, ME, lambda p: True, mtime=NOW - 1) == "yield")
ck("lease_decision stale mtime beats fresh ts",
   wk.lease_decision({"pid": 999999, "ts": NOW}, NOW, ME, lambda p: True, mtime=NOW - 60) == "takeover")

# ============ A4：看门狗谓词（只测 is_wedged，不触发 os._exit）============
wk.LAST_TICK_TS = time.time()
ck("watchdog: fresh tick not wedged", wk.is_wedged() is False)
wk.LAST_TICK_TS = time.time() - 25
ck("watchdog: >20s no tick -> wedged", wk.is_wedged() is True)
wk.LAST_TICK_TS = time.time() - 19
ck("watchdog: 19s no tick -> not yet", wk.is_wedged() is False)
wk.LAST_TICK_TS = time.time()  # 复位

# ============ A2：左键=气泡 toggle（托盘左键改绑），绝不写 enable-pet ============
class FakeWin:
    def __init__(self):
        self._vis = True
        self.bubble_focus = False
    def isVisible(self):
        return self._vis
    def show(self):
        self._vis = True
    def hide(self):
        self._vis = False
    def x(self): return 500
    def y(self): return 400
    def width(self): return 100
    def height(self): return 100

cfg_writes = []
_orig_setter = wk.set_pet_cfg
def counting_setter(key, value):
    cfg_writes.append((key, value))
    return _orig_setter(key, value)
wk.set_pet_cfg = counting_setter

fw = FakeWin()
wk.PET["win"] = fw
wk.PET["cfg"] = {"enable-pet": True, "slug": "fairy", "scale": 0.7, "roam": True, "topmost": True}
wk.PET["hidden"] = False
before_enable = wk.PET["cfg"]["enable-pet"]

wk.PET["balance_cfg"] = {"enabled": True}   # 托盘左键改绑：左键改绑气泡，开门控
from PySide6.QtCore import QRect as _QRect
class _FakeTray:
    def geometry(self): return _QRect(1800, 1000, 24, 24)
wk.TRAY_ICON = _FakeTray()
wk.on_tray_activated(QSystemTrayIcon.Trigger)   # 左键：弹泡（不再动桌宠显隐）
ck("A2: left-click keeps window visible", fw.isVisible() is True)
_bub = wk.PET.get("bubble")
# offscreen 下 show() 异步映射，isVisible 不即时；用同步的 _timer 活跃判"已弹出"
ck("tray left-click opens bubble", _bub is not None and _bub._timer.isActive())
wk.on_tray_activated(QSystemTrayIcon.Trigger)   # 左键：收泡
ck("tray left-click closes bubble", not _bub._timer.isActive())
ck("A2: left-click writes config ZERO times", len(cfg_writes) == 0)
ck("A2: enable-pet unchanged by left-click", wk.PET["cfg"]["enable-pet"] == before_enable)
wk.PET["bubble"].hide_bubble()
wk.PET["balance_cfg"] = {}

# enable-pet=false 无窗口 → 左键无操作
wk.PET["win"] = None
ck("A2: no window -> left-click no-op returns False", wk.toggle_pet_visibility() is False)
ck("A2: still zero config writes", len(cfg_writes) == 0)
wk.set_pet_cfg = _orig_setter

# ============ 写者租约校验（写者租约）：幽灵写者丢弃 ============
wk.STATE["data"] = None
wk.STATE["ts"] = 0.0
sp = BUS / "state.json"
wl = BUS / "writer_lease.json"
# 幽灵：state.writer_pid 与 writer_lease.pid 不匹配
sp.write_text(json.dumps({"writer_pid": 111, "pet_state": "busy", "tray": {"light": "red"}}), encoding="utf-8")
wl.write_text(json.dumps({"pid": 222, "ts": time.time()}), encoding="utf-8")
wk.read_state(time.time())
ck("ghost writer rejected (state not accepted)", wk.STATE["data"] is None)
# 合法：pid 匹配 + 租约新鲜
wl.write_text(json.dumps({"pid": 111, "ts": time.time()}), encoding="utf-8")
wk.read_state(time.time())
ck("matching writer accepted", (wk.STATE["data"] or {}).get("writer_pid") == 111)

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
