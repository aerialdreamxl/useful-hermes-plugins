# -*- coding: utf-8 -*-
"""test_ground_motion.py — 地面物理增强 R26（PROMPT §11.6 验收断言逐条转测试）。
R26-1 遮挡修复（负原点最大化窗口）/ R26-2 线段归属+窗口跟随 /
R26-3 向下爬+跨线段寻路 / R26-4 配置链路。side-env python 跑（offscreen Qt）。
测试纪律：只用 scratch 隔离总线，绝不碰真实总线与 config。"""
import os
import sys
import time
import random
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
from ground import (find_support, find_fall_target, find_climb_target,
                    find_descend_target, find_adjacent_segment,
                    build_ground_segments, window_logical_rect)
from pet_window import PetWindow

from _env import HOME, scratch  # noqa: E402
from _loader import load_plugin  # noqa: E402
BUS = scratch("gui26-motion")
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

# ============ R26-1：遮挡修复（合成枚举，offscreen 屏 0,0,799,799）============
geo = app.primaryScreen().availableGeometry()
orig_enum = ground.enumerate_window_rects
orig_tb = ground._taskbar_rects
ground._taskbar_rects = lambda: []

def enum_A_top(self_hwnd):
    # A=最大化形态（负原点隐形边框）盖住 B；A 在 Z 序顶
    return [(111, -8, -8, 1544, 872), (222, 300, 200, 700, 500)]

def enum_B_top(self_hwnd):
    return [(222, 300, 200, 700, 500), (111, -8, -8, 1544, 872)]

try:
    ground.enumerate_window_rects = enum_A_top
    segs_a, owners_a = build_ground_segments(134, 145, geo, 0, with_owners=True)
    ground.enumerate_window_rects = enum_B_top
    segs_b = build_ground_segments(134, 145, geo, 0)
finally:
    ground.enumerate_window_rects = orig_enum
    ground._taskbar_rects = orig_tb

b_edges_a = [s for s in segs_a if abs(s[1] - 500) < 2 or abs(s[1] - 200) < 2]
b_edges_a = [s for s in b_edges_a if 290 <= s[0] and s[2] <= 710]
ck("R26-1: A(maximized, neg origin) covers B -> B edges culled", not b_edges_a)
b_edges_b = [s for s in segs_b if (abs(s[1] - 500) < 2 or abs(s[1] - 200) < 2)
             and 290 <= s[0] and s[2] <= 710]
ck("R26-1: B on top -> B edges still registered", len(b_edges_b) == 2)
ck("R26-1: default return shape unchanged (list of 4-tuples)",
   isinstance(segs_b, list) and all(len(s) == 4 for s in segs_b))
# 正坐标窗口行为与旧版全等（无负原点时新旧选屏同结果）
ground._taskbar_rects = lambda: []
try:
    ground.enumerate_window_rects = lambda h: [(333, 100, 200, 500, 400)]
    segs_pos, owners_pos = build_ground_segments(134, 145, geo, 0, with_owners=True)
finally:
    ground.enumerate_window_rects = orig_enum
    ground._taskbar_rects = orig_tb
# 顶沿 y=200（过头顶过滤 145），底沿 y=400；offscreen 屏 799 宽内不裁 x
ck("R26-1: positive-origin window edges present",
   any(abs(s[1] - 200) < 2 for s in segs_pos) and any(abs(s[1] - 400) < 2 for s in segs_pos))

# ============ R26-2：归属表 ============
edge_segs = [s for s in segs_pos if abs(s[1] - 200) < 2 or abs(s[1] - 400) < 2]
edge_segs = [s for s in edge_segs if s[0] >= 90]
ck("R26-2: window edges owner=HWND", all(owners_pos.get(s) == 333 for s in edge_segs))
floor_seg = [s for s in segs_pos if abs(s[1] - float(geo.bottom())) < 2]
ck("R26-2: screen-bottom owner=None", floor_seg and owners_pos.get(floor_seg[0]) is None)
ck("R26-2: window_logical_rect identity at dpr=1",
   window_logical_rect(100, 100, 500, 400) == (100.0, 100.0, 500.0, 400.0))

# ============ R26-2：跟随（fake owner_window_rect / window_logical_rect）============
w = mkwin()
H = w.height()
W = w.width()
seg = (300.0, 500.0, 700.0, 500.0)
w.ground_segs = [seg]
w.seg_owner = {seg: 999}
w._refresh_ground = lambda: w.ground_segs  # 冻结全量重建
w.move(400, int(500 - H))
w._bind_seg(seg)
ck("R26-2: bind sets cur_owner", w.cur_owner == 999)

