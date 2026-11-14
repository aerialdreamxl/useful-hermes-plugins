# test_bubble_wire.py — 余额气泡收口接缝测试（side-env offscreen）。
# 验证 main.py 气泡接线：门控/toggle/跟随/隐藏必收/零 settings_req/balance 块进 worker。
# 纪律：只用 scratch/gui25-wire 总线目录，绝不碰真实总线/config。
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui25-wire")
BUS.mkdir(parents=True, exist_ok=True)

import main as m  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

PASS = FAIL = 0
def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok", name)
    else:
        FAIL += 1
        print("FAIL", name)


class FakeBubble:
    def __init__(self):
        self.visible = False
        self.calls = []
    def isVisible(self):
        return self.visible
    def show_for(self, r):
        self.visible = True
        self.calls.append(("show_for", r))
    def hide_bubble(self):
        self.visible = False
        self.calls.append(("hide",))
    def refresh_view(self):
        self.calls.append(("refresh",))
        return False
    def follow(self, r):
        self.calls.append(("follow", r))


class FakeWin:
    def __init__(self, vis=True):
        self._vis = vis
        self.bubble_focus = False
    def isVisible(self):
        return self._vis
    def x(self): return 500
    def y(self): return 400
    def width(self): return 100
    def height(self): return 100


class FakeTray:
    def __init__(self, rect=None):
        self._r = rect if rect is not None else QRect(1800, 1000, 24, 24)
    def geometry(self):
        return self._r


def reset(enabled=None, win=True, bubble=True):
    m.META = {"bus_dir": str(BUS), "hermes_home": str(BUS)}
    m.PET["bubble"] = FakeBubble() if bubble else None
    m.PET["win"] = FakeWin() if win else None
    m.PET["balance_cfg"] = {} if enabled is None else {"enabled": enabled}
    m.TRAY_ICON = FakeTray()
    for f in BUS.glob("settings_req.json*"):
        f.unlink()


# --- 1. 门控三态 ---
reset(enabled=None)
ok("gate-missing=false", m._balance_enabled() is False)
reset(enabled=False)
ok("gate-explicit-false", m._balance_enabled() is False)
reset(enabled=True)
ok("gate-explicit-true", m._balance_enabled() is True)

# --- 2. 单击 toggle ---
reset(enabled=False)
m._on_pet_click()
ok("click-disabled-noop", not m.PET["bubble"].visible and not m.PET["bubble"].calls)
reset(enabled=True)
m._on_pet_click()
ok("click-opens", m.PET["bubble"].visible)
ok("click-show-rect", m.PET["bubble"].calls[0][0] == "show_for"
   and m.PET["bubble"].calls[0][1] == QRect(500, 400, 100, 100))
m._on_pet_click()
ok("click-again-closes", not m.PET["bubble"].visible)
ok("click-zero-settings-req", not (BUS / "settings_req.json").exists())

# --- 3. 无桌宠窗口但有托盘几何 → 锚托盘弹泡（托盘左键改绑后语义）---
reset(enabled=True, win=False)
m._on_pet_click()
ok("click-no-win-anchor-tray", m.PET["bubble"].visible
   and m.PET["bubble"].calls[0][1] == QRect(1800, 1000, 24, 24))
# 无桌宠也无托盘几何 → 不弹不炸
reset(enabled=True, win=False)
m.TRAY_ICON = None
m._on_pet_click()
ok("click-no-anchor-noop", not m.PET["bubble"].visible)

# --- 4. _sync_bubble 门控/隐藏必收 ---
reset(enabled=True)
m.PET["bubble"].visible = True
m.PET["balance_cfg"] = {"enabled": False}
m._sync_bubble()
ok("sync-gate-off-hides", not m.PET["bubble"].visible)
reset(enabled=True)
m.PET["bubble"].visible = True
m.PET["win"]._vis = False
m._sync_bubble()
ok("sync-pet-hidden-follows-tray", m.PET["bubble"].visible
   and ("follow", QRect(1800, 1000, 24, 24)) in m.PET["bubble"].calls)
reset(enabled=True, win=False)
m.PET["bubble"].visible = True
m._sync_bubble()
ok("sync-no-win-follows-tray", m.PET["bubble"].visible)
# 无任何锚（无宠+无托盘几何）→ 必收
reset(enabled=True, win=False)
m.TRAY_ICON = None
m.PET["bubble"].visible = True
m._sync_bubble()
ok("sync-no-anchor-hides", not m.PET["bubble"].visible)

