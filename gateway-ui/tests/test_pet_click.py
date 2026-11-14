# -*- coding: utf-8 -*-
"""test_pet_click.py — 余额气泡 PetWindow 单击门槛 on_click 回调 offscreen 断言。

side-env python 跑（QT_QPA_PLATFORM=offscreen）。真实 pets 库存只读
（hermes_home=<HERMES_HOME>，slug=fairy），测试总线只进
scratch/gui25-bub。判据：位移≤6 触发 on_click、>6 不触发且拖拽语义保留
（dragging/确认计时器照旧）、on_click=None 旧行为、右键菜单不受影响。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

app = QApplication([])

from pet_window import PetWindow, CLICK_MAX_PX, CONFIRM_MIN_S, CONFIRM_MAX_S

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui25-bub/click")
BUS.mkdir(parents=True, exist_ok=True)

R = []


def ck(name, cond):
    R.append((name, bool(cond)))


meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg = {"enable-pet": True, "slug": "fairy", "scale": 0.7,
       "roam": True, "topmost": False}
clicks = []
win = PetWindow(meta, BUS, lambda: dict(cfg), None, pet_menu=None,
                on_click=lambda: clicks.append(time.time()))
win.show()
app.processEvents()
win._anim.stop()  # 隔离：停 33ms 物理 tick，防 roam/ground 污染断言
win.move(600, 500)
app.processEvents()


def press(gx, gy, button=Qt.LeftButton):
    local = QPointF(gx - win.x(), gy - win.y())
    ev = QMouseEvent(QEvent.MouseButtonPress, local, QPointF(gx, gy),
                     button, button, Qt.NoModifier)
    QApplication.sendEvent(win, ev)


def move(gx, gy):
    local = QPointF(gx - win.x(), gy - win.y())
    ev = QMouseEvent(QEvent.MouseMove, local, QPointF(gx, gy),
                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(win, ev)


def release(gx, gy, button=Qt.LeftButton):
    local = QPointF(gx - win.x(), gy - win.y())
    ev = QMouseEvent(QEvent.MouseButtonRelease, local, QPointF(gx, gy),
                     button, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(win, ev)


ck("CLICK_MAX_PX == 6 (spike)", CLICK_MAX_PX == 6)
ck("sheet loaded (real pets stock)", win.sheet is not None and win.slug == "fairy")

# ---- 零位移 release = 单击 ----
n0 = len(clicks)
press(650, 550)
ck("press: not dragging yet (threshold gate)", win.dragging is False)
release(650, 550)
ck("zero-move release fires on_click", len(clicks) == n0 + 1)
ck("click: dragging stays False", win.dragging is False)
ck("click: press state cleared", win._press_pos is None and win._drag_started is False)

# ---- 位移恰好 6（manhattan 4+2）= 单击 ----
n0 = len(clicks)
x0, y0 = win.x(), win.y()
press(650, 550)
move(654, 552)
ck("move<=6: window does not move", (win.x(), win.y()) == (x0, y0))
ck("move<=6: not dragging", win.dragging is False)
release(654, 552)
ck("move==6 release fires on_click", len(clicks) == n0 + 1)

# ---- 位移 7（>6）= 拖拽，不触发 on_click ----
n0 = len(clicks)
x0, y0 = win.x(), win.y()
press(650, 550)
move(657, 550)
ck("move>6: dragging True", win.dragging is True)
ck("drag start: dir=0", win.dir == 0)
ck("drag start: confirm timer zeroed (rule5)", win._confirm_due == 0.0)
ck("move>6: window followed mouse", (win.x(), win.y()) == (x0 + 7, y0))
move(680, 570)
ck("drag continues following", (win.x(), win.y()) == (x0 + 30, y0 + 20))
ck("dragging: confirm stays zero", win._confirm_due == 0.0)
now = time.time()
release(680, 570)
ck("drag release: no on_click", len(clicks) == n0)
ck("drag release: dragging False", win.dragging is False)
ck("drag release: phase pause", win.phase == "pause")
ck("drag release: pause_until far future", win.pause_until > now + 9000)
ck("drag release: confirm timer in [0.75,1.25]s",
   CONFIRM_MIN_S - 0.05 <= win._confirm_due - now <= CONFIRM_MAX_S + 0.05)
pos_file = BUS / "pet_pos.json"
ck("drag release: pet_pos.json written", pos_file.is_file())
saved = json.loads(pos_file.read_text(encoding="utf-8"))
ck("drag release: saved pos matches", saved["x"] == win.x() and saved["y"] == win.y())

# ---- 阈值内先动后超阈值：从 crossing 点跟手（无跳变）----
n0 = len(clicks)
win.move(600, 500)
app.processEvents()
press(650, 550)
move(653, 550)   # 3px：潜在单击，不动
ck("pre-threshold: no move", (win.x(), win.y()) == (600, 500))
move(658, 550)   # 累计 8px：crossing，跟手
ck("crossing: dragging True", win.dragging is True)
ck("crossing: window at mouse offset (no jump)", win.x() == 608 and win.y() == 500)
release(658, 550)
ck("crossed drag: no on_click", len(clicks) == n0)

# ---- 单击不写 settings_req（A2）----
press(650, 550)
release(650, 550)
ck("click writes no settings_req", not (BUS / "settings_req.json").exists())

# ---- 右键：弹菜单，不触发 on_click ----
popups = []
orig_popup = win._popup
win._popup = lambda: popups.append(1)
n0 = len(clicks)
press(650, 550, button=Qt.RightButton)
release(650, 550, button=Qt.RightButton)
ck("right press opens pet menu", len(popups) == 1)
ck("right click: no on_click", len(clicks) == n0)
ck("right click: not dragging", win.dragging is False)
win._popup = orig_popup

# ---- on_click 回调异常不炸窗口 ----
def boom():
    raise RuntimeError("boom")
win.on_click = boom
press(650, 550)
release(650, 550)
ck("callback exception swallowed", win.isVisible())
win.on_click = lambda: clicks.append(time.time())

# ---- on_click=None：旧版行为完全一致 ----
win2 = PetWindow(meta, BUS, lambda: dict(cfg), None, pet_menu=None)
win2.show()
app.processEvents()
win2._anim.stop()
win2.move(300, 300)
app.processEvents()


def press2(gx, gy, w=win2):
    local = QPointF(gx - w.x(), gy - w.y())
    ev = QMouseEvent(QEvent.MouseButtonPress, local, QPointF(gx, gy),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(w, ev)


def release2(gx, gy, w=win2):
    local = QPointF(gx - w.x(), gy - w.y())
    ev = QMouseEvent(QEvent.MouseButtonRelease, local, QPointF(gx, gy),
                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(w, ev)


press2(350, 350)
ck("legacy: press immediately dragging", win2.dragging is True)
ck("legacy: press zeroes confirm timer", win2._confirm_due == 0.0)
now = time.time()
release2(350, 350)
ck("legacy: zero-move release runs drag-release path", win2.dragging is False
   and win2.phase == "pause")
ck("legacy: confirm timer restarted",
   CONFIRM_MIN_S - 0.05 <= win2._confirm_due - now <= CONFIRM_MAX_S + 0.05)
ck("legacy: no crash without on_click", win2.isVisible())

# ---- 既有位置参数顺序不破坏（5 参构造）----
win3 = PetWindow(meta, BUS, lambda: dict(cfg), None, None)
ck("5-positional-arg construction still works", win3.on_click is None)
win3._anim.stop()
win3.close()

win2.close()
win.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R) - len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)