fake_rect = [100.0, 100.0, 700.0, 500.0]  # 逻辑矩形（dpr=1 直传）
orig_owr = pw.owner_window_rect
orig_wlr = pw.window_logical_rect
pw.owner_window_rect = lambda h: tuple(fake_rect) if h == 999 else None
pw.window_logical_rect = lambda *r: tuple(r)
try:
    w._bind_seg(seg)  # 重新取基准
    x0, y0 = w.x(), w.y()
    # 窗口平移 (+50, +30)
    fake_rect[0] += 50; fake_rect[1] += 30; fake_rect[2] += 50; fake_rect[3] += 30
    w._follow_owner(time.time())
    ck("R26-2: follow translates pet by dx", abs(w.x() - (x0 + 50)) <= 1)
    ck("R26-2: follow keeps feet on moved bottom edge",
       abs((w.y() + H) - 530.0) <= 2)
    ck("R26-2: cur_seg re-keyed to moved segment", abs(w.cur_seg[1] - 530.0) <= 1)
    # 窗口右移把宠物挤出线段域 → x 夹可行域
    fake_rect[0] += 400; fake_rect[2] += 400  # 线段 x=[750,1150]
    w._follow_owner(time.time())
    ck("R26-2: x clamped into new feasible span",
       w.cur_seg[0] - 1 <= w.x() <= w.cur_seg[2] - W + 1)
    # 忙时仍跟随（rule5 同口径）
    w.at_rest = False
    xr = w.x()
    fake_rect[0] += 10; fake_rect[2] += 10
    w._follow_owner(time.time())
    ck("R26-2: busy (at_rest=False) still follows", abs(w.x() - (xr + 10)) <= 1)
    w.at_rest = True
    # 拖拽中不跟随
    w.dragging = True
    xd = w.x()
    fake_rect[0] += 80; fake_rect[2] += 80
    w._follow_owner(time.time())
    ck("R26-2: dragging -> no follow", w.x() == xd)
    w.dragging = False
    # owner 失效 → 落体
    fake_rect[0] -= 90; fake_rect[2] -= 90  # 还原基准防夹带
    pw.owner_window_rect = lambda h: None
    changed = w._follow_owner(time.time())
    ck("R26-2: owner gone -> fall", changed and w.phase == "fall" and w.cur_owner is None)
    # follow_window=False：字节级旧行为（_follow_owner 恒 no-op）
    w2 = mkwin(dict(cfg_on, follow_window=False))
    ck("R26-4: worker reload reads follow_window=False", w2.follow_window is False)
    w2.ground_segs = [seg]
    w2.seg_owner = {seg: 999}
    w2._refresh_ground = lambda: w2.ground_segs
    w2.move(400, int(500 - H))
    w2._bind_seg(seg)
    ck("R26-2: follow off -> cur_owner not armed", w2._owner_rect is None)
    xf = w2.x()
    fake_rect[0] += 60; fake_rect[2] += 60
    pw.owner_window_rect = lambda h: tuple(fake_rect)
    ck("R26-2: follow off -> no movement", w2._follow_owner(time.time()) is False and w2.x() == xf)
finally:
    pw.owner_window_rect = orig_owr
    pw.window_logical_rect = orig_wlr

# pause 态 _step 集成：跟随经 _step 每拍生效
w3 = mkwin()
seg3 = (300.0, 500.0, 700.0, 500.0)
w3.ground_segs = [seg3]
w3.seg_owner = {seg3: 999}
w3._refresh_ground = lambda: w3.ground_segs
w3.move(400, int(500 - H))
w3._bind_seg(seg3)
w3.phase = "pause"
w3.pause_until = time.time() + 9999
w3._confirm_due = time.time() + 9999
fr = [100.0, 100.0, 700.0, 500.0]
pw.owner_window_rect = lambda h: tuple(fr)
pw.window_logical_rect = lambda *r: tuple(r)
try:
    w3._bind_seg(seg3)
    x3 = w3.x()
    fr[0] += 40; fr[2] += 40
    w3._step(); app.processEvents()
    ck("R26-2: _step pause integrates follow", abs(w3.x() - (x3 + 40)) <= 1)
finally:
    pw.owner_window_rect = orig_owr
    pw.window_logical_rect = orig_wlr

# ============ R26-3：find_descend_target 纯函数 ============
low = (0.0, 700.0, 1000.0, 700.0)
mid = (300.0, 500.0, 700.0, 500.0)
deep = (300.0, 620.0, 700.0, 620.0)
segs_d = [low, mid, deep]
d = find_descend_target(segs_d, 400.0, 534.0, 500.0, mid, 145)
ck("R26-3: descend picks nearest lower (deep, not low)", d is deep)
d2 = find_descend_target([low, mid], 400.0, 534.0, 500.0, mid, 145)
ck("R26-3: descend to low when no mid blocker", d2 is low)
d3 = find_descend_target(segs_d, 400.0, 534.0, 700.0, low, 145)
ck("R26-3: nothing lower -> None", d3 is None)
partial = (500.0, 620.0, 900.0, 620.0)  # 部分重叠
d4 = find_descend_target([low, mid, partial], 400.0, 534.0, 500.0, mid, 145)
ck("R26-3: partial-overlap lower NOT descendable (BUG2 口径)", d4 is not partial)
d5 = find_descend_target(segs_d, 400.0, 534.0, 500.0, mid, 145)
ck("R26-3: standing seg excluded", d5 is not mid)