# --- 5. 开着时刷新+跟随 ---
reset(enabled=True)
m.PET["bubble"].visible = True
m._sync_bubble()
kinds = [c[0] for c in m.PET["bubble"].calls]
ok("sync-refresh-called", "refresh" in kinds)
ok("sync-follow-called", "follow" in kinds)
ok("sync-no-show-when-open", "show_for" not in kinds)
# 未开时也 refresh（预读快照，开泡即显不空白），但不 follow（省几何空转）
reset(enabled=True)
m._sync_bubble()
kinds2 = [c[0] for c in m.PET["bubble"].calls]
ok("sync-closed-preread", kinds2 == ["refresh"])

# --- 6. 气泡未建时 sync 安全 ---
reset(enabled=True, bubble=False)
m._sync_bubble()
ok("sync-no-bubble-safe", True)

# --- 7. reload_settings 收 balance 块 ---
reset(enabled=None)
view = {"pet": {"enable-pet": True, "slug": "fairy"},
        "balance": {"enabled": True, "refresh_s": 120},
        "top": {}, "explicit": {}}
vp = BUS / "settings_view.json"
vp.write_text(json.dumps(view), encoding="utf-8")
m.SETTINGS["view_mtime"] = 0.0
m.SETTINGS["view_sig"] = None
m.reload_settings()
ok("reload-balance-block", m.PET["balance_cfg"] == {"enabled": True, "refresh_s": 120})
ok("reload-pet-intact", m.PET["cfg"]["slug"] == "fairy")
# 缺 balance 块 → 清空（功能关）
view2 = {"pet": {"enable-pet": True}, "top": {}, "explicit": {}}
vp.write_text(json.dumps(view2), encoding="utf-8")
m.SETTINGS["view_mtime"] = 0.0
m.reload_settings()
ok("reload-no-balance-clears", m.PET["balance_cfg"] == {})

# --- 8. sync_pet 构造 PetWindow 带 on_click（真构造，真实 pets 只读） ---
m.META = {"bus_dir": str(BUS), "hermes_home": HOME,
          "pet": {"enable-pet": True, "slug": "fairy", "scale": 0.5}}
m.PET["win"] = None
m.PET["cfg"] = {"enable-pet": True, "slug": "fairy", "scale": 0.5,
                "roam": False, "topmost": True}
m.PET["last_enable"] = None
m.STATE["data"] = {"pet_state": "idle", "active_sessions": 0}
try:
    m.sync_pet(app)
    w = m.PET["win"]
    ok("syncpet-creates-win", w is not None)
    ok("syncpet-onclick-wired", w is not None and w.on_click == m._on_pet_click)
    if w is not None:
        w.hide()
except Exception as exc:
    ok("syncpet-creates-win", False)
    ok("syncpet-onclick-wired", False)
    print("  sync_pet exception:", exc)

# --- 9. 托盘左键改绑：托盘左键改绑同款气泡 toggle ---
from PySide6.QtWidgets import QSystemTrayIcon  # noqa: E402
reset(enabled=True)
m.on_tray_activated(QSystemTrayIcon.Trigger)
ok("tray-left-opens", m.PET["bubble"].visible)
ok("tray-left-anchor-pet", m.PET["bubble"].calls[0][1] == QRect(500, 400, 100, 100))
m.on_tray_activated(QSystemTrayIcon.Trigger)
ok("tray-left-closes", not m.PET["bubble"].visible)
ok("tray-left-zero-req", not (BUS / "settings_req.json").exists())
# 桌宠隐藏 → 锚托盘图标
reset(enabled=True, win=False)
m.on_tray_activated(QSystemTrayIcon.Trigger)
ok("tray-left-no-pet-anchor-tray", m.PET["bubble"].visible
   and m.PET["bubble"].calls[0][1] == QRect(1800, 1000, 24, 24))
# 桌宠隐藏时 sync 不收泡（锚托盘），且不聚焦（无宠可钉）
m.PET["win"] = FakeWin(vis=False)
m._sync_bubble()
ok("sync-pet-hidden-keeps-tray-bubble", m.PET["bubble"].visible)
ok("sync-pet-hidden-no-focus", m.PET["win"].bubble_focus is False)
# 门控关 → 托盘弹的泡也必收
m.PET["balance_cfg"] = {"enabled": False}
m._sync_bubble()
ok("sync-gate-off-hides-tray-bubble", not m.PET["bubble"].visible)
# 无托盘几何（isNull）→ 不弹不炸
reset(enabled=True, win=False)
m.TRAY_ICON = FakeTray(QRect())
m.on_tray_activated(QSystemTrayIcon.Trigger)
ok("tray-null-geom-noop", not m.PET["bubble"].visible)
# 桌宠可见时聚焦语义不变
reset(enabled=True)
m.on_tray_activated(QSystemTrayIcon.Trigger)
ok("tray-left-pet-visible-focus", m.PET["win"].bubble_focus is True)

print(f"\nSUMMARY: {PASS}/{PASS + FAIL} PASS")
print("PASS" if FAIL == 0 else "FAIL")
sys.exit(1 if FAIL else 0)
