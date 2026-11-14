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


def build_ground_segments(pet_w, pet_h, screen_geo, self_hwnd):
    """规则3+4：产出地面线段列表 [(x1,y1,x2,y2), ...]（Qt 逻辑像素）。
    每窗口出上边界(顶沿)与下边界(底沿)两条线段 + 头顶空间过滤（菜单驻留 BUG1：
    seg_y - pet_h >= 该屏 top）；遮挡剔除：线段侵入 z 序更高窗口矩形内即无效
    （EnumWindows 序=Z 序）；最后并入屏幕下界 + 任务栏上界（同款头顶过滤），
    覆写为最终列表。规则2：只在游走开启时被调用。"""
    segs = []           # (x1, y1, x2, y2, zrank)
    win_rects = []      # (zrank, l, t, r, b) 逻辑像素，用于遮挡判定
    rects = enumerate_window_rects(self_hwnd)
    taskbars = _taskbar_rects()
    tb_set = set()
    for (l, t, r, b) in taskbars:
        tb_set.add((l, t, r, b))

    for zrank, (h, pl, pt, pr, pb) in enumerate(rects):
        is_taskbar = (pl, pt, pr, pb) in tb_set
        conv, scr, d = _to_logical_point(pl, pt)
        if conv is None:
            continue
        # 同一窗口四角同屏近似：用左上角所在屏的 dpr 换算整个矩形
        l, t = conv
        r, b = pr / d, pb / d
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
        # 菜单驻留 BUG1：站上该线后宠物整体须在屏内（seg_y - pet_h >= 屏 top）
        top_ok = float(g.top()) + pet_h - 1 <= t
        bot_ok = float(g.top()) + pet_h - 1 <= b
        if top_ok:
            segs.append((l, t, r, t, zrank))   # top edge
        if bot_ok:
            segs.append((l, b, r, b, zrank))   # bottom edge

    # 规则4：遮挡剔除——线段侵入 z 序更高（zrank 更小=更靠前）窗口的矩形内
    def occluded(sg):
        x1, y1, x2, _y2, z = sg
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
    seen = set()

    def add(x1, y1, x2, y2):
        x1, x2 = min(x1, x2), max(x1, x2)
        if x2 - x1 < 4:
            return
        key = (round(x1, 1), round(y1, 1), round(x2, 1))
        if key in seen:
            return
        seen.add(key)
        final.append((x1, y1, x2, y2))

    for (pl, pt, pr, pb) in taskbars:
        conv, scr, d = _to_logical_point(pl, pt)
        if conv is None:
            continue
        l, t = conv
        r = pr / d
        g = scr.availableGeometry()
        if float(g.top()) + pet_h - 1 <= t:
            add(l, t, r, t)  # 任务栏上界（BUG1 同款头顶空间过滤）

    g = screen_geo
    add(float(g.left()), float(g.bottom()), float(g.right()), float(g.bottom()))
    for sg in kept:
        add(sg[0], sg[1], sg[2], sg[3])
    return final