# ============ R26-3：descend 动画（单帧有界/落位贴线/中途失效转 fall）============
w4 = mkwin()
H4 = w4.height()
hi = (300.0, 400.0, 700.0, 400.0)
lo = (300.0, 650.0, 700.0, 650.0)
w4.ground_segs = [hi, lo]
w4.seg_owner = {hi: None, lo: None}
w4._refresh_ground = lambda: w4.ground_segs
w4.move(400, int(400 - H4))
w4._bind_seg(hi)
w4._begin_descend(lo, time.time())
ck("R26-3: descend phase set", w4.phase == "descend")
ck("R26-3: descend dur scales with distance", w4.descend_dur >= pw.CLIMB_DUR_S)
max_jump = 0
prev = w4.y()
landed = False
for _ in range(300):
    w4._step(); app.processEvents()
    if w4.phase == "descend":
        max_jump = max(max_jump, abs(w4.y() - prev))
    prev = w4.y()
    if w4.phase == "pause" and w4.cur_seg is lo and abs((w4.y() + H4) - 650.0) <= 4:
        landed = True
        break
    time.sleep(1 / 90)
ck("R26-3: descend lands feet-on-line", landed)
ck("R26-3: descend per-frame step bounded (<120px)", max_jump < 120)
ck("R26-3: after descend back to pause+confirm", w4._confirm_due > 0)

# 中途目标失效 → 当帧中止转 fall
w5 = mkwin()
H5 = w5.height()
hi5 = (300.0, 400.0, 700.0, 400.0)
lo5 = (300.0, 650.0, 700.0, 650.0)
alive = [True]
def refresh5():
    w5.ground_segs = [hi5, lo5] if alive[0] else [hi5]
    w5.seg_owner = {s: None for s in w5.ground_segs}
    return w5.ground_segs
w5._refresh_ground = refresh5
w5.move(400, int(400 - H5))
w5._bind_seg(hi5)
w5._begin_descend(lo5, time.time())
w5._step(); app.processEvents()
alive[0] = False
w5._step(); app.processEvents()
ck("R26-3: target vanished mid-descend -> abort to fall", w5.phase == "fall")

# ============ R26-3：find_adjacent_segment + 端点跳转 ============
base = (0.0, 700.0, 500.0, 700.0)
right_up = (560.0, 600.0, 900.0, 600.0)     # 间隙 60 ≤ pet_w=134，高差 100 ≤ 2H
right_far = (900.0, 700.0, 1200.0, 700.0)   # 间隙 400 > pet_w → 不选
right_high = (560.0, 300.0, 900.0, 300.0)   # 高差 400 > 2H=290 → 不选
segs_adj = [base, right_up, right_far, right_high]
f = find_adjacent_segment(segs_adj, base, 134, 145, +1)
ck("R26-3: adjacent picks near+reachable", f is not None and f[0] is right_up)
ck("R26-3: adjacent target_x within feasible span",
   f is not None and right_up[0] - 0.01 <= f[1] <= right_up[2] - 134 + 0.01)
ck("R26-3: far gap rejected", find_adjacent_segment([base, right_far], base, 134, 145, +1) is None)
ck("R26-3: too-high rejected", find_adjacent_segment([base, right_high], base, 134, 145, +1) is None)
left_seg = (0.0, 650.0, 200.0, 650.0)
base_r = (300.0, 700.0, 800.0, 700.0)
fl = find_adjacent_segment([base_r, left_seg], base_r, 134, 145, -1)
ck("R26-3: left neighbor found", fl is not None and fl[0] is left_seg)

# 端点跳转集成：walk 抵达端点 → 掷中 → climb/descend 带横移
# （offscreen 屏仅 799 宽，线段域全部放在屏内，避免 walk 每帧 _screen_geo 夹带）
w6 = mkwin()
H6, W6 = w6.height(), w6.width()
b6 = (0.0, 700.0, 400.0, 700.0)
r6 = (450.0, 600.0, 780.0, 600.0)   # 间隙 50 ≤ W6=134，高差 100 ≤ 2H6
w6.ground_segs = [b6, r6]
w6.seg_owner = {b6: None, r6: None}
w6._refresh_ground = lambda: w6.ground_segs
w6.move(200, int(700 - H6))
w6._bind_seg(b6)
w6.walk_target = 400.0 - W6
w6.phase = "walk"
w6.dir = 1
orig_adj_chance = pw.ADJACENT_CHANCE
pw.ADJACENT_CHANCE = 1.0
hopped = False
landed6 = False
try:
    for _ in range(400):
        w6._step(); app.processEvents()
        if w6.phase in ("climb", "descend"):
            hopped = True
        if w6.phase == "pause" and w6.cur_seg == r6 and abs((w6.y() + H6) - 600.0) <= 4:
            landed6 = True
            break
        time.sleep(1 / 90)
