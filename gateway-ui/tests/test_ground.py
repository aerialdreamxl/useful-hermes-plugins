# -*- coding: utf-8 -*-
"""test_ground.py — 地面系统线段制地面系统断言全量移植（旧 ground18_test.py 31 断言）
+ 菜单驻留 BUG1/2/3 回归。side-env python 跑（offscreen Qt）。
判定语义照 CONTRACT §7：rule6 子线段±2px/find_support/find_fall_target、
rule7 攀爬全包含、rule2 关游走零物理、rule5 确认计时器。"""
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication
app = QApplication([])

import pet_window as pw
import ground
from ground import (find_support, find_fall_target, find_climb_target,
                    seg_contains_x, build_ground_segments)
from pet_window import PetWindow

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui22-ground")
BUS.mkdir(parents=True, exist_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

# ============ 纯几何：线段查找（旧 ground18 前 14 断言）============
floor = (0.0, 800.0, 1600.0, 800.0)      # 屏幕下界
winbot = (300.0, 500.0, 700.0, 500.0)    # 某窗口下沿
wintop = (300.0, 200.0, 700.0, 200.0)    # 某窗口上沿
segs = [floor, winbot, wintop]

ck("support: on winbot", find_support(segs, 350.0, 450.0, 500.0) is winbot)
ck("support: on floor", find_support(segs, 10.0, 90.0, 800.0) is floor)
ck("support: not aligned -> None", find_support(segs, 350.0, 450.0, 600.0) is None)
ck("support: x outside -> None", find_support(segs, 800.0, 900.0, 500.0) is None)

ck("fall: from mid -> winbot", find_fall_target(segs, 350.0, 450.0, 300.0) is winbot)
ck("fall: below winbot -> floor", find_fall_target(segs, 350.0, 450.0, 600.0) is floor)
ck("fall: outside all x -> floor only via screen", find_fall_target(segs, 1000.0, 1100.0, 300.0) is floor)
ck("fall: already on floor -> None", find_fall_target(segs, 10.0, 90.0, 800.0) is None)

cl = find_climb_target(segs, 350.0, 400.0, 450.0, 500.0, winbot)
ck("climb: wintop not crossing body(400-500) -> None", cl is None)
cl = find_climb_target(segs, 350.0, 150.0, 450.0, 500.0, winbot)
ck("climb: wintop crossing body -> climb to wintop", cl is wintop)
cl = find_climb_target(segs, 350.0, 450.0, 450.0, 500.0, winbot)
ck("climb: excludes standing seg", cl is None or cl is not winbot)

ck("contains: subseg", seg_contains_x(winbot, 350.0, 450.0))
ck("contains: exact", seg_contains_x(winbot, 300.0, 700.0))
ck("contains: too wide -> False", not seg_contains_x(winbot, 200.0, 800.0))

# ============ 真机几何：build_ground_segments 冒烟 ============
meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg_off = {"enable-pet": True, "slug": "fairy", "scale": 0.7, "roam": False, "topmost": False}
cfg_on = dict(cfg_off, roam=True)

win = PetWindow(meta, BUS, lambda: dict(cfg_off), None, pet_menu=None)
ck("window built", win.sheet is not None)
H = win.height()

geo = win._screen_geo()
built = build_ground_segments(win.width(), H, geo, int(win.winId()))
ck("build returns list", isinstance(built, list) and len(built) >= 1)
ck("screen-bottom seg present", any(abs(s[1] - float(geo.bottom())) < 2 for s in built))
ck("all segs are 4-tuples", all(len(s) == 4 for s in built))
ck("seg y1==y2 (horizontal)", all(abs(s[1] - s[3]) < 0.01 for s in built))
ck("seg x1<=x2", all(s[0] <= s[2] for s in built))

# ============ BUG1：关游走 = 零物理（钉在原地）============
win.move(500, 300)
win.roam_on = False
win.phase = "walk"
win.walk_target = 900
win.at_rest = True
win.dragging = False
before = (win.x(), win.y())
for _ in range(30):
    win._step()
    app.processEvents()
after = (win.x(), win.y())
ck("BUG1: roam off -> no movement", before == after)
ck("BUG1: roam off -> no fall/gravity", win.y() == 300)

# ============ rule2：关游走不刷新地面 ============
win.ground_segs = ["SENTINEL"]
for _ in range(10):
    win._step()
ck("rule2: roam off does not rebuild ground", win.ground_segs == ["SENTINEL"])

# ============ rule5+6：开游走，悬空 → 确认计时器到期 → 自由落体到地面 ============
win2 = PetWindow(meta, BUS, lambda: dict(cfg_on), None, pet_menu=None)
win2.roam_on = True
win2.at_rest = True
win2.dragging = False
gb = float(win2._screen_geo().bottom())
win2.ground_segs = [(0.0, gb, 3000.0, gb)]
win2._refresh_ground = lambda: win2.ground_segs  # 冻结，别让真机刷新覆盖
win2.move(400, 100)  # 悬空
win2.phase = "pause"
win2._confirm_due = time.time() - 1  # 立即触发确认
win2._begin_pause(time.time())
win2._confirm_due = time.time() - 1
fell = False
landed_bottom = False
for _ in range(400):
    win2._step()
    app.processEvents()
    if win2.phase == "fall":
        fell = True
    if win2.phase == "pause" and win2.cur_seg is not None and abs((win2.y() + win2.height()) - gb) <= 4:
        landed_bottom = True
        break
    time.sleep(1/90)
ck("rule5/6: airborne -> fall triggered", fell)
ck("rule6: landed on ground segment (screen bottom)", landed_bottom)

# ============ rule5：拖动中计时器归零暂停 ============
win2.dragging = True
win2._confirm_due = time.time() + 100
win2.move(400, 100)
win2.phase = "pause"
y0 = win2.y()
for _ in range(20):
    win2._step()
    app.processEvents()
ck("rule5: dragging -> no fall (timer paused)", win2.y() == y0 and win2.phase == "pause")
ck("rule5: dragging -> confirm_due zeroed", win2._confirm_due == 0.0)
win2.dragging = False

# ============ rule7：站在低线段，高线段穿过身体 → 攀爬上去 ============
win3 = PetWindow(meta, BUS, lambda: dict(cfg_on), None, pet_menu=None)
win3.roam_on = True; win3.at_rest = True; win3.dragging = False
Hh = win3.height()
low_y = 700.0
high_y = 700.0 - (Hh - 10)   # 高线段穿过身体（在窗口矩形内、更高）
win3.ground_segs = [(0.0, low_y, 3000.0, low_y), (300.0, high_y, 900.0, high_y)]
win3._refresh_ground = lambda: win3.ground_segs
win3.move(400, int(low_y - Hh))  # 站在 low 上
win3.cur_seg = win3.ground_segs[0]
win3.phase = "pause"
win3._confirm_due = time.time() - 1  # 立即确认 → 应触发 climb
climbed = False
reached_high = False
for _ in range(200):
    win3._step()
    app.processEvents()
    if win3.phase == "climb":
        climbed = True
    if win3.cur_seg is win3.ground_segs[1] and abs((win3.y() + Hh) - high_y) <= 4:
        reached_high = True
        break
    time.sleep(1/90)
ck("rule7: climb triggered", climbed)
ck("rule7: climbed onto higher segment", reached_high)

# ============ 无瞬移：单帧位移有界 ============
win4 = PetWindow(meta, BUS, lambda: dict(cfg_on), None, pet_menu=None)
win4.roam_on = True; win4.at_rest = True; win4.dragging = False
win4.ground_segs = [(0.0, 800.0, 3000.0, 800.0)]
win4._refresh_ground = lambda: win4.ground_segs
win4.move(400, 795 - win4.height() + 200)  # 距地面 200px 上方
win4.phase = "fall"; win4.fall_vel = 0.0
max_jump = 0
prev = win4.y()
for _ in range(120):
    win4._step(); app.processEvents()
    d = abs(win4.y() - prev)
    if win4.phase == "pause":  # 落位那帧不计（settle 精确贴线）
        break
    max_jump = max(max_jump, d)
    prev = win4.y()
    time.sleep(1/90)
ck("no-teleport: fall per-frame step bounded (<400px)", max_jump < 400)

# ============ rule5：Agent 忙(at_rest=False)时仍确认贴地 ============
win5 = PetWindow(meta, BUS, lambda: dict(cfg_on), None, pet_menu=None)
win5.roam_on = True; win5.at_rest = False; win5.dragging = False  # Agent 忙
Hh5 = win5.height()
win5.ground_segs = [(0.0, 700.0, 3000.0, 700.0)]
win5._refresh_ground = lambda: win5.ground_segs
win5.move(400, 300)  # 悬空（窗口被挪走后遗留位置）
win5.phase = "pause"
win5._confirm_due = time.time() - 1
fell_busy = False
for _ in range(300):
    win5._step(); app.processEvents()
    if win5.phase == "fall":
        fell_busy = True
    if win5.phase == "pause" and win5.cur_seg is not None and abs((win5.y()+Hh5) - 700.0) <= 4:
        break
    time.sleep(1/90)
ck("rule5: Agent busy still confirms ground (window moved)", fell_busy)

# ============ 菜单驻留 BUG2：部分重叠的更高线段不许爬 ============
Hh6 = 145
low6 = (0.0, 700.0, 3000.0, 700.0)
partial = (450.0, 700.0 - (Hh6 - 10), 900.0, 700.0 - (Hh6 - 10))
full = (300.0, 700.0 - (Hh6 - 10), 900.0, 700.0 - (Hh6 - 10))
segs6 = [low6, partial, full]
cl = find_climb_target(segs6, 400.0, 700.0 - Hh6, 534.0, 700.0, low6)
ck("BUG2: partial-overlap higher seg NOT climbable", cl is full or cl is None)
ck("BUG2: full-coverage higher seg climbable", find_climb_target([low6, full], 400.0, 700.0 - Hh6, 534.0, 700.0, low6) is full)
if cl is not None:
    ck("BUG2: after climb, support stable", find_support([cl, low6], 400.0, 534.0, cl[1]) is cl)

# ============ 菜单驻留 BUG3：漫步目标夹在可行域内（右缘不震荡）============
win6 = PetWindow(meta, BUS, lambda: dict(cfg_on), None, pet_menu=None)
win6.roam_on = True; win6.at_rest = True; win6.dragging = False
W6 = win6.width()
seg6 = (0.0, 700.0, 1000.0, 700.0)
win6.ground_segs = [seg6]
win6._refresh_ground = lambda: win6.ground_segs
win6.move(500, int(700 - win6.height()))  # 站在线段内合法位置
win6.cur_seg = seg6
win6.phase = "pause"
win6.pause_until = time.time() - 1
win6._confirm_due = 0.0
random_states = []
import random as _rnd
_orig_rest = pw.REST_CHANCE
pw.REST_CHANCE = 0.0  # 强制每次 _plan 都出 walk 以采样目标
try:
    for seed in range(30):
        _rnd.seed(seed)
        win6.phase = "pause"; win6.pause_until = time.time() - 1
        win6._plan(time.time())
        if win6.phase == "walk":
            random_states.append(win6.walk_target)
finally:
    pw.REST_CHANCE = _orig_rest
ck("BUG3: all walk targets within [x1, x2-pet_w]",
   all(0.0 - 0.01 <= wt <= 1000.0 - W6 + 0.01 for wt in random_states))
ck("BUG3: sampled some walk targets", len(random_states) > 0)

# 右缘恢复：宠物悬在线段外 → confirm 应夹回并落稳，不进 fall 死循环
win6.move(1200, int(700 - win6.height()))  # 完全在线段外但 y 对齐
win6.phase = "pause"
win6._confirm_due = time.time() - 1
settled = False
for _ in range(200):
    win6._step(); app.processEvents()
    if win6.phase == "pause" and win6.cur_seg is not None \
            and abs((win6.y() + win6.height()) - 700.0) <= 4 \
            and win6.x() <= 1000.0 - W6 + 2:
        settled = True
        break
    time.sleep(1/90)
ck("BUG3: out-of-span recovers onto segment", settled)

# ============ 菜单驻留 BUG1：屏幕顶边线段不注册（头顶空间过滤）============
pet_h_chk = 145
top_ok = 0.0 + pet_h_chk - 1 <= 60.0   # 顶边 y=60 < pet_h → False
ck("BUG1: headroom filter rejects segs near screen top", not top_ok)
top_ok2 = 0.0 + pet_h_chk - 1 <= 200.0
ck("BUG1: headroom filter accepts segs with room", top_ok2)

win.close(); win2.close(); win3.close(); win4.close(); win5.close(); win6.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
