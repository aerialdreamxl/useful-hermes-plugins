# -*- coding: utf-8 -*-
"""test_ground_fixes.py — 桌宠/地面缺陷修复探针（N12/N13/N14/N15/N16）。

N12 端点跳方向改按行进方向取（旧判据 walk_target>=x 在抵达吸附后恒真 → 左邻跳不到）
N13 端点前置——只从行进方向端点掷跳（段中部抵达不再对角滑移跳邻段）
N14 纯垂直缩放（只拖底边/顶边）跟随：dh 并入零判定 + 底/顶沿按旧基线判
N15 跟随重键后浮点 1ulp 差异按容差找回线段（旧版精确相等判否 → 跟随断链）
N16 任务栏路径改矩形求交换算（旧版左上角点换算会丢负原点任务栏）
side-env python 跑（offscreen Qt）。测试纪律：只用 scratch 隔离总线，绝不碰真实总线与 config。
"""
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = Path(__file__).resolve().parent
WORKER = HERE.parent / "worker"
sys.path.insert(0, str(WORKER))
sys.path.insert(0, str(HERE))

from PySide6.QtWidgets import QApplication
app = QApplication([])

import pet_window as pw
import ground
from ground import build_ground_segments
from pet_window import PetWindow

from _env import HOME, scratch  # noqa: E402