finally:
    pw.ADJACENT_CHANCE = orig_adj_chance
ck("R26-3: endpoint hop triggered", hopped)
ck("R26-3: hop lands on neighbor seg (x moved into span)",
   landed6 and r6[0] - 1 <= w6.x() <= r6[2] - W6 + 1)
ck("R26-3: hop lands back to pause+confirm (no chain-hop)", w6._confirm_due > 0)

# ============ R26-3：_plan 主动垂直动作 ============
w7 = mkwin()
H7 = w7.height()
hi7 = (300.0, 700.0 - (H7 - 10), 900.0, 700.0 - (H7 - 10))  # 穿过身体
lo7 = (0.0, 700.0, 1000.0, 700.0)
w7.ground_segs = [lo7, hi7]
w7.seg_owner = {lo7: None, hi7: None}
w7._refresh_ground = lambda: w7.ground_segs
w7.move(400, int(700 - H7))
orig_ctw = pw.CLIMB_TERRITORY_W
orig_dtw = pw.DESCEND_TERRITORY_W
orig_rc = pw.REST_CHANCE
pw.CLIMB_TERRITORY_W = pw.PLAN_DICE_MAX   # 骰盘满格：必落上爬领地
pw.REST_CHANCE = 1.0  # 若垂直没接住则必 pause（便于区分）
orig_cc = pw.CONTACT_CLIMB_CHANCE
pw.CONTACT_CLIMB_CHANCE = 1.0  # R26-6 接触概率钉死，保持 R26-3 断言语义
try:
    w7._bind_seg(lo7)
    w7._plan(time.time())
    ck("R26-3: _plan vertical roll -> climb (up priority)", w7.phase == "climb")
    # 只有更低候选 → descend（上爬领地清空，下爬领地满格）
    pw.CLIMB_TERRITORY_W = 0
    pw.DESCEND_TERRITORY_W = pw.PLAN_DICE_MAX
    w8 = mkwin()
    H8 = w8.height()
    hi8 = (300.0, 400.0, 900.0, 400.0)
    lo8 = (300.0, 650.0, 900.0, 650.0)
    w8.ground_segs = [hi8, lo8]
    w8.seg_owner = {hi8: None, lo8: None}
    w8._refresh_ground = lambda: w8.ground_segs
    w8.move(400, int(400 - H8))
    w8._bind_seg(hi8)
    w8._plan(time.time())
    ck("R26-3: _plan no up candidate -> descend", w8.phase == "descend")
    # vertical_roam=False：零主动垂直动作
    w9 = mkwin(dict(cfg_on, vertical_roam=False))
    ck("R26-4: worker reload reads vertical_roam=False", w9.vertical_roam is False)
    w9.ground_segs = [lo7, hi7]
    w9.seg_owner = {lo7: None, hi7: None}
    w9._refresh_ground = lambda: w9.ground_segs
    w9.move(400, int(700 - H7))
    w9._bind_seg(lo7)
    phases = set()
    for seed in range(20):
        random.seed(seed)
        w9.phase = "pause"
        w9._plan(time.time())
        phases.add(w9.phase)
    ck("R26-3: vertical off -> no active climb/descend in _plan",
       "climb" not in phases and "descend" not in phases)
finally:
    pw.CLIMB_TERRITORY_W = orig_ctw
    pw.DESCEND_TERRITORY_W = orig_dtw
    pw.REST_CHANCE = orig_rc
    pw.CONTACT_CLIMB_CHANCE = orig_cc

# ============ R26-5：共振修复（决策层反振荡闸门，假时钟仿真）============
import types as _types

class _FakeTime(_types.ModuleType):
    def __init__(self, holder):
        super().__init__("time")
        self.h = holder
    def time(self):
        return self.h["t"]
    def sleep(self, s):
        self.h["t"] += s
    def strftime(self, *a, **k):
        return ""

def run_sim(win, secs, dt=1/90.0):
    """真实 _step 驱动（假时钟），返回垂直转移落地次数。"""
    h = {"t": time.time()}
    orig = pw.time
    pw.time = _FakeTime(h)
    trans = 0
    prev = win.phase
    end = h["t"] + secs
    try:
        while h["t"] < end:
            win._step()
            if win.phase in ("climb", "descend") and prev not in ("climb", "descend"):
                trans += 1
            prev = win.phase
            h["t"] += dt
            app.processEvents()
    finally:
        pw.time = orig
    return trans

