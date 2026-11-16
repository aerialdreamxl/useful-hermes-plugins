# -*- coding: utf-8 -*-
"""gateway-ui worker 桌宠浮窗 —— PetWindow（CONTRACT §7）。

透明浮窗（FramelessWindowHint|WindowStaysOnTopHint(topmost 可关)|Tool，
WA_TranslucentBackground），petdex spritesheet 渲染（sprites.py），
地面系统作者七条规则物理状态机 pause/walk/fall/climb（ground.py 几何，
判定语义原样移植十八~菜单驻留调试成果）：
  rule2 关游走=零物理普通浮窗；rule5 确认计时器 0.75-1.25s 随机
  （拖动归零暂停、移动归零但每帧实时判地面、与 Agent 忙闲无关）；
  rule6 自由落体 gravity=5200px/s²（子线段±2px 判定不掉，否则落正下方第一条）；
  rule7 攀爬在落体判定后（更高+穿过身体+全包含，CLIMB_DUR_S=0.22 线性插值无瞬移）；
  站立=底边贴线 GROUND_STAND_EPS=3（无 FEET_DROP_PX 偏移）；
  漫步目标夹 [x1, x2-pet_w]；无落点先夹 x 回屏内再落。
屏内出生：新建窗口一律屏内右下角地面（不读 pet_pos.json）；resize 路径
（old_h>0）沿用存档+高度差微调，双向 clamp。拖拽/游走落点原子写 pet_pos.json。
host 状态映射：idle→idle listening→waiting thinking→review acting/busy→run
happy→wave error→failed；fall/climb→jumping 行；walk→方向跑行。
at_rest = active_sessions==0；忙时掐断 walk 转 pause 演 host 动作。
verbose 时 PET 前缀写 bus/pet_debug.log（400KB 滚动 2000 行）。
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QPainter, QCursor, QTransform
from PySide6.QtWidgets import QWidget, QApplication

import sprites
from sprites import HOST_MAP, PetSheet, installed_pets, sheet_path, row_index, clamp_scale
import ground
from ground import (seg_contains_x, find_support, find_fall_target,
                    find_climb_target, find_descend_target,
                    find_adjacent_segment, build_ground_segments,
                    owner_window_rect, window_logical_rect,
                    GROUND_STAND_EPS)
from pet_menu import PetMenu

# ---- 节奏（mirror roam-behavior.ts）----
PAUSE_DWELL = {"mean": 4.2, "min": 1.5, "max": 13.0}
REST_CHANCE = 0.62
STROLL_MIN_FRACTION = 0.45
STROLL_MIN_PX = 110
STROLL_TOWARD_ROOM = 0.85
# ---- 物理（mirror use-pet-roam.ts / roam-geometry.ts）----
STRIDE_PER_LOOP = 0.8
GRAVITY_PX_S2 = 5200.0
ARRIVE_EPS = 1.5
MAX_DT = 0.05
# ---- 地面系统（作者七条规则）----
CONFIRM_MIN_S = 0.75   # rule5：地面确认计时器下限
CONFIRM_MAX_S = 1.25   # rule5：上限（每次随机取 [min,max]）
CLIMB_DUR_S = 0.22     # 攀爬动画时长（平滑上移，杜绝瞬移）
# ---- 地面物理增强 R26（PROMPT §11）----
# R26-5b（主人裁定）：主动垂直改大数轴骰盘——一次掷骰，落在哪个动作的
# 领地就做什么，彻底消除旧版双骰穿透（上爬掷不中再掷下爬，两候选并存时
# 实际概率 26.6% vs 名义 14%）。数轴 [0, 10000)：上爬领地 [0,400)、
# 下爬领地 [400,800)、其余 9200 格=临时起意没想法。领地只在候选存在时
# 生效；候选无则整段跳过不掷骰。闸门（冷却/封禁/同窗）仍管"能不能"，
# 骰盘只管"想不想"——落中领地但被闸拦=本轮没戏，不重掷不穿透。
PLAN_DICE_MAX = 10000     # 大数轴宽度
CLIMB_TERRITORY_W = 400   # 上爬领地宽（4%）
DESCEND_TERRITORY_W = 400 # 下爬领地宽（4%）
ADJACENT_CHANCE = 0.5    # R26-3：漫步抵端点后跨线段跳转概率
DESCEND_PX_S = 600.0     # R26-3：下爬速度（时长=距离/速度，下限 CLIMB_DUR_S）
FOLLOW_SAVE_MIN_S = 0.5  # R26-2：跟随落点写盘节流（硬件友好）
# R26-5（共振修复）：垂直转移记忆——上爬/下爬/端点跳共用一套反振荡闸门。
# 共振回路实锤：站窗底→rule7 被动爬上窗顶→plan 掷下爬回窗底→再被动爬……
# 加上端点 hop 左右往返，构成秒级闭环。修复：
VERT_COOLDOWN_S = 8.0    # 任一垂直转移落地后的全局冷却（期间禁一切主动/被动垂直动作）
REVERSE_BAN_S = 30.0     # 同一对线段再次转移的封禁基数（每重复一次×倍数）
REVERSE_BAN_MAX_MULT = 4 # 封禁倍数上限（同一对最多 120s 一振）
PAIR_DECAY_S = 300.0     # R26-5b（评审 P1-3）：线段对计数衰减窗——空置超此值重置，
                         # 防"pair 条目只增不减 → 数月后全库永久 120s 封禁"
# R26-6（主人裁定：没必要见梯子就爬）：接触概率掷——每次确认到合法爬/降
# 候选（接触/持续接触均算一次掷）只有 CONTACT_CLIMB_CHANCE 概率真动手，
# 掷不中就安静站着（下次确认再掷）。与 R26-5 闸门叠加：闸门管"能不能"，
# 概率管"想不想"。仅 vertical_roam=true 生效；false 保持旧行为必爬。
CONTACT_CLIMB_CHANCE = 0.4
# ---- 余额气泡（BalanceBubble 单击门槛，spike 实测定值）----
CLICK_MAX_PX = 6       # press→release 位移 manhattanLength ≤6 判单击，>6 拖拽

VERBOSE = False        # worker tick 从 state.json.verbose 同步

# 事件反应 play_reaction 通用反应映射：kind → 精灵行状态名（直接走 row_index）。
# done→happy→wave 行、failed→failed 行、await_input→wave 行（HOST_MAP 无 wave
# 键，故这里直接给精灵行名，语义等价）。本轮只加 API，不接任何事件源。
REACTION_MAP = {"done": "wave", "failed": "failed", "await_input": "wave"}


class PetWindow(QWidget):
    def __init__(self, meta, bus_dir, get_cfg, set_cfg=None, pet_menu=None,
                 on_click=None):
        super().__init__()
        self.meta = meta
        self.bus = Path(bus_dir)
        self.get_cfg = get_cfg
        self.set_cfg_cb = set_cfg
        self.pet_menu = pet_menu
        # 余额气泡：左键单击回调（press→release 位移 ≤CLICK_MAX_PX 触发）。
        # None=旧版行为完全一致；新参数在末尾 keyword，不破坏既有位置参数。
        self.on_click = on_click
        self._press_pos = None
        self._drag_started = False
        self.pos_file = self.bus / "pet_pos.json"
        self.sheet = None
        self.slug = None
        self.host_state = "idle"
        self.at_rest = True
        # 气泡聚焦增强：余额气泡聚焦态（main._sync_bubble 每拍按气泡可见性置位）。
        # True=气泡开着 → 掐断游走钉在原地，演 review 行（同模型思考动画）。
        self.bubble_focus = False
        # 平台跳跃 state machine（mirror use-pet-roam.ts）
        self.phase = "pause"
        self.pause_until = time.time() + random.uniform(0.4, 1.2)
        self.walk_target = 0.0
        self.dir = 0
        self.last = time.time()
        self.frame = 0
        self.last_step = time.time()
        self.mirror = False
        self.cur_row = -1
        self._frames = []
        # 地面系统（作者七条规则）：线段制地面系统
        self.ground_segs = []        # rule1：[(x1,y1,x2,y2),...] 随主循环实时刷新
        self.cur_seg = None          # 当前站立线段
        # R26-2：线段归属与窗口跟随（follow_window 默认 true，reload 读配置）
        self.seg_owner = {}          # seg(4元组) -> 来源窗 HWND（屏底/任务栏=None）
        self.cur_owner = None        # 当前站立线段的来源窗 HWND
        self._owner_rect = None      # 上次跟随基准（逻辑矩形 l,t,r,b）
        self._follow_save_ts = 0.0   # 跟随落点写盘节流
        self.follow_window = True
        # R26-3：垂直游走（vertical_roam 默认 true，reload 读配置）
        self.vertical_roam = True
        self.descend_dur = CLIMB_DUR_S   # 本次下爬动画时长（随距离线性）
        # R26-5：共振修复状态——垂直转移记忆（冷却 + 有向对封禁倍数）
        self._vert_cooldown_until = 0.0      # 全局冷却截止（任一垂直转移落地后设置）
        self._vert_ban = {}                  # (from_key,to_key) -> (until_ts, mult)
        self._vert_pending = None            # 进行中的垂直转移 (src,dst)，落地时登记
        self._confirm_due = 0.0      # rule5：地面确认计时器（0.75-1.25s 随机）
        self._roam_gate = None
        self.fall_vel = 0.0
        self.climb_from_y = 0.0
        self.climb_to_y = 0.0
        self.climb_elapsed = 0.0
        self.climb_seg = None
        self.dragging = False
        self._drag_off = QPoint()
        # 事件反应高 DPI：当前屏幕 DPR（reload 时刷新；换屏 dpr 变化重建 sheet）
        self._dpr = self._cur_dpr()
        # 事件反应 play_reaction：反应播放态（None=未播放；不接事件源，收口方后续接线）
        self._reaction = None          # {"kind","row","until","prev_phase","prev_pause"}
        self._reaction_timer = QTimer(self)
        self._reaction_timer.setSingleShot(True)
        self._reaction_timer.timeout.connect(self._end_reaction)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._step)
        self._anim.start(33)
        if self.pet_menu is None:
            self.pet_menu = PetMenu(get_cfg, self._set_cfg,
                                    meta.get("hermes_home", ""))
        if self.pet_menu.on_change is None:
            self.pet_menu.on_change = self.reload
        self.reload()

    # ---- 配置/素材 ----
    def _cur_dpr(self):
        """当前窗口所在屏的 devicePixelRatio（screen() 为空时回落 screenAt/主屏）。
        事件反应：sheet 按物理像素渲染需要它；与 _screen_geo 同口径选屏。"""
        scr = self.screen() \
            or QApplication.screenAt(self.frameGeometry().center()) \
            or QApplication.primaryScreen()
        try:
            return float(scr.devicePixelRatio()) if scr is not None else 1.0
        except Exception:
            return 1.0

    def reload(self):
        cfg = self.get_cfg()
        slug = cfg.get("slug") or ""
        pets = installed_pets(self.meta.get("hermes_home", ""))
        if not slug or slug not in pets:
            slug = pets[0] if pets else None
        scale = clamp_scale(float(cfg.get("scale", 0.7) or 0.7))
        # 事件反应高 DPI：取当前屏 dpr；dpr 变化（拖到不同缩放屏/改缩放）时
        # 重建 sheet 缓存——逻辑尺寸 w/h 不变，窗口几何与存档语义不受影响。
        dpr = self._cur_dpr()
        dpr_changed = self.sheet is not None and abs(dpr - self._dpr) > 1e-6
        self._dpr = dpr
        if slug and (slug != self.slug or self.sheet is None or dpr_changed
                     or abs(scale - self.sheet.scale) > 1e-6):
            sp = sheet_path(self.meta.get("hermes_home", ""), slug)
            if sp:
                old_h = self.height() if self.sheet else 0
                self.sheet = PetSheet(sp, scale, dpr=dpr)
                self.slug = slug
                self.cur_row = -1  # sheet 重建：强制重取帧
                self.resize(self.sheet.w, self.sheet.h)
                self._place_initial(old_h)
                if dpr_changed:
                    self._plog(f"screen dpr -> {dpr}: sheet rebuilt "
                               f"(logical {self.sheet.w}x{self.sheet.h}, "
                               f"physical {self.sheet.pw}x{self.sheet.ph})")
        top = bool(cfg.get("topmost", True))
        if top != getattr(self, "_top", None):
            self._top = top
            vis = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, top)
            if vis:
                self.show()
        roam_new = bool(cfg.get("roam", True))
        if roam_new and not getattr(self, "roam_on", False):
            # 游走刚开启：刷新地面并起确认计时器
            self._refresh_ground()
            self._schedule_confirm(time.time())
        self.roam_on = roam_new
        # R26：两个行为细节键（显示类，缺键按默认 true 读，config 补齐在宿主侧）
        self.follow_window = bool(cfg.get("follow_window", True))
        self.vertical_roam = bool(cfg.get("vertical_roam", True))
        if not self.follow_window:
            self.cur_owner = None
            self._owner_rect = None

    # ---- 几何 ----
    def _screen_geo(self):
        scr = QApplication.screenAt(self.frameGeometry().center()) \
            or QApplication.primaryScreen()
        return scr.availableGeometry()

    def _ground_top(self):
        return float(self._screen_geo().bottom()) - self.height()

    def _place_initial(self, old_h=0):
        g = self._screen_geo()
        x, y = float(g.right() - self.width() - 80), self._ground_top()
        if old_h:
            # 运行中换宠物/改大小：沿用旧位置（存档+高度差微调），体验不变
            try:
                saved = json.loads(self.pos_file.read_text(encoding="utf-8"))
                x = float(saved.get("x", x))
                y = float(saved.get("y", y))
                if abs(old_h - self.height()) > 2:
                    y += old_h - self.height()
            except Exception:
                pass
        # 屏内出生 FEATURE（作者裁定）：新建窗口一律刷在屏内右下角地面，不读
        # pet_pos.json——出逃存档不再"复活"，重启后必能看见宠物验证修复。
        # 顺手补 y 上界 clamp：旧版只夹下界，存档 y 为负（顶部出逃）永久救不回。
        x = max(float(g.left()), min(x, float(g.right()) - self.width()))
        y = max(float(g.top()), min(y, self._ground_top()))
        self.move(int(x), int(y))
        self._plog(f"placed initial at ({int(x)},{int(y)}) "
                   f"({'resize' if old_h else 'fresh spawn, in-screen'})")

    def save_pos(self):
        try:
            tmp = self.pos_file.with_name("pet_pos.json.tmp")
            tmp.write_text(json.dumps({"x": self.x(), "y": self.y()}), encoding="utf-8")
            tmp.replace(self.pos_file)
        except Exception:
            pass

    def set_host(self, pet_state, at_rest):
        if pet_state != self.host_state:
            self._plog(f"host_state {self.host_state} -> {pet_state} "
                       f"(pose={HOST_MAP.get(pet_state, 'idle')}, at_rest={at_rest})")
        self.host_state = pet_state
        self.at_rest = at_rest

    def _set_cfg(self, key, value):
        if self.set_cfg_cb:
            self.set_cfg_cb(key, value)
        self.reload()

    # ---- 事件反应通用反应 API（本轮不接事件源，收口方后续接线）----
    def play_reaction(self, kind, duration_s=4.0):
        """播放一次性反应动画：kind ∈ {"done","failed","await_input"}。
        done→wave 行（happy）、failed→failed 行、await_input→wave 行。
        播放期间暂停游走/物理（复用 rule2 同款钉住机制），duration 后恢复
        原状态；重复调用刷新计时（不叠加恢复点）；隐藏/未建 sheet/未知 kind
        一律 no-op 返回 False。offscreen 可测。"""
        if self.sheet is None or not self.isVisible():
            return False
        pose = REACTION_MAP.get(str(kind))
        if pose is None:
            self._plog(f"play_reaction: unknown kind {kind!r} -> no-op")
            return False
        try:
            dur = float(duration_s)
        except (TypeError, ValueError):
            dur = 4.0
        if dur <= 0:
            dur = 4.0
        row = row_index(pose, self.sheet.rows)
        if self._reaction is not None:
            # 重复调用：只刷新行与计时，恢复点保持首次播放前的状态
            self._reaction["kind"] = kind
            self._reaction["row"] = row
        else:
            self._reaction = {"kind": kind, "row": row,
                              "prev_phase": self.phase,
                              "prev_pause": self.pause_until}
            self.phase = "pause"  # 钉住：不走 walk/fall/climb
        self._reaction["until"] = time.time() + dur
        self.dir = 0
        self.cur_row = -1  # 立即切到反应行
        self._reaction_timer.start(int(dur * 1000))
        self._plog(f"play_reaction {kind} pose={pose} dur={dur:.2f}s")
        self.update()
        return True

    def _end_reaction(self):
        """反应计时到：恢复播放前的状态（host_state 从未被改动）。"""
        r = self._reaction
        if r is None:
            return
        self._reaction = None
        self.phase = r["prev_phase"]
        self.pause_until = r["prev_pause"]
        self._plog(f"reaction {r['kind']} ended -> restore phase={self.phase}")
        self.update()

    def screenChanged(self, screen=None):
        """拖到不同缩放的屏幕：重查 dpr，变了就重建 sheet 缓存。
        接线在 showEvent（QWindow.screenChanged 信号；QWidget 无此虚函数），
        reload 里另有同款兜底（tick 每拍调用），双保险。"""
        try:
            dpr = self._cur_dpr()
            if self.sheet is not None and abs(dpr - self._dpr) > 1e-6:
                self.reload()
        except Exception:
            pass

    def showEvent(self, ev):
        super().showEvent(ev)
        # 事件反应：窗口 native handle 就绪后接 QWindow.screenChanged 信号
        # （QWidget 没有 screenChanged 虚函数，必须走 QWindow）。
        try:
            h = self.windowHandle()
            if h is not None and not getattr(self, "_scr_sig_hooked", False):
                h.screenChanged.connect(self.screenChanged)
                self._scr_sig_hooked = True
        except Exception:
            pass

    # ---- 决策与物理（地面系统：作者七条规则线段制地面系统）----
    def _refresh_ground(self):
        """rule1+rule3+rule4：实时重建地面线段列表（仅游走开启时被调用）。
        R26-2：同时刷新归属表 seg_owner；cur_seg 若还在新列表里按值重绑
        （重建后是新 tuple 对象，is 比较会失效），owner 同步。"""
        try:
            hwnd = int(self.winId())
        except Exception:
            hwnd = 0
        segs, owners = build_ground_segments(self.width(), self.height(),
                                             self._screen_geo(), hwnd,
                                             with_owners=True,
                                             screen_geos=[s.geometry() for s in
                                                          QApplication.screens()])
        self.ground_segs = segs
        self.seg_owner = owners
        if self.cur_seg is not None:
            if self.cur_seg in owners:
                # 值相同但对象换新：重绑到列表内实例（is 语义维持）
                for sg in segs:
                    if sg == self.cur_seg:
                        self.cur_seg = sg
                        break
                self.cur_owner = owners.get(self.cur_seg)
            else:
                self.cur_owner = None
        return segs

    def _bind_seg(self, seg):
        """R26-2：绑定站立线段并同步归属（站定/改绑/落位/攀爬完成统一走这里）。"""
        self.cur_seg = seg
        self.cur_owner = self.seg_owner.get(seg) if seg is not None else None
        if self.cur_owner and self.follow_window:
            rect = owner_window_rect(self.cur_owner)
            self._owner_rect = window_logical_rect(*rect) if rect else None
        else:
            self._owner_rect = None

    def _follow_owner(self, now):
        """R26-2：pause 态窗口跟随。对 cur_owner 做 O(1) GetWindowRect，
        矩形变化 → 按 dx/dy 平移贴线，x 夹可行域（BUG3）。owner 失效
        （关/最小化/cloaked）→ 立即自由落体。返回 True=已改变状态。
        忙时/气泡聚焦/事件反应期间同样生效（rule5 同口径）；拖拽中不调用。"""
        if not self.follow_window or self.cur_owner is None \
                or self.cur_seg is None or self.dragging:
            return False
        rect = owner_window_rect(self.cur_owner)
        if rect is None:
            self._plog("follow: owner gone (closed/minimized/cloaked) -> fall")
            self._bind_seg(None)
            self._begin_fall(now)
            return True
        lr = window_logical_rect(*rect)
        if lr is None:
            self._bind_seg(None)
            self._begin_fall(now)
            return True
        if self._owner_rect is None:
            self._owner_rect = lr
            return False
        dx = lr[0] - self._owner_rect[0]
        dy = lr[1] - self._owner_rect[1]
        dw = (lr[2] - lr[0]) - (self._owner_rect[2] - self._owner_rect[0])
        self._owner_rect = lr
        if abs(dx) < 0.5 and abs(dy) < 0.5 and abs(dw) < 0.5:
            return False
        # 线段随窗口平移/伸缩：底沿 y=矩形 bottom，顶沿 y=矩形 top
        seg = self.cur_seg
        on_bottom = abs(seg[1] - (self._owner_rect[3] - dy)) <= GROUND_STAND_EPS + 1
        new_y = lr[3] if on_bottom else lr[1]
        new_x1 = seg[0] + dx
        new_x2 = seg[2] + dx + dw
        nx = float(self.x()) + dx
        nx = max(new_x1, min(nx, new_x2 - float(self.width())))
        ny = new_y - float(self.height())
        self.move(int(nx), int(ny))
        new_seg = (new_x1, new_y, new_x2, new_y)
        # 归属表同步换键（下次全量重建会再校准）
        self.seg_owner.pop(seg, None)
        self.seg_owner[new_seg] = self.cur_owner
        self.cur_seg = new_seg
        if now - self._follow_save_ts >= FOLLOW_SAVE_MIN_S:
            self._follow_save_ts = now
            self.save_pos()
        return False

    def _bottom_seg(self):
        """桌宠下边界线段（屏幕坐标，Qt 逻辑像素；y 向下增大）。"""
        x1 = float(self.x())
        x2 = x1 + float(self.width())
        yb = float(self.y()) + float(self.height())
        return x1, yb, x2

    def _dwell(self):
        u = 1 - random.random()
        return min(PAUSE_DWELL["max"], max(PAUSE_DWELL["min"],
                                           -math.log(u) * PAUSE_DWELL["mean"]))

    def _stroll_target(self, seg, from_x):
        span = seg[2] - seg[0]
        if span <= 4:
            return seg[0]
        rl, rr = from_x - seg[0], seg[2] - from_x
        go_right = (random.random() < STROLL_TOWARD_ROOM) == (rr >= rl)
        room = max(0.0, rr if go_right else rl)
        min_d = min(room, max(span * STROLL_MIN_FRACTION, STROLL_MIN_PX))
        dist = min_d + random.random() * max(0.0, room - min_d)
        return from_x + dist if go_right else from_x - dist

    def _begin_pause(self, now):
        self.phase = "pause"
        self.dir = 0
        self.pause_until = now + self._dwell()

    def _plog(self, msg):
        if not VERBOSE:
            return
        try:
            f = self.bus / "pet_debug.log"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} PET {msg}\n")
            if f.stat().st_size > 400_000:
                keep = f.read_text(encoding="utf-8").splitlines()[-2000:]
                tmp = f.with_name("pet_debug.log.tmp")
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                tmp.replace(f)
        except Exception:
            pass

    def _schedule_confirm(self, now):
        """rule5：确认计时器 [0.75, 1.25]s 随机。"""
        self._confirm_due = now + random.uniform(CONFIRM_MIN_S, CONFIRM_MAX_S)

    # ---- R26-5：共振记忆（垂直转移闸门）----
    def _seg_key(self, seg):
        if seg is None:
            return None
        return (round(seg[0]), round(seg[1]), round(seg[2]))

    def _vert_gate_ok(self, src, dst, now):
        """垂直转移 src->dst 是否放行：全局冷却未生效 且 该有向对未被封禁。"""
        if now < self._vert_cooldown_until:
            return False
        key = (self._seg_key(src), self._seg_key(dst))
        until, _c = self._vert_ban.get(key, (0.0, 0))
        return now >= until

    def _note_vert(self, src, dst, now):
        """落地登记：设全局冷却；同一对线段的反向转移封禁按时数递增
        （30s→60s→90s→120s 封顶）——把 A↔B 振荡从秒级共振压成分钟级偶发。
        R26-5b（评审 P1-3）：pair 计数空置超 PAIR_DECAY_S 重置为 1——长跑后
        旧 pair 不永久高倍封禁；pair 表仍随防胀清理整体回收。"""
        ka, kb = self._seg_key(src), self._seg_key(dst)
        if ka is None or kb is None:
            return
        pk = (min(ka, kb), max(ka, kb))
        ts, cnt = self._vert_ban.get(("pair", pk), (0.0, 0))
        if now - ts > PAIR_DECAY_S:
            cnt = 0   # 空置太久：重复计数清零重来
        cnt += 1
        mult = min(cnt, REVERSE_BAN_MAX_MULT)
        self._vert_ban[("pair", pk)] = (now, cnt)
        self._vert_ban[(kb, ka)] = (now + REVERSE_BAN_S * mult, cnt)
        self._vert_cooldown_until = now + VERT_COOLDOWN_S
        if len(self._vert_ban) > 256:  # 长跑防胀：按时间戳清过期（pair 条目
            # 也参与回收——空置>PAIR_DECAY_S 视作过期，评审 P1-3 原版漏了这条）
            self._vert_ban = {k: v for k, v in self._vert_ban.items()
                              if (k[0] == "pair" and now - v[0] <= PAIR_DECAY_S)
                              or (k[0] != "pair" and v[0] > now)}

    def _consume_vert_pending(self, seg, now):
        """climb/descend 动画完成时调用：与发起时记录的 (src,dst) 匹配才登记。
        R26-5b（评审 P0）：生产路径 _vertical_seg_alive 每帧 _refresh_ground
        重建地面，climb_seg 按值重绑成新 tuple 对象——`is` 匹配恒 False，
        冷却/封禁从不生效（实测 150s 振 55 次且零登记）。改值相等匹配；
        窗口中途挪动时值不再相等，走既有 abort→fall 链路，无安全影响。"""
        p = self._vert_pending
        if p is not None and seg is not None and (p[1] is seg or p[1] == seg):
            self._note_vert(p[0], p[1], now)
        self._vert_pending = None

    def _on_seg(self, seg, now):
        """落位到线段上（底边贴线）。R26-2：经 _bind_seg 同步归属。"""
        self._bind_seg(seg)
        self.move(self.x(), int(seg[1] - self.height()))
        self._begin_pause(now)
        self._schedule_confirm(now)
        self.save_pos()

    def _begin_fall(self, now):
        self.phase = "fall"
        self.fall_vel = 0.0
        self.dir = 0
        self.cur_seg = None
        self._vert_pending = None  # R26-5：半途中止的垂直转移不登记

    def _begin_climb(self, seg, now, target_x=None):
        """rule7：攀爬 = 平滑上移动画（CLIMB_DUR_S），杜绝瞬移。
        R26-3：target_x 非 None 时同步平滑横移（跨线段跳转）。"""
        self.phase = "climb"
        self.climb_from_y = float(self.y())
        self.climb_to_y = seg[1] - self.height()
        self.climb_elapsed = 0.0
        self.climb_seg = seg
        self.climb_from_x = float(self.x())
        self.climb_to_x = float(self.x()) if target_x is None else float(target_x)
        self.dir = 0
        self._plog(f"climb -> seg y={seg[1]:.0f} x=[{seg[0]:.0f},{seg[2]:.0f}] "
                   f"dy={self.climb_from_y - self.climb_to_y:.0f}px")

    def _begin_descend(self, seg, now, target_x=None):
        """R26-3：向下爬 = 平滑下移动画（对称 rule7）。时长=距离/DESCEND_PX_S，
        下限 CLIMB_DUR_S；复用 climb 插值管线（phase=descend），演 jumping 行。"""
        self.phase = "descend"
        self.climb_from_y = float(self.y())
        self.climb_to_y = seg[1] - self.height()
        dist = abs(self.climb_to_y - self.climb_from_y)
        self.descend_dur = max(CLIMB_DUR_S, dist / DESCEND_PX_S)
        self.climb_elapsed = 0.0
        self.climb_seg = seg
        self.climb_from_x = float(self.x())
        self.climb_to_x = float(self.x()) if target_x is None else float(target_x)
        self.dir = 0
        self._plog(f"descend -> seg y={seg[1]:.0f} x=[{seg[0]:.0f},{seg[2]:.0f}] "
                   f"dy={dist:.0f}px dur={self.descend_dur:.2f}s")

    def _vertical_seg_alive(self):
        """R26-3：垂直动作（climb/descend）每帧校验目标线段仍有效——
        刷新地面后目标须仍在列表内（按值），否则当帧中止转自由落体。"""
        self._refresh_ground()
        tgt = self.climb_seg
        if tgt is None:
            return False
        for sg in self.ground_segs:
            if sg == tgt:
                self.climb_seg = sg
                return True
        return False

    def _confirm_ground(self, now, moving):
        """rule5+6+7：地面确认。返回 True 表示已改变状态（打断移动）。
        moving=True 时（walk 中）只做"脚下是否仍是地面"的实时判定，
        地面数据更新导致离开地面 → 立即停止移动。每次确认都刷新地面数据。"""
        segs = self._refresh_ground()
        x1, yb, x2 = self._bottom_seg()
        support = find_support(segs, x1, x2, yb)
        if moving:
            if support is None:
                self._plog(f"confirm(moving): lost ground at y={yb:.0f} -> stop+fall")
                self._begin_fall(now)
                return True
            if support is not self.cur_seg:
                self._bind_seg(support)  # 走到了另一条线上，静默改绑（R26-2 同步归属）
            return False
        # 停驻确认：rule6 自由落体 → rule7 攀爬
        if support is not None:
            self._bind_seg(support)
            target = find_climb_target(segs, x1, float(self.y()), x2, yb, support)
            # R26-5b（评审 P1-4）：被动爬向同窗顶沿（窄悬浮窗顶底互穿）同属
            # 共振燃料，与主动下爬对称排除。owner=None（屏底/任务栏）不参与，
            # 防"两个无主线段"被误判同窗。
            if target is not None and self.cur_owner is not None \
                    and self.seg_owner.get(target) == self.cur_owner:
                target = None
            # R26-5：被动攀爬同样过共振闸门——刚落地下来的 8s 冷却内不再自动爬。
            # R26-6：过闸后再掷接触概率（主人裁定：没必要见梯子就爬；每次确认
            # 都算一次接触，掷不中就安静站着）。vertical_roam=False 保持旧行为必爬。
            if target is not None and (not self.vertical_roam
                                       or (self._vert_gate_ok(support, None, now)
                                           and self._vert_gate_ok(support, target, now)
                                           and random.random() < CONTACT_CLIMB_CHANCE)):
                if self.vertical_roam:
                    self._vert_pending = (support, target)
                self._begin_climb(target, now)
                return True
            self._schedule_confirm(now)
            return False
        # 不在任何地面线段上 → 自由落体（rule6）
        tgt = find_fall_target(segs, x1, x2, yb)
        if tgt is not None:
            self._plog(f"confirm: airborne -> fall to seg y={tgt[1]:.0f}")
            self._begin_fall(now)
            return True
        # 找不到落点：多因身体横移出所有线段 x 域（如屏幕边缘）——先把 x
        # 夹回屏内再落，恢复贴地而非原地震荡（菜单驻留 BUG3 兜底）
        g = self._screen_geo()
        cx = min(max(float(self.x()), float(g.left())),
                 float(g.right()) - float(self.width()))
        if abs(cx - float(self.x())) > 1:
            self._plog(f"confirm: x out of ground span -> clamp {self.x()}->{cx:.0f}")
            self.move(int(cx), self.y())
            # 夹回后先重查 support：y 本就对齐时温柔贴地即可，不必坠落
            x1n = float(self.x())
            sup2 = find_support(segs, x1n, x1n + float(self.width()), yb)
            if sup2 is not None:
                self._on_seg(sup2, now)
                return True
        self._begin_fall(now)
        return True

    def _try_adjacent_hop(self, now):
        """R26-3：漫步抵端点后的跨线段跳转。端点外侧（行进方向）存在
        x 邻接（间隙 ≤ pet_w）且高差 ≤ 2×pet_h 的线段 → 按 ADJACENT_CHANCE
        掷跳：更高=climb（带横移）、更低=descend（带横移）、同高=平移衔接
        （walk 直送）。返回 True=已发起跳转。每跳落地后 _on_seg 强制回
        pause+确认计时器，杜绝连跳失控。"""
        if not self.vertical_roam or self.cur_seg is None:
            return False
        # R26-5：端点跳与垂直动作共用冷却/封禁（A→B→A 左右横跳同属共振）
        if not self._vert_gate_ok(self.cur_seg, None, now):
            return False
        if random.random() >= ADJACENT_CHANCE:
            return False
        dir_hint = 1 if self.walk_target >= float(self.x()) else -1
        found = find_adjacent_segment(self.ground_segs, self.cur_seg,
                                      float(self.width()),
                                      float(self.height()), dir_hint)
        if found is None:
            return False
        seg, tx = found
        if not self._vert_gate_ok(self.cur_seg, seg, now):
            return False
        dy = seg[1] - self.cur_seg[1]
        self._plog(f"adjacent hop: dy={dy:.0f} -> seg x=[{seg[0]:.0f},{seg[2]:.0f}] tx={tx:.0f}")
        if dy < -GROUND_STAND_EPS:
            self._vert_pending = (self.cur_seg, seg)
            self._begin_climb(seg, now, target_x=tx)
        elif dy > GROUND_STAND_EPS:
            self._vert_pending = (self.cur_seg, seg)
            self._begin_descend(seg, now, target_x=tx)
        else:
            # 同高平移：walk 直送（落点后既有 pause+确认链路）。
            # 平移也计入一次转移登记——反向跳同样进封禁计数。
            self._note_vert(self.cur_seg, seg, now)
            self.walk_target = tx
            self.dir = 1 if tx > self.x() else -1
            self.phase = "walk"
        return True

    def _plan(self, now):
        """游走决策：在当前线段上漫步。R26-5b（主人裁定）：主动垂直走
        大数轴骰盘——一次掷骰落领地即动（上爬 [0,400)、下爬 [400,800)、
        其余没想法），无旧版双骰穿透；vertical_roam=false 时零主动垂直
        动作（旧行为）。"""
        segs = self.ground_segs
        # R26-2：跟随可能已把宠物挪到新位置而 ground_segs 还是旧快照——
        # 决策前先重建，防按旧数据误判 fall（0.23ms/次，仅决策时调用）
        if self.follow_window and self.cur_owner is not None:
            segs = self._refresh_ground()
        x1, yb, x2 = self._bottom_seg()
        support = find_support(segs, x1, x2, yb)
        self._bind_seg(support)
        if self.cur_seg is None:
            self._begin_fall(now)
            return
        # R26-5b（主人裁定）：大数轴骰盘——一次 randrange 掷 [0,10000)，
        # 落上爬领地→爬、落下爬领地→降、其余=没想法（临时起意，绝大多数
        # 时候不动）。无双骰穿透：上爬掷不中不会"再试试下爬"。全局冷却
        # 未过则整段跳过。vertical_roam=false 保持旧行为零主动垂直动作。
        if self.vertical_roam and self._vert_gate_ok(self.cur_seg, None, now):
            up = find_climb_target(segs, x1, float(self.y()), x2, yb,
                                   self.cur_seg)
            # R26-5b（评审 P1-4）：主动上爬同窗顶沿对称排除（与下爬同口径）
            if up is not None and self.cur_owner is not None \
                    and self.seg_owner.get(up) == self.cur_owner:
                up = None
            down = find_descend_target(segs, x1, x2, yb, self.cur_seg,
                                       float(self.height()))
            # R26-5：同窗顶↔底沿互降 = 共振燃料。cur_owner 线段的下降目标
            # 若归属同一窗口（或就是同窗顶沿换算出的 y），直接拒绝。
            if down is not None and self.cur_owner is not None \
                    and self.seg_owner.get(down) == self.cur_owner:
                down = None
            if up is not None or down is not None:
                r = random.randrange(PLAN_DICE_MAX)
                if up is not None and r < CLIMB_TERRITORY_W \
                        and self._vert_gate_ok(self.cur_seg, up, now):
                    self._vert_pending = (self.cur_seg, up)
                    self._begin_climb(up, now)
                    return
                if down is not None \
                        and CLIMB_TERRITORY_W <= r < CLIMB_TERRITORY_W + DESCEND_TERRITORY_W \
                        and self._vert_gate_ok(self.cur_seg, down, now):
                    self._vert_pending = (self.cur_seg, down)
                    self._begin_descend(down, now)
                    return
                # 落空（领地闲置/被闸拦）：本轮没想法，回落漫步
        if random.random() < REST_CHANCE:
            self._begin_pause(now)
            self._schedule_confirm(now)
            return
        # 菜单驻留 BUG3：可行域是 [seg.x1, seg.x2 - pet_w]（x 是左上角）——
        # 旧版走到 seg.x2 时身体伸出线段外 → containment 失败 → fall/兜底
        # pause 互相触发 = 右边缘震荡
        wt = self._stroll_target(self.cur_seg, float(self.x()))
        lo = self.cur_seg[0]
        hi = max(lo, self.cur_seg[2] - float(self.width()))
        self.walk_target = min(max(wt, lo), hi)
        self.dir = 1 if self.walk_target > self.x() else -1
        if abs(self.walk_target - float(self.x())) < ARRIVE_EPS:
            self._begin_pause(now)  # 无处可走（线段比宠物窄）
            self._schedule_confirm(now)
            return
        self.phase = "walk"

    def _step(self):
        if self.sheet is None:
            return
        now = time.time()
        dt = min(MAX_DT, max(0.0, now - self.last))
        self.last = now

        # 事件反应 play_reaction：播放期间钉住（同 rule2 机制），不跑地面/重力/
        # 确认/游走，只演反应行；计时到由 _reaction_timer 恢复原状态。
        if self._reaction is not None:
            if now >= self._reaction["until"]:
                self._end_reaction()  # 兜底：定时器未及触发（事件循环阻塞）
            else:
                # R26-2：反应播放期间窗口被挪走仍要贴线跟随（rule5 同口径）
                if self.roam_on and not self.dragging:
                    self._follow_owner(now)
                self.dir = 0
                self._advance_frame(now)
                self.update()
                return

        # rule2（BUG1 修复）：游走关闭 = 普通浮窗桌宠——不跑任何地面/重力/
        # 确认代码，宠物钉在原地演 host_state 动画，作者随意摆放。
        if not self.roam_on:
            if self._roam_gate is not False:
                self._plog("roam OFF: physics frozen, plain floating window")
                self._roam_gate = False
            self.dir = 0
            if self.phase not in ("pause", "idle_static"):
                self.phase = "pause"
            self._advance_frame(now)
            self.update()
            return

        roaming = self.at_rest and not self.dragging and not self.bubble_focus
        if self._roam_gate is not True:
            self._plog(f"roam ON: gate={'open' if roaming else 'closed(busy/drag/bubble)'}")
            self._roam_gate = True

        if self.dragging:
            # rule5：拖动中 → 确认计时器归零暂停
            self._confirm_due = 0.0
            self.phase = "pause"
        elif self.phase == "pause":
            # rule5：确认计时器绑定"游走开启且未拖动"，与 Agent 忙/闲无关——
            # Agent 忙时宠物摆姿势，窗口被挪动仍要确认贴地；只是不主动游走。
            # R26-2：pause 每拍先做 O(1) 窗口跟随（忙时/聚焦时同样生效）。
            if self._follow_owner(now):
                pass  # owner 失效已转 fall
            elif not self.dragging and self._confirm_due and now >= self._confirm_due:
                self._confirm_ground(now, moving=False)
            elif roaming and now >= self.pause_until:
                self._plan(now)
            else:
                self.dir = 0
        elif self.phase == "walk":
            if not roaming:
                self._plog("walk aborted: host busy -> stop and pose")
                self._begin_pause(now)
                self._schedule_confirm(now)
                self.dir = 0
            else:
                speed = (self.width() * STRIDE_PER_LOOP) / (sprites.LOOP_MS / 1000.0)
                rem = self.walk_target - self.x()
                step_d = speed * dt
                if abs(rem) <= max(ARRIVE_EPS, step_d):
                    self.move(int(self.walk_target), self.y())
                    # R26-3：抵端点先试跨线段跳转（掷不中/无候选 → 既有 pause）
                    if self._try_adjacent_hop(now):
                        self.save_pos()
                    else:
                        # rule5：自然停止 → 继续计时
                        self._begin_pause(now)
                        self._schedule_confirm(now)
                        self.save_pos()
                else:
                    nx = int(self.x() + (1 if rem > 0 else -1) * step_d)
                    self.move(nx, self.y())
                    # rule5：移动中计时器归零暂停，但**实时**刷新并判定地面——
                    # 地面数据更新（窗口挪动/关闭）致离开地面线段 → 立即停止移动
                    self._confirm_due = 0.0
                    self._refresh_ground()  # 0.23ms/帧，可负担
                    x1, yb, x2 = self._bottom_seg()
                    sup = find_support(self.ground_segs, x1, x2, yb)
                    if sup is None:
                        self._plog("walk: lost ground (window moved?) -> stop+fall")
                        self._confirm_ground(now, moving=True)
                    else:
                        self._bind_seg(sup)
        elif self.phase == "fall":
            # rule6：自由落体到下方第一条地面线段
            prev_y = float(self.y())
            self.fall_vel += GRAVITY_PX_S2 * dt
            y = prev_y + self.fall_vel * dt
            x1 = float(self.x())
            x2 = x1 + float(self.width())
            tgt = find_fall_target(self.ground_segs, x1, x2, prev_y + self.height())
            if tgt is None:
                tgt = find_fall_target(self.ground_segs, x1, x2, y + self.height())
            landed = False
            if tgt is not None:
                stand_y = tgt[1] - self.height()
                if prev_y <= stand_y <= y or y >= stand_y:
                    self._plog(f"land on seg y={tgt[1]:.0f}")
                    self.move(self.x(), int(stand_y))
                    self._on_seg(tgt, now)
                    landed = True
            if not landed:
                self.move(self.x(), int(y))
                # 兜底：越过屏幕底
                gbot = float(self._screen_geo().bottom()) - self.height()
                if self.y() >= gbot:
                    self.move(self.x(), int(gbot))
                    self._begin_pause(now)
                    self._schedule_confirm(now)
        elif self.phase in ("climb", "descend"):
            # rule7/R26-3：攀爬/下爬动画（线性插值，非瞬移；descend 时长随距离）。
            # 每帧校验目标线段仍有效（窗口关闭/被盖 → 当帧中止转自由落体）。
            if not self._vertical_seg_alive():
                self._plog(f"{self.phase}: target seg vanished -> abort to fall")
                self.climb_seg = None
                self._begin_fall(now)
            else:
                dur = CLIMB_DUR_S if self.phase == "climb" else self.descend_dur
                self.climb_elapsed += dt
                t = min(1.0, self.climb_elapsed / dur)
                y = self.climb_from_y + (self.climb_to_y - self.climb_from_y) * t
                x = self.climb_from_x + (self.climb_to_x - self.climb_from_x) * t
                self.move(int(x), int(y))
                if t >= 1.0:
                    # R26-5：垂直转移落地登记（冷却+反向封禁），再走既有落位链路
                    self._consume_vert_pending(self.climb_seg, now)
                    self._on_seg(self.climb_seg, now)
                    self.climb_seg = None
        self._advance_frame(now)
        self.update()

    def _advance_frame(self, now):
        sheet = self.sheet
        if sheet is None:
            return
        if self._reaction is not None:
            # 事件反应：反应播放期优先于 host 状态/物理姿态
            row, mirror = self._reaction["row"], False
        elif self.phase in ("fall", "jump", "climb", "descend"):
            row, mirror = row_index("jump", sheet.rows), False
        elif self.phase == "walk" and self.dir != 0:
            row, mirror = sheet.walk_row(self.dir)
        elif self.bubble_focus:
            # 气泡聚焦增强：气泡开着钉在原地演 review（同模型思考动画），
            # 优先于 host_state（即便宿主 idle 也演思考态，"正在给你看数据"）。
            row, mirror = row_index("review", sheet.rows), False
        else:
            state = HOST_MAP.get(self.host_state, "idle")
            row, mirror = row_index(state, sheet.rows), False
        frames = sheet.frames(row)
        if row != self.cur_row:
            self.cur_row = row
            self.frame = 0
            self.last_step = now
        step_s = (sprites.LOOP_MS / 1000.0) / max(1, len(frames))
        if now - self.last_step >= step_s:
            self.frame = (self.frame + 1) % len(frames)
            self.last_step = now
        self.mirror = mirror
        self._frames = frames

    def paintEvent(self, ev):
        if self.sheet is None or not self._frames:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        pm = self._frames[self.frame % len(self._frames)]
        if self.mirror:
            # PySide6 6.12 的 transformed() 只收 QTransform（Qt.Horizontal
            # 直接 TypeError，旧代码左走镜像一画就炸）；QTransform 镜像保留
            # pixmap 的 devicePixelRatio（已探针实测），高 DPI 不糊。
            pm = pm.transformed(QTransform().scale(-1, 1))
        p.drawPixmap(0, 0, pm)
        p.end()

    # ---- 交互 ----
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            # 余额气泡：记录 press 全局坐标（单击/拖拽判定基准）。
            self._press_pos = ev.globalPosition().toPoint()
            self._drag_started = False
            self._drag_off = ev.globalPosition().toPoint() - self.pos()
            if self.on_click is None:
                # 旧版行为完全一致：press 即进入拖拽（无单击语义）。
                self.dragging = True
                self.dir = 0
                self._confirm_due = 0.0  # rule5：拖动中计时器归零暂停
        elif ev.button() == Qt.RightButton:
            self._popup()

    def mouseMoveEvent(self, ev):
        if (self.on_click is not None and not self._drag_started
                and self._press_pos is not None):
            # 余额气泡门槛：位移 >CLICK_MAX_PX 才进入既有拖拽；阈值内=潜在单击，
            # 窗口不动。进入拖拽后的语义与旧版 press 完全一致（一行不改）。
            g = ev.globalPosition().toPoint()
            if (g - self._press_pos).manhattanLength() > CLICK_MAX_PX:
                self._drag_started = True
                self.dragging = True
                self.dir = 0
                self._confirm_due = 0.0  # rule5：拖动中计时器归零暂停
                # _drag_off 保持 press 时记录的偏移（旧版语义）：crossing 帧
                # 窗口跟手到 press 以来全位移，之后逐帧跟手，无二次锚定跳变。
        if self.dragging:
            self.move(ev.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            # 地面系统 rule5：拖动结束 → 计时器重新开始计时（0.75-1.25s 后
            # 确认地面：自由落体+攀爬）；期间保持放手位置不动。
            now = time.time()
            self._plog("drag released -> confirm timer restart")
            self.phase = "pause"
            self.pause_until = now + 9999  # 确认计时器先到
            self._schedule_confirm(now)
            if self.roam_on:  # rule2：关游走不跑地面代码，放手即钉住
                self._refresh_ground()
            self.save_pos()
        elif (ev.button() == Qt.LeftButton and self._press_pos is not None
              and not self._drag_started and self.on_click is not None):
            # 余额气泡：未达拖拽阈值 = 单击 → 回调（气泡 toggle 由收口接线）。
            try:
                self.on_click()
            except Exception as exc:
                self._plog(f"on_click RAISED: {type(exc).__name__}: {exc}")
        if ev.button() == Qt.LeftButton:
            self._press_pos = None
            self._drag_started = False

    def _popup(self):
        if self.pet_menu is not None:
            self.pet_menu.exec_at(QCursor.pos())
