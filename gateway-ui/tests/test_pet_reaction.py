# -*- coding: utf-8 -*-
"""test_pet_reaction.py — 事件反应 PetWindow.play_reaction 通用反应 API 断言。

side-env python 跑（QT_QPA_PLATFORM=offscreen）。临时总线只进 scratch
gui24-dpi-*；真实 pets/ 只读。本轮 API 不接任何事件源（收口方后续接线）。
判据：
- kind ∈ {done,failed,await_input} → wave/failed/wave 行（done 经 happy→wave）。
- 播放期间钉住（phase=pause，不跑地面/游走/物理），dir=0。
- duration 后恢复原状态（phase/pause_until/host_state 原样）。
- 重复调用刷新计时、切换行，恢复点仍是首次播放前状态。
- 隐藏/未建 sheet/未知 kind → no-op 返回 False。
- 反应行优先于 host 状态与 fall/walk 姿态。
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

import sprites
from sprites import row_index, HOST_MAP
from pet_window import PetWindow, REACTION_MAP

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui24-dpi-reaction")
BUS.mkdir(parents=True, exist_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg = {"enable-pet": True, "slug": "fairy", "scale": 0.7,
       "roam": True, "topmost": False}
win = PetWindow(meta, BUS, lambda: dict(cfg), None, pet_menu=None)
win.show()
app.processEvents()
# 隔离反应语义：停掉 33ms 动画 tick（否则 pump() 期间 _step 的 roam/ground
# 逻辑会自行改 phase，污染"恢复原状态"断言）。反应计时器是独立 QTimer，不受影响。
win._anim.stop()

def pump(seconds):
    """真实事件循环推进（QTimer 反应计时器靠它触发）。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.005)

def cur_row():
    win._advance_frame(time.time())
    return win.cur_row

ROWS = win.sheet.rows
WAVE_ROW = row_index("wave", ROWS)
FAILED_ROW = row_index("failed", ROWS)
IDLE_ROW = row_index("idle", ROWS)

# ---- 映射契约 ----
ck("REACTION_MAP has exactly 3 kinds",
   set(REACTION_MAP) == {"done", "failed", "await_input"})
ck("done maps to wave row (happy->wave)", REACTION_MAP["done"] == "wave"
   and HOST_MAP["happy"] == "wave")
ck("failed maps to failed row", REACTION_MAP["failed"] == "failed")
ck("await_input maps to wave row", REACTION_MAP["await_input"] == "wave")

# ---- 隐藏时 no-op ----
win.hide()
app.processEvents()
ck("hidden window: play_reaction returns False", win.play_reaction("done") is False)
ck("hidden window: no reaction state", win._reaction is None)
win.show()
app.processEvents()

# ---- 未知 kind no-op ----
ck("unknown kind returns False", win.play_reaction("bogus") is False)
ck("unknown kind leaves no state", win._reaction is None)

# ---- done：wave 行 + 钉住 + 计时恢复 ----
win.set_host("idle", True)
win.phase = "pause"
win.pause_until = time.time() + 30  # 未到期 → 恢复后仍 pause
prev_pause = win.pause_until
ck("play done returns True", win.play_reaction("done", 0.35) is True)
ck("during reaction: phase pinned to pause", win.phase == "pause")
ck("during reaction: dir=0", win.dir == 0)
ck("during reaction: state recorded", win._reaction is not None
   and win._reaction["kind"] == "done")
ck("during reaction: wave row shown", cur_row() == WAVE_ROW)
ck("during reaction: host_state untouched", win.host_state == "idle")
# 物理冻结：反应期 _step 不得移动窗口、不得改 phase（不注入非法态——
# 反应契约就是钉住，外部也不该在播放中改 phase）
win.phase = "pause"
x_before, y_before = win.x(), win.y()
for _ in range(10):
    win._step()
    app.processEvents()
ck("during reaction: no movement (physics frozen)",
   (win.x(), win.y()) == (x_before, y_before))
ck("during reaction: phase stays pause", win.phase == "pause")
pump(0.7)
ck("after duration: reaction cleared", win._reaction is None)
ck("after duration: phase restored", win.phase == "pause")
ck("after duration: pause_until restored", abs(win.pause_until - prev_pause) < 1e-6)
ck("after duration: host row back to idle", cur_row() == IDLE_ROW)

# ---- failed：failed 行 ----
ck("play failed returns True", win.play_reaction("failed", 0.3) is True)
ck("failed shows failed row", cur_row() == FAILED_ROW)
pump(0.6)
ck("failed reaction ends", win._reaction is None)

# ---- await_input：wave 行 ----
ck("play await_input returns True", win.play_reaction("await_input", 0.3) is True)
ck("await_input shows wave row", cur_row() == WAVE_ROW)
pump(0.6)
ck("await_input reaction ends", win._reaction is None)

# ---- 重复调用：刷新计时 + 切行，恢复点不变 ----
win.phase = "pause"
win.pause_until = time.time() + 30
prev_pause = win.pause_until
win.play_reaction("done", 0.3)
pump(0.15)
ck("repeat call while active returns True", win.play_reaction("failed", 0.5) is True)
ck("repeat call switches row to failed", cur_row() == FAILED_ROW)
ck("repeat call keeps single state", win._reaction["kind"] == "failed")
pump(0.35)  # 首次 0.3s 已过，但被刷新到 0.5s → 仍在播放
ck("repeat call refreshed timer (still active past first deadline)",
   win._reaction is not None)
pump(0.4)
ck("after refreshed duration: cleared", win._reaction is None)
ck("after repeat: restore point unchanged", abs(win.pause_until - prev_pause) < 1e-6)

# ---- 反应行优先于 host 状态 ----
win.set_host("error", True)  # host error → failed 行
win.play_reaction("done", 0.3)  # 反应 done → wave 行应压过 host failed
ck("reaction row overrides host state", cur_row() == WAVE_ROW)
pump(0.6)
ck("after reaction: host error row resumes", cur_row() == FAILED_ROW)
win.set_host("idle", True)

# ---- 反应行优先于 fall 姿态 ----
win.phase = "fall"
win.play_reaction("await_input", 0.3)
ck("reaction overrides fall pose", cur_row() == WAVE_ROW)
ck("reaction pins fall to pause", win.phase == "pause")
pump(0.6)
ck("after reaction: prev fall phase restored", win.phase == "fall")
win.phase = "pause"

# ---- duration 缺省 4.0 / 非法值回落 ----
ck("default duration accepted", win.play_reaction("done") is True)
ck("default duration ~4s", win._reaction is not None
   and 3.5 < win._reaction["until"] - time.time() <= 4.0)
win._reaction_timer.stop()
win._end_reaction()
ck("bad duration falls back to 4.0", win.play_reaction("done", "xyz") is True
   and 3.5 < win._reaction["until"] - time.time() <= 4.0)
win._reaction_timer.stop()
win._end_reaction()
ck("non-positive duration falls back to 4.0", win.play_reaction("done", 0) is True
   and 3.5 < win._reaction["until"] - time.time() <= 4.0)
win._reaction_timer.stop()
win._end_reaction()

# ---- 无 sheet 时 no-op（不炸）----
saved_sheet = win.sheet
win.sheet = None
ck("no sheet: play_reaction returns False", win.play_reaction("done") is False)
win.sheet = saved_sheet

win.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