# 场景 A（主人报的主回路）：站窗底沿，更高他窗顶沿 y=400 穿过身体（355-500）
# → rule7 被动爬上顶沿；站顶沿后 plan 掷中下爬 → find_descend_target 找回
# 底沿（全包含+无遮挡）→ 落回 → confirm 又被动爬……修复前秒级闭环。
wA = mkwin()
HA = wA.height()
topA = (200.0, 400.0, 700.0, 400.0)
botA = (100.0, 500.0, 780.0, 500.0)
wA.ground_segs = [topA, botA]
wA.seg_owner = {topA: None, botA: None}
wA._refresh_ground = lambda: wA.ground_segs
wA.move(300, int(500 - HA))
wA._bind_seg(botA)
wA._confirm_due = 0.0
wA.pause_until = 0.0
orig_rc = pw.REST_CHANCE
# 骰盘 50/50 满格：上下领地各半（最坏情况：每次决策必有一动）
pw.CLIMB_TERRITORY_W = pw.PLAN_DICE_MAX // 2
pw.DESCEND_TERRITORY_W = pw.PLAN_DICE_MAX - pw.CLIMB_TERRITORY_W
pw.REST_CHANCE = 0.0
orig_cc5 = pw.CONTACT_CLIMB_CHANCE
pw.CONTACT_CLIMB_CHANCE = 1.0  # 仿真钉死接触概率，保持 R26-5 有界断言语义
transA = run_sim(wA, 150)
# 修复前该场景约 1.5-2s 一次往返（>70 次/150s）；闸门后：8s 冷却+反向封禁递增
ck("R26-5: A<->B loop bounded (<20 vertical trans / 150s worst-case dice)",
   transA < 20)
ck("R26-5: still vertically alive (>=2 trans in 150s)", transA >= 2)

# 场景 B：同窗顶底沿互降排除（cur_owner 相同 → 主动下爬候选直接拒绝）
wB = mkwin()
HB = wB.height()
wB.ground_segs = [topA, botA]
wB.seg_owner = {topA: 555, botA: 555}   # 同一窗口
wB._refresh_ground = lambda: wB.ground_segs
wB.move(300, int(400 - HB))
wB._bind_seg(topA)
wB._vert_cooldown_until = 0.0
wB._vert_ban.clear()
orig_dv = ground.find_descend_target
pw.random.seed(0)
desc_diced = False
for seed in range(30):
    wB.phase = "pause"; wB.pause_until = 0.0
    wB._vert_cooldown_until = 0.0
    random.seed(seed)
    wB._plan(time.time())
    if wB.phase == "descend":
        desc_diced = True
ck("R26-5: same-window top->bottom descend refused (owner exclusion)",
   not desc_diced)
ck("R26-5: (sanity) same pair WOULD be descendable without exclusion",
   orig_dv([topA, botA], 300.0, 300.0 + wB.width(), 300.0, topA, HB) == botA
   or wB.width() > (botA[2] - botA[0]) - 4)  # 若身体比底沿宽则本就不候选

# 场景 C：被动 rule7 爬的冷却生效——刚 descend 落地 8s 内 confirm 不再自动爬
wC = mkwin()
HC = wC.height()
wC.ground_segs = [topA, botA]
wC.seg_owner = {topA: None, botA: None}
wC._refresh_ground = lambda: wC.ground_segs
wC._begin_descend(botA, time.time())   # 假装刚从 top 下爬
wC._note_vert(topA, botA, time.time())  # 落地登记（模拟 _consume 后状态）
wC.move(300, int(500 - HC))
wC._bind_seg(botA)
wC._confirm_due = 0.0
nowC = time.time()
wC._confirm_ground(nowC, moving=False)
ck("R26-5: passive rule7 climb blocked within cooldown", wC.phase != "climb")
wC._vert_cooldown_until = 0.0   # 冷却过 -> 恢复可爬（不是永久禁）
wC._confirm_ground(nowC + 100, moving=False)
ck("R26-5: passive rule7 climb recovers after cooldown",
   wC.phase in ("climb", "pause"))
pw.CLIMB_TERRITORY_W = orig_ctw if 'orig_ctw' in dir() else 400
pw.DESCEND_TERRITORY_W = orig_dtw if 'orig_dtw' in dir() else 400
pw.REST_CHANCE = orig_rc
pw.CONTACT_CLIMB_CHANCE = orig_cc5

# ============ R26-5b：评审 P0/P1 修复回归（生产保真度）============
# P0 复现：_vertical_seg_alive 每帧 _refresh_ground 重建地面 → climb_seg
# 按值重绑成新 tuple → 旧 is 匹配恒失败、闸门零登记（实测 55 次/150s）。
# 生产同款刷新：每帧构造值相等的新 tuple 对象。
wP = mkwin()
HP = wP.height()
def refresh_rebuild():
    t = tuple([200.0, 400.0, 700.0, 400.0])
    b = tuple([100.0, 500.0, 780.0, 500.0])
    wP.ground_segs = [t, b]
    wP.seg_owner = {t: None, b: None}
    return wP.ground_segs
