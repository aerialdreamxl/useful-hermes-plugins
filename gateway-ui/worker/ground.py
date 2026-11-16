# -*- coding: utf-8 -*-
"""gateway-ui worker 地面系统 —— 地面系统作者七条规则线段制（CONTRACT §7）。

地面数据 = [(x1,y1,x2,y2), ...] 线段列表（Qt 逻辑像素，一套 x/y=桌面一点，
两点成线段）。判定语义原样移植旧 pet_window.py 十八~菜单驻留调试成果，不改判定：
  rule6 子线段判定 seg_contains_x（±2px 容差）/ find_support（±GROUND_STAND_EPS）
  rule6 自由落体 find_fall_target（正下方第一条）
  rule7 攀爬 find_climb_target（更高+穿过身体+全包含，菜单驻留 BUG2）
Win32 枚举剔除规则与 z 序遮挡剔除照旧；头顶空间过滤（菜单驻留 BUG1）照旧。
规则2：整个系统只在游走开启时被调用（调用点在 pet_window._step）。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# ---- 物理常量（mirror 旧 pet_window.py / use-pet-roam.ts）----
GROUND_STAND_EPS = 3.0  # 站立对齐容差（底边 y 与线段 y；无 FEET_DROP_PX 偏移）
CONTAIN_TOL = 2.0       # rule6 子线段 x 容差 ±2px


def seg_contains_x(seg, x1, x2):
    """规则6：线段 (x1..x2) 是否为 seg 的子线段（含 2px 容差）。"""
    return seg[0] - CONTAIN_TOL <= x1 and x2 <= seg[2] + CONTAIN_TOL


def find_support(segs, x1, x2, bottom_y):
    """规则6 第一步：桌宠下边界线段 (x1,bottom_y)-(x2,bottom_y) 已经是某条
    地面线段的子线段且 y 对齐 → 返回该线段（不用往下掉）。"""
    best = None
    for sg in segs:
        if seg_contains_x(sg, x1, x2) and abs(sg[1] - bottom_y) <= GROUND_STAND_EPS:
            if best is None or sg[1] < best[1]:
                best = sg
    return best


def find_fall_target(segs, x1, x2, bottom_y):
    """规则6 第二步：不在任何地面上 → 找正下方第一条地面线段（其 y 在
    bottom_y 之下、x 区间覆盖桌宠底部线段），自由落体到该线段。"""
    best = None
    for sg in segs:
        if sg[1] <= bottom_y + GROUND_STAND_EPS:
            continue
        if seg_contains_x(sg, x1, x2):
            if best is None or sg[1] < best[1]:
                best = sg
    return best


def find_climb_target(segs, x1, y1, x2, y2, standing_seg):
    """规则7（自由落体判定之后执行）：某条非当前站立、比现在高的地面线段
    穿过桌宠窗口矩形 → 向上攀爬至这条线段。返回 seg 或 None。
    "更高"=线段 y 小于当前底边 y（屏幕坐标 y 向下）；"穿过"=线段 y 落在
    窗口 (y1, y2) 开区间。菜单驻留 BUG2：必须全包含（seg_contains_x）——
    部分重叠处爬上去站不稳 → 落回 → 再爬 = 震荡循环。取最高（y 最小）一条。"""
    best = None
    for sg in segs:
        if sg is standing_seg:
            continue
        sy = sg[1]
        if sy >= y2 - GROUND_STAND_EPS:  # 不高于当前站立面
            continue
        if not (y1 < sy < y2):            # 未穿过窗口矩形
            continue
        if not seg_contains_x(sg, x1, x2):
            continue
        if best is None or sy < best[1]:
            best = sg
    return best


def _to_logical_point(px, py):
    """物理像素点 → (逻辑点, screen, dpr)；按包含该点的屏 dpr 换算。"""
    from PySide6.QtWidgets import QApplication
    for scr in QApplication.screens():
        g = scr.geometry()
        d = scr.devicePixelRatio() or 1.0
        gx1, gy1 = g.left() * d, g.top() * d
        gx2, gy2 = gx1 + g.width() * d, gy1 + g.height() * d
        if gx1 <= px <= gx2 and gy1 <= py <= gy2:
            return (px / d, py / d), scr, d
    return None, None, None


def _rect_screen(pl, pt, pr, pb):
    """R26-1：物理矩形 → 交面积最大的屏 (screen, dpr)。最大化窗口左上角
    带负坐标隐形边框（实测 -7,-7），旧版拿左上角点选屏会整窗丢弃 → 不参与
    遮挡剔除 → 被盖窗口的沿照旧注册（浮空 BUG 根因）。改矩形求交后负原点
    窗口正常收录；完全无交的窗口返回 (None, None)。"""
    from PySide6.QtWidgets import QApplication
    best, best_d, best_area = None, None, 0.0
    for scr in QApplication.screens():
        g = scr.geometry()
        d = scr.devicePixelRatio() or 1.0
        gx1, gy1 = g.left() * d, g.top() * d
        gx2, gy2 = gx1 + g.width() * d, gy1 + g.height() * d
        iw = min(pr, gx2) - max(pl, gx1)
        ih = min(pb, gy2) - max(pt, gy1)
        if iw > 0 and ih > 0 and iw * ih > best_area:
            best, best_d, best_area = scr, d, iw * ih
    return best, best_d


def window_logical_rect(pl, pt, pr, pb):
    """R26-2：窗口物理矩形 → 逻辑矩形 (l, t, r, b)（按交面积最大屏的 dpr
    整体换算，不裁剪——跟随需要原始 dx/dy）。无交屏返回 None。"""
    scr, d = _rect_screen(pl, pt, pr, pb)
    if scr is None:
        return None
    return (pl / d, pt / d, pr / d, pb / d)


def owner_window_rect(hwnd):
    """R26-2：单个窗口物理矩形（O(1) 跟随查询）。窗口不存在/最小化/不可见/
    cloaked → None（失效链：转自由落体）。"""
    if os.name != "nt" or not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            return None
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        cloaked = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, 14, ctypes.byref(cloaked), 4)
        if cloaked.value:
            return None
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _taskbar_rects():
    """Win32 任务栏矩形（物理像素）。Shell_TrayWnd 主任务栏 + 副屏任务栏。"""
    out = []
    try:
        user32 = ctypes.windll.user32
        for clsname in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            h = user32.FindWindowW(clsname, None)
            if h:
                r = wintypes.RECT()
                if user32.GetWindowRect(h, ctypes.byref(r)):
                    out.append((r.left, r.top, r.right, r.bottom))
    except Exception:
        pass
    return out


def enumerate_window_rects(self_hwnd):
    """规则3：OS 全量窗口 → 剔除 最小化/不可见/cloaked(非本桌面、挂起 UWP)/
    桌宠自己(pid+HWND)/owned 子窗(GW_OWNER)/非根窗(GA_ROOT)/桌面壳类/太小，
    返回 [(hwnd, l, t, r, b), ...]，顺序 = EnumWindows 序（顶→底 Z-order，
    规则4 遮挡判定依赖此序）。实测 0.23ms/次（399 窗口→5 有效）。"""
    out = []
    if os.name != "nt":
        return out
    try:
        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi
        hwnds = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(h, _l):
            hwnds.append(h)
            return True

        user32.EnumWindows(cb, 0)
        pid = wintypes.DWORD(0)
        cloaked = ctypes.c_int(0)
        rect = wintypes.RECT()
        cls = ctypes.create_unicode_buffer(256)
        mypid = os.getpid()
        for h in hwnds:
            try:
                if not h or h == self_hwnd:
                    continue
                if not user32.IsWindowVisible(h) or user32.IsIconic(h):
                    continue
                # 非本桌面/挂起 UWP/被 cloak 的窗口（虚拟桌面 COM 在 26H1
                # E_NOINTERFACE 不可用，cloaked 是实测可得的等效过滤）
                cloaked.value = 0
                dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)
                if cloaked.value:
                    continue
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if pid.value == mypid:
                    continue
                if user32.GetWindow(h, 4):         # GW_OWNER：owned 弹窗
                    continue
                if user32.GetAncestor(h, 2) != h:  # GA_ROOT：非根=子窗口
                    continue
                user32.GetClassNameW(h, cls, 256)
                if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd",
                                 "Shell_SecondaryTrayWnd", "Button",
                                 "tooltips_class32", "MultitaskingViewFrame"):
                    continue
                if not user32.GetWindowRect(h, ctypes.byref(rect)):
                    continue
                w, hh = rect.right - rect.left, rect.bottom - rect.top
                if w < 60 or hh < 40:
                    continue
                out.append((h, rect.left, rect.top, rect.right, rect.bottom))
            except Exception:
                continue
    except Exception:
        pass
    return out


def build_ground_segments(pet_w, pet_h, screen_geo, self_hwnd, with_owners=False,
                          screen_geos=None):
    """规则3+4：产出地面线段列表 [(x1,y1,x2,y2), ...]（Qt 逻辑像素）。
    每窗口出上边界(顶沿)与下边界(底沿)两条线段 + 头顶空间过滤（菜单驻留 BUG1：
    seg_y - pet_h >= 该屏 top）；遮挡剔除：线段侵入 z 序更高窗口矩形内即无效
    （EnumWindows 序=Z 序）；最后并入屏幕下界 + 任务栏上界（同款头顶过滤），
    覆写为最终列表。规则2：只在游走开启时被调用。
    R26-1：选屏改矩形求交（负原点最大化窗口不再被丢，遮挡剔除恢复）。
    R26-2：with_owners=True 返回 (final, owner_map)——owner_map[seg]=来源窗
    HWND（屏幕下界/任务栏上界=None）；默认 False 返回形状与旧版全等。
    R26-6：screen_geos=全部屏幕几何列表（缺省 [screen_geo]）——头顶空间
    过滤对任意屏成立即保留（主人裁定：爬上去站不进屏顶空间的线段剔除；
    单屏调用行为与旧版全等）。"""
    segs = []           # (x1, y1, x2, y2, zrank, hwnd)
    win_rects = []      # (zrank, l, t, r, b) 逻辑像素，用于遮挡判定
    rects = enumerate_window_rects(self_hwnd)
    taskbars = _taskbar_rects()
    tb_set = set()
    for (l, t, r, b) in taskbars:
        tb_set.add((l, t, r, b))

    for zrank, (h, pl, pt, pr, pb) in enumerate(rects):
        is_taskbar = (pl, pt, pr, pb) in tb_set
        scr, d = _rect_screen(pl, pt, pr, pb)
        if scr is None:
            continue
        # 同一窗口四角同屏近似：用交面积最大屏的 dpr 换算整个矩形
        l, t, r, b = pl / d, pt / d, pr / d, pb / d
        g = scr.availableGeometry() if not is_taskbar else scr.geometry()
        # 裁剪到该屏
        l = max(float(g.left()), min(l, float(g.right())))
        r = max(float(g.left()), min(r, float(g.right())))
        t = max(float(g.top()), min(t, float(g.bottom())))
        b = max(float(g.top()), min(b, float(g.bottom())))
        if r - l < 20 or b - t < 10:
            continue
        win_rects.append((zrank, l, t, r, b))
        if is_taskbar:
            continue  # 任务栏自身的沿不参与，最后统一加"任务栏上界"
        # 规则3：左上/右下两点 → 上边界、下边界
        # 菜单驻留 BUG1 + R26-6：站上该线后宠物整体须在某块屏内
        # （seg_y - pet_h >= 屏 top 且 x 域在该屏内）——站不进去的线段
        # 从源头剔除，爬/跳都不会选到它。R26-6 多屏：任一屏成立即保留。
        geos = screen_geos if screen_geos else [g]

        def _standable(seg_x1, seg_y, seg_x2):
            for gg in geos:
                gl, gt, gr = float(gg.left()), float(gg.top()), float(gg.right())
                if gt + pet_h - 1 <= seg_y \
                        and seg_x2 - pet_w >= gl - CONTAIN_TOL \
                        and seg_x1 + pet_w <= gr + CONTAIN_TOL:
                    return True
            return False

        if _standable(l, t, r):
            segs.append((l, t, r, t, zrank, h))   # top edge
        if _standable(l, b, r):
            segs.append((l, b, r, b, zrank, h))   # bottom edge
        # R26-6：屏顶裁剪后 y<pet_h 的负原点顶沿同样按站得进判（用裁剪前 t）
        if scr is not None and pt < 0 and t != pt / d:
            lt = pt / d
            if _standable(l, lt, r):
                segs.append((l, lt, r, lt, zrank, h))

    # 规则4：遮挡剔除——线段侵入 z 序更高（zrank 更小=更靠前）窗口的矩形内
    def occluded(sg):
        x1, y1, x2, _y2, z = sg[0], sg[1], sg[2], sg[3], sg[4]
        for (wz, wl, wt, wr, wb) in win_rects:
            if wz >= z:
                continue  # 只看层级更高（更靠前）的窗口
            # 线段与高窗矩形相交（线段 y 在矩形内，x 区间有重叠）
            if wt - 1 <= y1 <= wb + 1 and x2 > wl + 1 and x1 < wr - 1:
                return True
        return False

    kept = [sg for sg in segs if not occluded(sg)]

    # 规则4 收尾：屏幕下界 + 任务栏上界线段并入，覆写为最终列表
    final = []
    owner_map = {}
    seen = set()

    def add(x1, y1, x2, y2, owner=None):
        x1, x2 = min(x1, x2), max(x1, x2)
        if x2 - x1 < 4:
            return
        key = (round(x1, 1), round(y1, 1), round(x2, 1))
        if key in seen:
            return
        seen.add(key)
        sg = (x1, y1, x2, y2)
        final.append(sg)
        owner_map[sg] = owner

    for (pl, pt, pr, pb) in taskbars:
        # N16：与窗口路径同口径——矩形求交选屏（R26-1），再按该屏 dpr 整体
        # 换算。旧版拿左上角点换算，负原点/跨屏任务栏会被整条丢弃。
        scr, d = _rect_screen(pl, pt, pr, pb)
        if scr is None:
            continue
        l, t, r, b = pl / d, pt / d, pr / d, pb / d
        tb_g = scr.availableGeometry()
        if float(tb_g.top()) + pet_h - 1 <= t:
            add(l, t, r, t)  # 任务栏上界（BUG1 同款头顶空间过滤）

    g = screen_geo
    add(float(g.left()), float(g.bottom()), float(g.right()), float(g.bottom()))
    for sg in kept:
        add(sg[0], sg[1], sg[2], sg[3], owner=sg[5])
    if with_owners:
        return final, owner_map
    return final


def find_descend_target(segs, x1, x2, bottom_y, standing_seg, pet_h):
    """R26-3（rule7 的向下对称）：非站立、更低（y 更大）、x 全包含
    （BUG2 同口径），且下降路径无遮挡——standing_y 与目标 y 之间不存在
    其他线段横在身体 x 域内（有则先落那条，不穿地）。取最近（y 最小）一条。
    目标须容得下宠物身高（bottom_y..目标 y 的落差 ≥ 0 即可站，头顶空间由
    注册时的 BUG1 过滤保证）。返回 seg 或 None。"""
    best = None
    for sg in segs:
        if sg is standing_seg:
            continue
        sy = sg[1]
        if sy <= bottom_y + GROUND_STAND_EPS:  # 不低于当前站立面
            continue
        if not seg_contains_x(sg, x1, x2):
            continue
        # 下降路径遮挡：中间横着别的线段 → 那条更近，本轮不选远的
        blocked = False
        for other in segs:
            if other is sg or other is standing_seg:
                continue
            oy = other[1]
            if bottom_y + GROUND_STAND_EPS < oy < sy - GROUND_STAND_EPS \
                    and seg_contains_x(other, x1, x2):
                blocked = True
                break
        if blocked:
            continue
        if best is None or sy < best[1]:
            best = sg
    return best


def find_adjacent_segment(segs, standing_seg, pet_w, pet_h, dir_hint):
    """R26-3 跨线段寻路候选：端点外侧 x 邻接（间隙 ≤ pet_w）、高差 ≤ 2×pet_h
    的线段。dir_hint=+1 找右邻 / -1 找左邻。返回 (seg, target_x) 或 None——
    target_x 已夹进目标可行域 [x1, x2-pet_w]（BUG3 纪律）。同高/更高/更低
    都可返回，调用方按高度差选 climb/descend/平移管线。"""
    if standing_seg is None:
        return None
    sx1, sy, sx2 = standing_seg[0], standing_seg[1], standing_seg[2]
    best = None
    for sg in segs:
        if sg is standing_seg:
            continue
        if abs(sg[1] - sy) > 2.0 * pet_h:
            continue
        if sg[2] - sg[0] < pet_w:  # 目标站不下整个身体
            continue
        if dir_hint > 0:
            gap = sg[0] - sx2
            tx = sg[0]
        else:
            gap = sx1 - sg[2]
            tx = sg[2] - pet_w
        if gap < -CONTAIN_TOL or gap > pet_w:
            continue
        tx = max(sg[0], min(tx, sg[2] - pet_w))
        if best is None or abs(sg[1] - sy) < abs(best[0][1] - sy):
            best = (sg, tx)
    return best