BUS = scratch("gui27-fixes")
BUS.mkdir(parents=True, exist_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

meta = {"hermes_home": HOME, "bus_dir": str(BUS)}
cfg_on = {"enable-pet": True, "slug": "fairy", "scale": 0.7,
          "roam": True, "topmost": False}


def mkwin(cfg=None):
    w = PetWindow(meta, BUS, lambda: dict(cfg or cfg_on), None, pet_menu=None)
    w.roam_on = True
    w.at_rest = True
    w.dragging = False
    return w

# ============ N12：端点跳方向按行进方向取（左邻可达）============
# 旧判据 `1 if walk_target >= x else -1`：抵达时 move 把 x 吸附到
# int(walk_target)，`int(walk_target) <= walk_target` 永真 → 恒 +1 → 左邻
# 线段永远跳不到。这里把 walk_target 钉成与 x 完全相等（抵达态）复现。
w12 = mkwin()
H12, W12 = w12.height(), w12.width()
cur12 = (300.0, 700.0, 800.0, 700.0)
left12 = (0.0, 650.0, 200.0, 650.0)          # 左侧间隙 100 ≤ W12，高差 50
w12.ground_segs = [cur12, left12]
w12.seg_owner = {cur12: None, left12: None}
w12._refresh_ground = lambda: w12.ground_segs
w12.move(300, int(700 - H12))                # 左端点（可行域 lo=300）
w12._bind_seg(cur12)
orig_adj = pw.ADJACENT_CHANCE
pw.ADJACENT_CHANCE = 1.0
try:
    w12._vert_cooldown_until = 0.0
    w12._vert_ban.clear()
    w12.dir = -1
    w12.walk_target = float(w12.x())          # 抵达吸附态：walk_target == x
    w12.phase = "walk"
    hopped12 = w12._try_adjacent_hop(time.time())
finally:
    pw.ADJACENT_CHANCE = orig_adj
ck("N12: dir=-1 + walk_target==x -> hop fires (dir_hint follows dir)",
   hopped12 is True)
ck("N12: left hop picks left neighbor (climb to y=650)",
   w12.phase == "climb" and w12.climb_seg is left12)

# 右向镜像（防右邻回归）：右端点 + walk_target == x 仍能跳右邻
w12b = mkwin()
H12b, W12b = w12b.height(), w12b.width()
cur12b = (0.0, 700.0, 400.0, 700.0)
right12b = (450.0, 600.0, 780.0, 600.0)      # 右侧间隙 50 ≤ W12b，高差 100
w12b.ground_segs = [cur12b, right12b]
w12b.seg_owner = {cur12b: None, right12b: None}
w12b._refresh_ground = lambda: w12b.ground_segs
w12b.move(int(400 - W12b), int(700 - H12b))  # 右端点（可行域 hi=266）
w12b._bind_seg(cur12b)
pw.ADJACENT_CHANCE = 1.0
try:
    w12b._vert_cooldown_until = 0.0
    w12b._vert_ban.clear()
    w12b.dir = 1
    w12b.walk_target = float(w12b.x())
    w12b.phase = "walk"
    hopped12b = w12b._try_adjacent_hop(time.time())
finally:
    pw.ADJACENT_CHANCE = orig_adj
ck("N12: dir=+1 right-endpoint hop still works",
   hopped12b is True and w12b.phase == "climb" and w12b.climb_seg is right12b)

# ============ N13：端点前置——段中部抵达不掷跳 ============
w13 = mkwin()
H13, W13 = w13.height(), w13.width()
seg13 = (0.0, 700.0, 400.0, 700.0)
nb13 = (450.0, 600.0, 780.0, 600.0)          # 右邻：间隙 50 ≤ W13，高差 100
w13.ground_segs = [seg13, nb13]
w13.seg_owner = {seg13: None, nb13: None}
w13._refresh_ground = lambda: w13.ground_segs
w13._bind_seg(seg13)
end13 = float(400 - W13)                     # 行进方向可行端点 = 266
pw.ADJACENT_CHANCE = 1.0
try:
    w13._vert_cooldown_until = 0.0
    w13._vert_ban.clear()
    # (a) 段中部（_stroll_target 的段内随机点）抵达 → 不掷跳
    w13.move(100, int(700 - H13))
    w13.dir = 1
    w13.walk_target = 100.0
    w13.phase = "walk"
    mid = w13._try_adjacent_hop(time.time())
    ck("N13: mid-seg arrival -> no hop (design: hop only at endpoint)",
       mid is False and w13.phase == "walk")
    # (b) 端点外 4px（> 2×ARRIVE_EPS）→ 仍算段中部
    w13.move(int(end13 - 4), int(700 - H13))
    w13.walk_target = end13 - 4
    near = w13._try_adjacent_hop(time.time())
    ck("N13: 4px short of endpoint -> still no hop (threshold 2*ARRIVE_EPS)",
       near is False and w13.phase == "walk")
    # (c) 真抵端点（≤2×ARRIVE_EPS）→ 掷跳
    w13.move(int(end13 - 1), int(700 - H13))
    w13.walk_target = end13 - 1
    at_end = w13._try_adjacent_hop(time.time())
finally:
    pw.ADJACENT_CHANCE = orig_adj
ck("N13: endpoint arrival -> hop fires",
   at_end is True and w13.phase == "climb" and w13.climb_seg is nb13)
# (d) 同高平移落点不是端点 → 不连跳（平移分支 walk_target=tx 是邻段左沿）
w13b = mkwin()
H13b, W13b = w13b.height(), w13b.width()
a13 = (0.0, 700.0, 500.0, 700.0)
b13 = (550.0, 700.0, 1000.0, 700.0)          # 同高邻段（间隙 50）
c13 = (1050.0, 700.0, 1400.0, 700.0)         # 再右一段（连跳才会用到）
w13b.ground_segs = [a13, b13, c13]
w13b.seg_owner = {a13: None, b13: None, c13: None}
w13b._refresh_ground = lambda: w13b.ground_segs
w13b._bind_seg(a13)
pw.ADJACENT_CHANCE = 1.0
try:
    w13b._vert_cooldown_until = 0.0
    w13b._vert_ban.clear()
    w13b.move(int(500 - W13b), int(700 - H13b))   # a13 右端点
    w13b.dir = 1
    w13b.walk_target = float(w13b.x())
    w13b.phase = "walk"
    first13b = w13b._try_adjacent_hop(time.time())  # 同高 → walk 直送 b13 左沿
    w13b.move(int(550), int(700 - H13b))            # 抵达 b13 左沿（非端点）
    w13b.dir = 1
    w13b.walk_target = 550.0
    w13b.phase = "walk"
    second13b = w13b._try_adjacent_hop(time.time())
finally:
    pw.ADJACENT_CHANCE = orig_adj
ck("N13: same-height translation hop starts walk", first13b is True and w13b.phase == "walk")
ck("N13: arrival at neighbor's inner edge -> no chain hop", second13b is False)

# ============ N14：纯垂直缩放跟随（只拖底边/顶边）============
w14 = mkwin()
H14, W14 = w14.height(), w14.width()
seg14 = (300.0, 500.0, 700.0, 500.0)
w14.ground_segs = [seg14]
w14.seg_owner = {seg14: 999}
w14._refresh_ground = lambda: w14.ground_segs
w14.move(400, int(500 - H14))
fake14 = [100.0, 100.0, 700.0, 500.0]        # owner 逻辑矩形 l,t,r,b
orig_owr = pw.owner_window_rect
orig_wlr = pw.window_logical_rect
pw.owner_window_rect = lambda h: tuple(fake14) if h == 999 else None
pw.window_logical_rect = lambda *r: tuple(r)
try:
    w14._bind_seg(seg14)                     # 取基准
    # (a) 只拖底边 +120：dx=dy=dw=0、dh=+120（旧版这三项全零直接 return）
    fake14[3] += 120.0
    w14._follow_owner(time.time())
    ck("N14: pure bottom resize -> feet follow new bottom (y += 120)",
       abs((w14.y() + H14) - 620.0) <= 2)
    ck("N14: pure dh re-keys cur_seg to new bottom edge",
       abs(w14.cur_seg[1] - 620.0) <= 1)
    ck("N14: pure dh does not jump to owner top edge",
       abs(w14.y() - (620.0 - H14)) <= 2)
    ck("N14: pure dh keeps x (no horizontal drift)", abs(w14.x() - 400) <= 1)
    # (b) 底边再上移 60：跟随回缩
    fake14[3] -= 60.0
    w14._follow_owner(time.time())
    ck("N14: bottom resize back up -> feet follow", abs((w14.y() + H14) - 560.0) <= 2)
    # (c) 矩形无变化仍早退（dh 没有破坏零判定）
    y14 = w14.y()
    ck("N14: unchanged rect still no-op",
       w14._follow_owner(time.time()) is False and w14.y() == y14)
finally:
    pw.owner_window_rect = orig_owr
    pw.window_logical_rect = orig_wlr

# (d) 只拖顶边（bottom 不动，dy≠0 且高度变）——旧版用新矩形反推旧底沿会把
#     底沿误判成顶沿，宠物跳到顶沿；旧基线判定下底沿归属不变、y 不动。
w14b = mkwin()
H14b = w14b.height()
w14b.ground_segs = [seg14]
w14b.seg_owner = {seg14: 999}
w14b._refresh_ground = lambda: w14b.ground_segs
w14b.move(400, int(500 - H14b))
fake14b = [100.0, 100.0, 700.0, 500.0]
pw.owner_window_rect = lambda h: tuple(fake14b) if h == 999 else None
pw.window_logical_rect = lambda *r: tuple(r)
try:
    w14b._bind_seg(seg14)
    fake14b[1] += 100.0                      # 顶边下拖：bottom 固定 500
    w14b._follow_owner(time.time())
    ck("N14: top-edge drag keeps pet on unchanged bottom edge",
       abs((w14b.y() + H14b) - 500.0) <= 2 and abs(w14b.cur_seg[1] - 500.0) <= 1)
finally:
    pw.owner_window_rect = orig_owr
    pw.window_logical_rect = orig_wlr

# ============ N15：跟随重键后的浮点断链（容差找回）============
w15 = mkwin()
seg15 = (300.0, 500.0, 700.0, 500.0)
drift15 = (300.0 + 1e-9, 500.0, 700.0, 500.0)   # 1ulp 级差异（元组精确相等判否）
orig_bgs = pw.build_ground_segments
pw.build_ground_segments = lambda *a, **k: ([seg15], {seg15: 4242})
try:
    w15.cur_seg = drift15
    w15.cur_owner = None
    w15._refresh_ground()
    ck("N15: 1ulp drift -> seg re-found and rebound to list instance",
       w15.cur_seg is seg15)
    ck("N15: 1ulp drift -> owner re-bound (follow chain intact)",
       w15.cur_owner == 4242)
    # 精确相等优先：同值不同对象也重绑（is 语义维持）
    w15.cur_seg = (300.0, 500.0, 700.0, 500.0)
    w15.cur_owner = 4242
    w15._refresh_ground()
    ck("N15: exact-value twin rebound to list instance (is)",
       w15.cur_seg is seg15 and w15.cur_owner == 4242)
    # 超出容差（1px > SEG_MATCH_EPS）→ 走原有落空分支，不误绑
    w15.cur_seg = (301.0, 500.0, 700.0, 500.0)
    w15.cur_owner = 4242
    w15._refresh_ground()
    ck("N15: >eps drift -> owner cleared (no false bind)", w15.cur_owner is None)
    ck("N15: SEG_MATCH_EPS is sub-pixel", 0 < pw.SEG_MATCH_EPS <= 0.5)
finally:
    pw.build_ground_segments = orig_bgs

# ============ N16：任务栏上界改矩形求交换算 ============
geo16 = app.primaryScreen().availableGeometry()
orig_enum16 = ground.enumerate_window_rects
orig_tb16 = ground._taskbar_rects
try:
    ground.enumerate_window_rects = lambda h: []
    # (a) 负原点任务栏（最大化形态隐藏边框 -8）：点换算整条丢、矩形求交保留
    ground._taskbar_rects = lambda: [(-8, 700, 799, 799)]
    segs16 = build_ground_segments(134, 145, geo16, 0)
    ck("N16: negative-origin taskbar top edge registered (rect intersect)",
       any(abs(s[1] - 700.0) < 1 and s[2] > 700.0 for s in segs16))
    ck("N16: legacy point conversion would have dropped that taskbar",
       ground._to_logical_point(-8, 700)[0] is None)
    # (b) 正原点任务栏行为不变（回归）
    ground._taskbar_rects = lambda: [(0, 700, 799, 799)]
    segs16b = build_ground_segments(134, 145, geo16, 0)
    ck("N16: positive-origin taskbar top edge still registered",
       any(abs(s[1] - 700.0) < 1 and s[0] == 0.0 for s in segs16b))
    # (c) 头顶空间过滤照旧：站不进去的任务栏上界剔除
    ground._taskbar_rects = lambda: [(0, 100, 799, 799)]
    segs16c = build_ground_segments(134, 145, geo16, 0)
    ck("N16: taskbar without headroom still filtered",
       not any(abs(s[1] - 100.0) < 1 for s in segs16c))
finally:
    ground.enumerate_window_rects = orig_enum16
    ground._taskbar_rects = orig_tb16

for wn in (w12, w12b, w13, w13b, w14, w14b, w15):
    wn.close()

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