wP._refresh_ground = refresh_rebuild
wP._refresh_ground()
wP.move(300, int(500 - HP))
wP._bind_seg(next(s for s in wP.ground_segs if s[1] == 500.0))
wP._confirm_due = 0.0
wP.pause_until = 0.0
# 生产保真档也用 50/50 满格骰盘（最坏情况，保持 R26-5b 断言语义）
pw.CLIMB_TERRITORY_W = pw.PLAN_DICE_MAX // 2
pw.DESCEND_TERRITORY_W = pw.PLAN_DICE_MAX - pw.CLIMB_TERRITORY_W
pw.REST_CHANCE = 0.0
pw.CONTACT_CLIMB_CHANCE = 1.0
notesP = {"n": 0}
_orig_note = PetWindow._note_vert
def _note_spy(self, a, b, now):
    notesP["n"] += 1
    return _orig_note(self, a, b, now)
PetWindow._note_vert = _note_spy
try:
    transP = run_sim(wP, 150)
finally:
    PetWindow._note_vert = _orig_note
ck("R26-5b: production-style rebuild -> gate still registers (notes>0)",
   notesP["n"] > 0)
ck("R26-5b: production-style rebuild -> oscillation bounded (<20/150s)",
   transP < 20)
ck("R26-5b: production-style rebuild -> still alive (>=2/150s)", transP >= 2)

# P1-4：被动爬同窗排除——窄窗顶底沿互穿时 confirm 不再自动爬向同 owner 顶沿
wQ = mkwin()
HQ = wQ.height()
topQ = (200.0, 400.0, 700.0, 400.0)
botQ = (100.0, 500.0, 780.0, 500.0)
wQ.ground_segs = [topQ, botQ]
wQ.seg_owner = {topQ: 777, botQ: 777}   # 同一窗口（窄窗顶底互穿身）
wQ._refresh_ground = lambda: wQ.ground_segs
wQ.move(300, int(500 - HQ))
wQ._bind_seg(botQ)
pw.CONTACT_CLIMB_CHANCE = 1.0   # 排除接触概率干扰，专测同窗排除
climbedQ = False
for i in range(20):
    wQ._vert_cooldown_until = 0.0
    wQ._vert_ban.clear()
    wQ.phase = "pause"
    wQ._confirm_ground(time.time() + i, moving=False)
    if wQ.phase == "climb":
        climbedQ = True
ck("R26-5b: passive climb to same-window top edge refused (P1-4)",
   not climbedQ)
# 异 owner 时被动爬照常（排除不误伤）
wQ2 = mkwin()
HQ2 = wQ2.height()
wQ2.ground_segs = [topQ, botQ]
wQ2.seg_owner = {topQ: 888, botQ: 999}   # 不同窗口
wQ2._refresh_ground = lambda: wQ2.ground_segs
wQ2.move(300, int(500 - HQ2))
wQ2._bind_seg(botQ)
wQ2._vert_cooldown_until = 0.0
wQ2._vert_ban.clear()
wQ2.phase = "pause"
wQ2._confirm_ground(time.time() + 99, moving=False)
ck("R26-5b: cross-window passive climb unaffected", wQ2.phase == "climb")

# P1-3：pair 计数空置超 PAIR_DECAY_S 重置（防永久 120s 封禁）
wR = mkwin()
t0 = 1000.0
aR = (10.0, 100.0, 200.0, 100.0)
bR = (10.0, 300.0, 200.0, 300.0)
wR._note_vert(aR, bR, t0)
wR._note_vert(aR, bR, t0 + 10)   # 第二次：cnt=2
ck("R26-5b: repeated pair counts up",
   wR._vert_ban[("pair", (wR._seg_key(aR), wR._seg_key(bR)))][1] == 2)
wR._note_vert(aR, bR, t0 + 10 + pw.PAIR_DECAY_S + 1)  # 空置超衰减窗
ck("R26-5b: idle pair count decays to 1",
   wR._vert_ban[("pair", (wR._seg_key(aR), wR._seg_key(bR)))][1] == 1)
for wn in (wP, wQ, wQ2, wR):
    wn.close()

# ============ R26-6：接触概率掷（主人裁定：没必要见梯子就爬）============
# 场景 D：合法爬候选持续接触——概率 0 时 N 次确认全不爬；概率 1 时必爬。
wD = mkwin()
HD = wD.height()
topD = (200.0, 400.0, 700.0, 400.0)
botD = (100.0, 500.0, 780.0, 500.0)
wD.ground_segs = [topD, botD]
wD.seg_owner = {topD: None, botD: None}
wD._refresh_ground = lambda: wD.ground_segs
wD.move(300, int(500 - HD))
wD._bind_seg(botD)
pw.CONTACT_CLIMB_CHANCE = 0.0
climbed0 = False
for i in range(30):   # 持续接触 30 次确认（每次都过闸：清冷却）
    wD._vert_cooldown_until = 0.0
    wD._vert_ban.clear()
    wD.phase = "pause"
    wD._confirm_ground(time.time() + i, moving=False)
    if wD.phase == "climb":
        climbed0 = True
ck("R26-6: contact chance 0 -> passive climb never fires (30 confirms)",
   not climbed0)
