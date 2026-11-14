# -*- coding: utf-8 -*-
"""test_bubble_focus.py — 气泡聚焦增强：气泡开着钉住桌宠演 review（模型思考动画）。
side-env offscreen。真实 pets 只读（slug=fairy），总线只进 scratch/gui25-focus。
判据：
- bubble_focus=True → _advance_frame 演 review 行（即便 host idle）；_step 掐断游走钉住。
- reaction 优先级仍最高（播放期盖过 bubble_focus）。
- bubble_focus=False → 恢复 host_state 动画/游走。
- main 接缝：_set_bubble_focus 置位；_on_pet_click 即时置位；_sync_bubble 按可见性驱动。
- 纯运行时标志，零 settings_req（A2）。
"""
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication
app = QApplication([])

from pet_window import PetWindow
from sprites import HOST_MAP, row_index
import main as m

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui25-focus")
BUS.mkdir(parents=True, exist_ok=True)

R = []
def ck(name, cond):
    R.append((name, bool(cond)))

meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg = {"enable-pet": True, "slug": "fairy", "scale": 0.7,
       "roam": True, "topmost": False}
win = PetWindow(meta, BUS, lambda: dict(cfg), None, pet_menu=None, on_click=None)
win.show()
app.processEvents()
win._anim.stop()   # 隔离物理 tick，手动驱动 _step/_advance_frame
win.move(600, 500)
app.processEvents()

ck("sheet-loaded", win.sheet is not None)
review_row = row_index("review", win.sheet.rows)
idle_row = row_index("idle", win.sheet.rows)

# ---- 1. 默认无聚焦 → host idle 演 idle 行 ----
win.bubble_focus = False
win.host_state = "idle"
win.phase = "pause"
win._advance_frame(time.time())
ck("no-focus-idle-row", win.cur_row == idle_row)

# ---- 2. 聚焦 → 演 review 行（host 仍 idle）----
win.bubble_focus = True
win.host_state = "idle"
win.phase = "pause"
win._advance_frame(time.time())
ck("focus-review-row", win.cur_row == review_row)
ck("focus-overrides-idle", win.cur_row != idle_row)

# ---- 3. 聚焦时 host busy 也演 review（气泡优先于 host_state）----
win.host_state = "acting"   # 正常会演 run
win._advance_frame(time.time())
ck("focus-overrides-busy", win.cur_row == review_row)

# ---- 4. reaction 优先级最高（盖过 bubble_focus）----
win.bubble_focus = True
win.host_state = "idle"
ok = win.play_reaction("done", duration_s=4.0)
ck("reaction-starts", ok is True)
win._advance_frame(time.time())
wave_row = row_index("wave", win.sheet.rows)
ck("reaction-over-focus", win.cur_row == wave_row)
win._end_reaction()   # 收尾恢复

# ---- 5. _step 聚焦掐断游走：walk 被中止转 pause，位置不动 ----
win.bubble_focus = True
win.host_state = "idle"
win.at_rest = True
win.dragging = False
win.roam_on = True
win.phase = "walk"
win.dir = 1
win.walk_target = win.x() + 300
x0 = win.x()
win._step()
ck("focus-aborts-walk", win.phase == "pause")
ck("focus-no-move", win.x() == x0)
ck("focus-dir-zero", win.dir == 0)

# ---- 6. 聚焦时 pause 不启动游走（_plan 不被调）----
win.phase = "pause"
win.pause_until = 0.0   # 即便计时到期
win.at_rest = True
called = {"plan": False}
orig_plan = win._plan
win._plan = lambda now: called.__setitem__("plan", True)
win._step()
ck("focus-no-plan", called["plan"] is False)
win._plan = orig_plan

# ---- 7. 解除聚焦 → 恢复 host 动画 ----
win.bubble_focus = False
win.host_state = "idle"
win.phase = "pause"
win._advance_frame(time.time())
ck("unfocus-restores-idle", win.cur_row == idle_row)

# ---- 8. 解除聚焦 + 闲 → 游走可恢复（_plan 被调）----
win.phase = "pause"
win.pause_until = 0.0
win.at_rest = True
win.dragging = False
called2 = {"plan": False}
win._plan = lambda now: called2.__setitem__("plan", True)
win._step()
ck("unfocus-roam-resumes", called2["plan"] is True)
win._plan = orig_plan

# ---- 9. main 接缝：_set_bubble_focus 置位 ----
m.META = {"bus_dir": str(BUS), "hermes_home": HOME}
m.PET["win"] = win
m._set_bubble_focus(True)
ck("main-set-focus-true", win.bubble_focus is True)
m._set_bubble_focus(False)
ck("main-set-focus-false", win.bubble_focus is False)
m.PET["win"] = None
m._set_bubble_focus(True)   # 无窗口不炸
ck("main-set-focus-nowin-safe", True)

# ---- 10. main 接缝：_sync_bubble 按气泡可见性驱动聚焦 ----
class FakeBubble:
    def __init__(self, vis):
        self._vis = vis
        self.calls = []
    def isVisible(self):
        return self._vis
    def hide_bubble(self):
        self._vis = False
        self.calls.append("hide")
    def refresh_view(self):
        self.calls.append("refresh")
        return False
    def follow(self, r):
        self.calls.append("follow")

class FakeWin:
    def __init__(self):
        self.bubble_focus = False
        self._vis = True
    def isVisible(self):
        return self._vis
    def x(self): return 600
    def y(self): return 500
    def width(self): return 100
    def height(self): return 100

fw = FakeWin()
m.PET["win"] = fw
m.PET["balance_cfg"] = {"enabled": True}
# 气泡可见 → 聚焦
m.PET["bubble"] = FakeBubble(True)
m._sync_bubble()
ck("sync-visible-focus", fw.bubble_focus is True)
# 气泡不可见 → 解除聚焦
m.PET["bubble"] = FakeBubble(False)
m._sync_bubble()
ck("sync-hidden-unfocus", fw.bubble_focus is False)
# 门控关 → 收气泡 + 解除聚焦
m.PET["bubble"] = FakeBubble(True)
m.PET["balance_cfg"] = {"enabled": False}
m._sync_bubble()
ck("sync-gate-off-unfocus", fw.bubble_focus is False)
# 气泡未建 → 解除聚焦不炸
m.PET["bubble"] = None
m.PET["balance_cfg"] = {"enabled": True}
m._sync_bubble()
ck("sync-no-bubble-unfocus", fw.bubble_focus is False)

# ---- 11. A2：聚焦全程零 settings_req ----
ck("focus-zero-settings-req", not (BUS / "settings_req.json").exists())

win.hide()
n_ok = sum(1 for _, ok in R if ok)
for name, ok in R:
    print(("ok " if ok else "FAIL ") + name)
print(f"\nSUMMARY: {n_ok}/{len(R)} PASS")
print("PASS" if n_ok == len(R) else "FAIL")
sys.exit(0 if n_ok == len(R) else 1)