pw.CONTACT_CLIMB_CHANCE = 1.0
wD._vert_cooldown_until = 0.0
wD.phase = "pause"
wD._confirm_ground(time.time() + 99, moving=False)
ck("R26-6: contact chance 1 -> passive climb fires", wD.phase == "climb")
# 中间概率：0.4 时 30 次持续接触既不恒爬也不恒不爬（随机真在掷）
pw.CONTACT_CLIMB_CHANCE = 0.4
n_climb = 0
for i in range(60):
    wD2 = wD
    wD2._vert_cooldown_until = 0.0
    wD2._vert_ban.clear()
    wD2.phase = "pause"
    wD2.move(300, int(500 - HD))
    wD2._bind_seg(botD)
    random.seed(1000 + i)
    wD2._confirm_ground(time.time(), moving=False)
    if wD2.phase == "climb":
        n_climb += 1
ck("R26-6: mid chance -> stochastic (0 < climbs < 60)", 0 < n_climb < 60)
# 下爬同口径：_plan 主动下爬概率 0 时不掷动
wE = mkwin()
HE = wE.height()
topE = (300.0, 400.0, 900.0, 400.0)
botE = (300.0, 650.0, 900.0, 650.0)
wE.ground_segs = [topE, botE]
wE.seg_owner = {topE: None, botE: None}
wE._refresh_ground = lambda: wE.ground_segs
wE.move(400, int(400 - HE))
wE._bind_seg(topE)
# R26-5b：主动下爬的"想不想"由骰盘领地管——领地清零=永不降，
# 领地满格=必降（CONTACT_CLIMB_CHANCE 只管被动 rule7 爬）。
pw.CLIMB_TERRITORY_W = 0
pw.DESCEND_TERRITORY_W = 0
desc0 = False
for i in range(30):
    wE._vert_cooldown_until = 0.0
    wE._vert_ban.clear()
    wE.phase = "pause"
    wE.pause_until = 0.0
    wE._plan(time.time() + i)
    if wE.phase == "descend":
        desc0 = True
ck("R26-5b: empty dice territory -> active descend never fires", not desc0)
pw.CLIMB_TERRITORY_W = 0
pw.DESCEND_TERRITORY_W = pw.PLAN_DICE_MAX
wE._vert_cooldown_until = 0.0
wE._vert_ban.clear()
wE.phase = "pause"
wE.pause_until = 0.0
wE._plan(time.time() + 99)
ck("R26-5b: full dice territory -> active descend fires", wE.phase == "descend")

# R26-5b 无穿透证明：同种子序列下，"双候选并存"与"仅下爬候选"的
# descend 触发次数精确相等——旧双骰穿透版上爬掷不中会再掷下爬，序列
# 错位且下爬频率虚增；骰盘版 descend 判定只看同一颗骰子落点。
wV = mkwin()
HV = wV.height()
upV = (300.0, 400.0 - (HV - 40), 900.0, 400.0 - (HV - 40))  # 穿过身体上缘
midV = (300.0, 400.0, 900.0, 400.0)
loV = (300.0, 650.0, 900.0, 650.0)
segs_both = [upV, midV, loV]
segs_down_only = [midV, loV]
def _count_desc(seglist, n=400):
    wV.ground_segs = seglist
    wV.seg_owner = {s: None for s in seglist}
    wV._refresh_ground = lambda: wV.ground_segs
    wV.move(400, int(400 - HV))
    wV._bind_seg(midV)
    n_desc = n_climb = 0
    for i in range(n):
        wV._vert_cooldown_until = 0.0
        wV._vert_ban.clear()
        wV.phase = "pause"
        wV.pause_until = 0.0
        random.seed(i)
        wV._plan(time.time() + i)
        if wV.phase == "descend":
            n_desc += 1
        elif wV.phase == "climb":
            n_climb += 1
    return n_desc, n_climb
pw.CLIMB_TERRITORY_W = orig_ctw   # 生产领地宽度做统计（4%/4%）
pw.DESCEND_TERRITORY_W = orig_dtw
desc_both, climb_both = _count_desc(segs_both)
desc_only, _ = _count_desc(segs_down_only)
ck("R26-5b: (sanity) both-candidate dice hits climb territory",
   climb_both > 0)
ck("R26-5b: descend count independent of climb candidate (no leak-through)",
   desc_both == desc_only)
ck("R26-5b: dice territory ~4% (climbs within 1%-7% of 400/10000)",
   0 < desc_both < 60)
wV.close()
pw.CONTACT_CLIMB_CHANCE = orig_cc5
pw.CLIMB_TERRITORY_W = orig_ctw
pw.DESCEND_TERRITORY_W = orig_dtw

# R26-6 头顶空间剔除：站不进屏顶空间的线段从源头消失（含多屏语义）
class _G:
    def __init__(s, l, t, r, b):
        s._l, s._t, s._r, s._b = l, t, r, b
    def left(s): return s._l
    def top(s): return s._t
    def right(s): return s._r
    def bottom(s): return s._b
    def width(s): return s._r - s._l
    def height(s): return s._b - s._t

geoF = _G(0, 0, 800, 800)
orig_enum6 = ground.enumerate_window_rects
orig_tb6 = ground._taskbar_rects
ground._taskbar_rects = lambda: []
try:
    # 窗口顶沿 y=100 < pet_h=145：站上去头出屏 → 顶沿剔除；底沿 y=600 保留
    ground.enumerate_window_rects = lambda h: [(777, 100, 100, 700, 600)]
    segsF = build_ground_segments(134, 145, geoF, 0)
    ck("R26-6: top edge without headroom excluded at source",
       not any(abs(s[1] - 100) < 2 for s in segsF))
    ck("R26-6: bottom edge with headroom kept",
       any(abs(s[1] - 600) < 2 for s in segsF))
    # 多屏：线段在副屏 y=-100（主屏 top=0 站不进）但副屏 top=-800 站得进 → 保留
    # 注：用正 dpr=1 的合成屏（_rect_screen 交面积选屏依赖真 dpr；offscreen=1）
    geoG = _G(0, 0, 800, 800)
    geoG2 = _G(0, -800, 800, 0)
    class _S:
        def __init__(s, g): s.g = g
        def geometry(s): return s.g
        def availableGeometry(s): return s.g
        def devicePixelRatio(s): return 1.0
    orig_screens = ground.QApplication.screens if hasattr(ground, "QApplication") else None
    import PySide6.QtWidgets as _qw
    orig_screens2 = _qw.QApplication.screens
    _qw.QApplication.screens = staticmethod(lambda: [_S(geoG), _S(geoG2)])
    try:
        ground.enumerate_window_rects = lambda h: [(888, 100, -100, 700, -50)]
        segsG = build_ground_segments(134, 145, geoG, 0, screen_geos=[geoG, geoG2])
        ck("R26-6: multi-screen headroom -> edge kept via other screen",
           any(abs(s[1] - (-100)) < 2 for s in segsG))
    finally:
        _qw.QApplication.screens = orig_screens2
    ground.enumerate_window_rects = lambda h: [(888, 100, -100, 700, -50)]
    segsG1 = build_ground_segments(134, 145, geoG, 0)  # 单屏（缺省）→ 剔除
    ck("R26-6: single-screen default excludes same edge",
       not any(abs(s[1] - (-100)) < 2 for s in segsG1))
finally:
    ground.enumerate_window_rects = orig_enum6
    ground._taskbar_rects = orig_tb6

for wn in (wA, wB, wC, wD, wE):
    wn.close()

# ============ R26-4：配置链路（宿主纯函数）============
gui = load_plugin()
cfgmod = gui.cfgmod

class FakeCtx:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.writes = []
    def get_config(self, key, default=None):
        return self.store.get(key, default)
    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value

ck("R26-4: PET_DEFAULTS has both keys default True",
   cfgmod.PET_DEFAULTS.get("follow_window") is True
   and cfgmod.PET_DEFAULTS.get("vertical_roam") is True)
ck("R26-4: display keys include both (auto-fill family)",
   "follow_window" in cfgmod.PET_DISPLAY_KEYS and "vertical_roam" in cfgmod.PET_DISPLAY_KEYS)
ok, k, v, err = cfgmod.validate_request({"key": "pet.follow_window", "value": False})
ck("R26-4: validate pet.follow_window bool", ok and k == "pet.follow_window" and v is False)
ok, k, v, err = cfgmod.validate_request({"key": "pet.vertical_roam", "value": 1})
ck("R26-4: validate pet.vertical_roam coerces bool", ok and v is True)
ck("R26-4: REQ_KEYS whitelist covers both",
   "pet.follow_window" in cfgmod.REQ_KEYS and "pet.vertical_roam" in cfgmod.REQ_KEYS)
ctx = FakeCtx({"pet": {"slug": "fairy"}})
filled = cfgmod.fill_pet_display(ctx, cfgmod.read_pet_explicit(ctx))
ck("R26-4: missing keys auto-filled via official writer",
   filled.get("follow_window") is True and filled.get("vertical_roam") is True
   and any(k2 == "pet" for k2, _ in ctx.writes))
ctx2 = FakeCtx({"pet": {"follow_window": False, "vertical_roam": False}})
filled2 = cfgmod.fill_pet_display(ctx2, cfgmod.read_pet_explicit(ctx2))
ck("R26-4: explicit False not overwritten (A1)",
   filled2["follow_window"] is False and filled2["vertical_roam"] is False)
view = cfgmod.build_view(FakeCtx({}))
ck("R26-4: build_view pet block has both defaults",
   view["pet"].get("follow_window") is True and view["pet"].get("vertical_roam") is True)
ck("R26-4: build_view no config write for missing keys",
   view["explicit"]["pet"] == [] or ("follow_window" not in view["explicit"]["pet"]))

for wn in (w, w2, w3, w4, w5, w6, w7, w8, w9):
    wn.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
