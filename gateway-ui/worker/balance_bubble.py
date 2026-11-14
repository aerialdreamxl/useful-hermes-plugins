# -*- coding: utf-8 -*-
"""balance_bubble.py — 余额气泡 BalanceBubble（CONTRACT §10，余额气泡）。

worker 进程内 Qt 组件：左键单击桌宠弹出（收口接线 on_click），只读宿主
刷好的 balance_view.json，零网络调用。布局四段（§10.5）：
  标题行 < 平台名 >（箭头 24x24 热区，仅 1 provider 隐藏）
  金额行 大号 display（ok=false 显示 error 截断 24 字符；过期变灰）
  峰谷行 [峰]红/[谷]绿 + HH:MM:SS 倒计时（QTimer 1s 本地走，跨边界用
         peak_rules 以 view 透传的 rule/windows/holidays 推进；peak=null 整行 hide）
  底行小字 extra.today（若有）+ "缓存于 X 分前"（age>2*refresh_s 加"数据可能过期"）

读取 mtime+内容签名双判（§6 同款）；缺失/损坏 → "等待宿主数据"单行态；
providers 空 → "未配置余额源"。箭头循环切换（内存 idx，重启回 default_id），
切换零 settings_req（A2）。右键菜单扁平两项：立即刷新（写 balance_refresh_req.json
原子 tmp+rename）/ 关闭气泡。

锚定（spike 实锤算法）：头顶上方 8px 居中，无头空间翻脚下，clamp 进屏；
QRect.bottom() 含 -1 陷阱 → 一律 top()+height() 口径。双窗无父子关系。
不 import main（避免循环）；get_screen_rect 回调注入屏幕矩形。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QCursor, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from qfluentwidgets import RoundMenu
from PySide6.QtGui import QAction

import peak_rules

# ---- 视觉常量（CONTRACT §10.5）----
BUBBLE_W = 220
RADIUS = 10
BG = QColor(24, 26, 32, 235)
FG = "#f2f3f5"
FG_DIM = "#9aa0a8"
FG_STALE = "#8a8f98"
PEAK_RED = "#e0433f"
VALLEY_GREEN = "#2fa24c"
GAP_PX = 8                 # 气泡与宠物的间距（spike 定值）
PAD_X, PAD_TOP, PAD_BOT = 12, 10, 10
ROW_TITLE_H, ROW_AMOUNT_H, ROW_PEAK_H, ROW_BOTTOM_H = 24, 28, 18, 16
ROW_GAP = 6
MSG_H = 44                 # 单行态（等待/未配置）高度
ERROR_MAX_CHARS = 24
DEFAULT_REFRESH_S = 300
# 余额气泡修订：lines 结构化多行（作者原型图排版）行高
ROW_L_TITLE_H, ROW_L_AMOUNT_H, ROW_L_INFO_H = 20, 28, 20
# 进度条修正：bar 行=进度条+百分比（作者裁定第二行是进度条不是文本）
ROW_L_BAR_H = 22
BAR_TRACK = QColor(255, 255, 255, 30)     # 空槽：半透明白
BAR_FILL = QColor("#4cc2ff")              # 填充： fluent 蓝
BAR_FILL_STALE = QColor("#8a8f98")        # 过期变灰
BAR_PCT_W = 46                            # 右侧百分比标签宽
BAR_GAP = 8                               # 条与百分比间距
BAR_H = 8                                 # 条本体高（行内垂直居中）
BAR_RADIUS = 4


def bubble_pos(pet_rect, bw, bh, screen):
    """spike 同款锚定纯函数：头顶上方 8px 居中 → 无头空间翻脚下 → clamp 进屏。
    返回 (QPoint, flipped)。注意 screen.right()=left+w-1（含 -1 陷阱），
    clamp 用 right()-bw 保证 x+bw <= right()+1 即完整入屏。"""
    x = pet_rect.center().x() - bw // 2
    x = max(screen.left(), min(x, screen.right() - bw))
    y = pet_rect.top() - bh - GAP_PX
    flipped = False
    if y < screen.top():
        y = pet_rect.top() + pet_rect.height() + GAP_PX
        flipped = True
        if y + bh > screen.top() + screen.height():
            y = max(screen.top(), screen.top() + screen.height() - bh)
    return QPoint(x, y), flipped


class _Arrow(QLabel):
    """24x24 箭头热区（QLabel 子类，点击回调注入）。"""

    def __init__(self, text, on_activate, parent=None):
        super().__init__(text, parent)
        self._on_activate = on_activate
        self.setFixedSize(24, 24)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        f = QFont(self.font())
        f.setPixelSize(13)
        self.setFont(f)
        self.setStyleSheet(f"color: {FG_DIM}; background: transparent;")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._on_activate is not None:
            self._on_activate()
        super().mousePressEvent(ev)


class _ProgressBar(QWidget):
    """进度条修正：余额进度条（作者原型图第二行）。左条右百分比；
    fraction 0~1 填充，过期变灰。纯绘制，无交互。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fraction = 0.0
        self._pct_text = ""
        self._stale = False
        self.lbl_pct = QLabel("", self)
        self.lbl_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f = QFont(self.lbl_pct.font())
        f.setPixelSize(15)
        f.setBold(True)
        self.lbl_pct.setFont(f)
        self.lbl_pct.setStyleSheet(f"color: {FG}; background: transparent;")

    def set_value(self, fraction, pct_text, stale=False):
        try:
            self._fraction = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            self._fraction = 0.0
        self._pct_text = str(pct_text or "")
        self._stale = bool(stale)
        self.lbl_pct.setText(self._pct_text)
        self.lbl_pct.setStyleSheet(
            f"color: {FG_STALE if self._stale else FG}; background: transparent;")
        self.update()

    def resizeEvent(self, ev):
        w = self.width()
        self.lbl_pct.setGeometry(w - BAR_PCT_W, 0, BAR_PCT_W, self.height())
        super().resizeEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        track_w = max(0, self.width() - BAR_PCT_W - BAR_GAP)
        top = (self.height() - BAR_H) / 2.0
        track = QRectF(0, top, track_w, BAR_H)
        p.setBrush(BAR_TRACK)
        p.drawRoundedRect(track, BAR_RADIUS, BAR_RADIUS)
        fill_w = track_w * self._fraction
        if fill_w > 0.5:
            p.setBrush(BAR_FILL_STALE if self._stale else BAR_FILL)
            p.drawRoundedRect(QRectF(0, top, fill_w, BAR_H), BAR_RADIUS, BAR_RADIUS)
        p.end()


class BalanceBubble(QWidget):
    """余额气泡窗。构造(meta, bus_dir, get_screen_rect)；get_screen_rect()
    返回屏幕可用区 QRect（None 时回落主屏）。对外：refresh_view()（main tick
    每拍调，签名去重）、show_for(pet_rect)/hide_bubble()/follow(pet_rect)。"""

    def __init__(self, meta, bus_dir, get_screen_rect=None):
        super().__init__()
        self.meta = meta or {}
        self.bus = Path(bus_dir)
        self._get_screen_rect = get_screen_rect
        self._view_path = self.bus / "balance_view.json"
        self._mtime = 0.0
        self._sig = None
        self._view = None            # 最近一次合法 view（dict）
        self._idx = 0                # 内存 provider 下标（重启回 default_id）
        self._mode = "waiting"       # waiting | empty | data
        self._peak = None            # 当前 provider 的峰谷推进态
        self._stale = False
        self.amount_text_full = ""   # 金额行完整文本（elide 前，测试可读）
        self.bottom_text_full = ""   # 底行完整文本（elide 前，测试可读）
        self.last_refresh_req_id = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(BUBBLE_W)

        # ---- 四段控件（绝对定位，行高固定，总高按内容算）----
        self.arrow_l = _Arrow("<", lambda: self.step_provider(-1), self)
        self.arrow_r = _Arrow(">", lambda: self.step_provider(1), self)
        self.lbl_title = QLabel("", self)
        self.lbl_title.setAlignment(Qt.AlignCenter)
        self.lbl_amount = QLabel("", self)
        # 余额气泡修订：lines 多行（title/amount/info），有 lines 时取代标题+金额行
        self.lbl_line_title = QLabel("", self)
        self.lbl_line_amount = QLabel("", self)
        self.lbl_line_info = QLabel("", self)
        self.bar_line = _ProgressBar(self)   # 进度条修正：第二行进度条+百分比
        self.lbl_peak_tag = QLabel("", self)
        self.lbl_peak_cd = QLabel("", self)
        self.lbl_bottom = QLabel("", self)
        self.lbl_msg = QLabel("", self)
        self.lbl_msg.setAlignment(Qt.AlignCenter)
        for lbl, px, bold in ((self.lbl_title, 13, False),
                              (self.lbl_amount, 20, True),
                              (self.lbl_line_title, 13, False),
                              (self.lbl_line_amount, 20, True),
                              (self.lbl_line_info, 13, False),
                              (self.lbl_peak_tag, 11, False),
                              (self.lbl_peak_cd, 11, False),
                              (self.lbl_bottom, 11, False),
                              (self.lbl_msg, 12, False)):
            f = QFont(lbl.font())
            f.setPixelSize(px)
            f.setBold(bold)
            lbl.setFont(f)
            lbl.setStyleSheet(f"color: {FG}; background: transparent;")
        self.lbl_bottom.setStyleSheet(f"color: {FG_DIM}; background: transparent;")
        self.lbl_peak_cd.setStyleSheet(f"color: {FG_DIM}; background: transparent;")

        # ---- 右键菜单（扁平两项，QAction 生于父菜单永不搬移，tray 同款）----
        self._menu = RoundMenu(parent=None)
        self._act_refresh = QAction("立即刷新", self._menu)
        self._act_refresh.triggered.connect(self.request_refresh)
        self._act_close = QAction("关闭气泡", self._menu)
        self._act_close.triggered.connect(self.hide_bubble)
        self._menu.addActions([self._act_refresh, self._act_close])

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)

        self.resize(BUBBLE_W, MSG_H)
        self._render()

    # ------------------------------------------------------------ 屏幕 ----
    def _screen_rect(self):
        if self._get_screen_rect is not None:
            try:
                r = self._get_screen_rect()
                if isinstance(r, QRect):
                    return r
            except Exception:
                pass
        scr = QApplication.primaryScreen()
        return scr.availableGeometry() if scr is not None else QRect(0, 0, 1920, 1080)

    # ------------------------------------------------------- view 读取 ----
    def refresh_view(self):
        """main tick 每拍调。mtime+内容签名双判（NTFS mtime 精度有限，
        同 mtime 改内容仍须刷新）；无变化早退不空转。返回是否重渲染。"""
        try:
            p = self._view_path
            if not p.is_file():
                if self._mode != "waiting" or self._view is not None:
                    self._view = None
                    self._mtime, self._sig = 0.0, None
                    self._mode = "waiting"
                    self._render()
                    return True
                return False
            mt = p.stat().st_mtime
            raw = p.read_text(encoding="utf-8")
            if mt == self._mtime and raw == self._sig:
                return False
            self._mtime, self._sig = mt, raw
            try:
                view = json.loads(raw)
            except Exception:
                view = None
            if not isinstance(view, dict) or not isinstance(view.get("providers"), list):
                self._view = None
                self._mode = "waiting"
                self._render()
                return True
            self._view = view
            self._mode = "empty" if not view["providers"] else "data"
            self._reselect_provider()
            self._cur_id = self.current_provider_id()
            self._sync_peak_state()
            self._render()
            return True
        except Exception:
            return False

    def _providers(self):
        return (self._view or {}).get("providers") or []

    def _current(self):
        provs = self._providers()
        if not provs:
            return None
        self._idx %= len(provs)
        return provs[self._idx]

    def _reselect_provider(self):
        """view 更新后按 id 保住当前选择；不在列 → 回 default_id → 首个。"""
        provs = self._providers()
        if not provs:
            self._idx = 0
            return
        cur_id = getattr(self, "_cur_id", None)
        ids = [str(p.get("id") or "") for p in provs]
        if cur_id in ids:
            self._idx = ids.index(cur_id)
            return
        default_id = str((self._view or {}).get("default_id") or "")
        self._idx = ids.index(default_id) if default_id in ids else 0

    def current_provider_id(self):
        prov = self._current()
        return str(prov.get("id") or "") if prov else ""

    def step_provider(self, delta):
        """箭头循环切换（内存态）。零 settings_req（A2：点击≠指令）。"""
        n = len(self._providers())
        if n <= 1:
            return
        self._idx = (self._idx + delta) % n
        self._cur_id = self.current_provider_id()
        self._sync_peak_state()
        self._render()

    # ------------------------------------------------------- 峰谷推进 ----
    def _sync_peak_state(self):
        """从 view 的 peak 块建推进态。边界优先级（§10.3）：
        next_boundary_ts > windows > rule；is_peak 服务端字段优先。"""
        prov = self._current()
        p = (prov or {}).get("peak")
        if not isinstance(p, dict):
            self._peak = None
            return
        now = time.time()
        rule = p.get("rule")
        windows = p.get("windows")
        holidays = p.get("holidays")
        try:
            bnd = float(p.get("next_boundary_ts")) if p.get("next_boundary_ts") is not None else None
        except (TypeError, ValueError):
            bnd = None
        if bnd is None or bnd <= now:
            if windows or rule:
                bnd = peak_rules.next_boundary(now, rule, windows, holidays)
            else:
                bnd = now  # 无规则可推：倒计时停 00:00:00 等宿主刷新
        isp = p.get("is_peak")
        if isp is None:
            isp = peak_rules.is_peak_at(now, rule, windows, holidays) if (windows or rule) else False
        self._peak = {"is_peak": bool(isp), "boundary": bnd,
                      "rule": rule, "windows": windows, "holidays": holidays,
                      "label_peak": str(p.get("label_peak") or "峰"),
                      "label_valley": str(p.get("label_valley") or "谷")}

    def _advance_peak(self, now):
        """跨边界：用 view 透传的同一份规则推进（单一事实源 peak_rules）。"""
        pk = self._peak
        if pk is None or now < pk["boundary"]:
            return
        if pk["windows"] or pk["rule"]:
            pk["is_peak"] = peak_rules.is_peak_at(now, pk["rule"], pk["windows"], pk["holidays"])
            pk["boundary"] = peak_rules.next_boundary(now, pk["rule"], pk["windows"], pk["holidays"])
        else:
            pk["boundary"] = now  # 无规则：钉在 00:00:00，等宿主新快照

    # --------------------------------------------------------- 渲染 ----
    def _elide(self, lbl, text):
        fm = QFontMetrics(lbl.font())
        return fm.elidedText(text, Qt.ElideRight, max(10, lbl.width()))

    def _render(self):
        data_widgets = (self.arrow_l, self.arrow_r, self.lbl_title,
                        self.lbl_amount, self.lbl_line_title, self.lbl_line_amount,
                        self.lbl_line_info, self.bar_line, self.lbl_peak_tag,
                        self.lbl_peak_cd, self.lbl_bottom)
        if self._mode != "data":
            for w in data_widgets:
                w.hide()
            self.lbl_msg.setText("未配置余额源" if self._mode == "empty" else "等待宿主数据")
            self.lbl_msg.setGeometry(PAD_X, 0, BUBBLE_W - 2 * PAD_X, MSG_H)
            self.lbl_msg.show()
            self.resize(BUBBLE_W, MSG_H)
            self.update()
            return
        self.lbl_msg.hide()
        prov = self._current() or {}
        n = len(self._providers())
        y = PAD_TOP
        # 标题行：< 平台名 >（仅 1 provider 隐藏箭头）
        multi = n > 1
        self.arrow_l.setGeometry(4, y, 24, 24)
        self.arrow_r.setGeometry(BUBBLE_W - 28, y, 24, 24)
        self.arrow_l.setVisible(multi)
        self.arrow_r.setVisible(multi)
        self.lbl_title.setGeometry(30, y, BUBBLE_W - 60, ROW_TITLE_H)
        self.lbl_title.setText(str(prov.get("name") or prov.get("id") or "?"))
        self.lbl_title.show()
        y += ROW_TITLE_H + ROW_GAP
        # 余额气泡修订：lines 结构化多行（作者原型图排版）取代金额行；无 lines 走旧单行。
        lines = prov.get("lines") if prov.get("ok", True) else None
        if isinstance(lines, list) and lines:
            self.lbl_amount.hide()
            y = self._render_lines(lines, y)
        else:
            self.lbl_line_title.hide()
            self.lbl_line_amount.hide()
            self.lbl_line_info.hide()
            self.bar_line.hide()
            # 金额行：ok=false → error 截断 24 字符；过期变灰
            if prov.get("ok", True):
                amount = str(prov.get("display") if prov.get("display") is not None
                             else (prov.get("value") if prov.get("value") is not None else "--"))
            else:
                err = str(prov.get("error") or "查询失败")
                amount = err[:ERROR_MAX_CHARS]
            self.amount_text_full = amount
            self.lbl_amount.setGeometry(PAD_X, y, BUBBLE_W - 2 * PAD_X, ROW_AMOUNT_H)
            self.lbl_amount.setText(self._elide(self.lbl_amount, amount))
            self.lbl_amount.setStyleSheet(
                f"color: {FG_STALE if self._stale else FG}; background: transparent;")
            self.lbl_amount.show()
            y += ROW_AMOUNT_H + ROW_GAP
        # 峰谷行：peak=null 整行 hide
        if self._peak is not None:
            pk = self._peak
            tag = f"[{pk['label_peak']}]" if pk["is_peak"] else f"[{pk['label_valley']}]"
            color = PEAK_RED if pk["is_peak"] else VALLEY_GREEN
            self.lbl_peak_tag.setGeometry(PAD_X, y, 40, ROW_PEAK_H)
            self.lbl_peak_tag.setText(tag)
            self.lbl_peak_tag.setStyleSheet(f"color: {color}; background: transparent;")
            self.lbl_peak_tag.show()
            self.lbl_peak_cd.setGeometry(PAD_X + 44, y, BUBBLE_W - 2 * PAD_X - 44, ROW_PEAK_H)
            self.lbl_peak_cd.show()
            self._render_countdown()
            y += ROW_PEAK_H + ROW_GAP
        else:
            self.lbl_peak_tag.hide()
            self.lbl_peak_cd.hide()
        # 底行小字：extra.today（若有）+ 缓存时间（+过期角标）
        self.lbl_bottom.setGeometry(PAD_X, y, BUBBLE_W - 2 * PAD_X, ROW_BOTTOM_H)
        self._render_bottom()
        self.lbl_bottom.show()
        self.resize(BUBBLE_W, y + ROW_BOTTOM_H + PAD_BOT)
        self.update()

    def _render_lines(self, lines, y):
        """余额气泡修订：作者原型图排版。按 lines 顺序逐行落位：title/amount/info 文本槽
        + bar 进度条槽（进度条修正：第二行是进度条+百分比）。返回下一行 y。
        amount/bar 槽过期变灰（_render_bottom 同款联动）。"""
        text_slots = {"title": (self.lbl_line_title, ROW_L_TITLE_H),
                      "amount": (self.lbl_line_amount, ROW_L_AMOUNT_H),
                      "info": (self.lbl_line_info, ROW_L_INFO_H)}
        seen = set()
        amount_full = ""
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            style = str(ln.get("style") or "info")
            if style == "bar":
                seen.add("bar")
                self.bar_line.setGeometry(PAD_X, y, BUBBLE_W - 2 * PAD_X, ROW_L_BAR_H)
                self.bar_line.set_value(ln.get("fraction"), ln.get("text"),
                                        stale=self._stale)
                self.bar_line.show()
                y += ROW_L_BAR_H + ROW_GAP
                continue
            if style not in text_slots:
                style = "info"
            seen.add(style)
            lbl, h = text_slots[style]
            text = str(ln.get("text") or "")
            if style == "amount":
                amount_full = text
            if not text:
                lbl.hide()
                continue
            lbl.setGeometry(PAD_X, y, BUBBLE_W - 2 * PAD_X, h)
            # 文本槽整行显示（作者原型图）：超宽缩字号适配，不截断
            base_px = 20 if style == "amount" else 13
            f = QFont(lbl.font())
            f.setPixelSize(base_px)
            fm = QFontMetrics(f)
            px = base_px
            while px > 9 and fm.horizontalAdvance(text) > lbl.width():
                px -= 1
                f.setPixelSize(px)
                fm = QFontMetrics(f)
            lbl.setFont(f)
            lbl.setText(text)
            color = FG_STALE if (self._stale and style == "amount") else FG
            lbl.setStyleSheet(f"color: {color}; background: transparent;")
            lbl.show()
            y += h + ROW_GAP
        # 未出现的槽隐藏（含 bar）
        for style, (lbl, _h) in text_slots.items():
            if style not in seen:
                lbl.hide()
        if "bar" not in seen:
            self.bar_line.hide()
        self.amount_text_full = amount_full
        return y

    def _render_countdown(self):
        if self._peak is None:
            return
        rem = max(0.0, self._peak["boundary"] - time.time())
        self.lbl_peak_cd.setText(peak_rules.fmt_countdown(rem))

    def _render_bottom(self):
        if self._view is None:
            return
        now = time.time()
        try:
            view_ts = float(self._view.get("ts") or 0)
        except (TypeError, ValueError):
            view_ts = 0.0
        try:
            refresh_s = float(self._view.get("refresh_s") or DEFAULT_REFRESH_S)
        except (TypeError, ValueError):
            refresh_s = DEFAULT_REFRESH_S
        age = max(0.0, now - view_ts)
        self._stale = age > 2 * refresh_s
        prov = self._current() or {}
        extra = prov.get("extra") or {}
        parts = []
        today = extra.get("today") if isinstance(extra, dict) else None
        if today:
            parts.append(str(today))
        parts.append(f"缓存于 {int(age // 60)} 分前")
        if self._stale:
            parts.append("数据可能过期")
        self.bottom_text_full = " · ".join(parts)
        self.lbl_bottom.setText(self._elide(self.lbl_bottom, self.bottom_text_full))
        self.lbl_amount.setStyleSheet(
            f"color: {FG_STALE if self._stale else FG}; background: transparent;")
        self.lbl_line_amount.setStyleSheet(
            f"color: {FG_STALE if self._stale else FG}; background: transparent;")
        # bar 行过期联动：重设 stale 触发重绘（填充色+百分比色同步变灰）
        if not self.bar_line.isHidden():
            self.bar_line.set_value(self.bar_line._fraction,
                                    self.bar_line._pct_text, stale=self._stale)

    def _on_tick(self):
        """QTimer 1s：倒计时本地走 + 跨边界推进 + 底行缓存时间/过期刷新。"""
        if not self.isVisible() or self._mode != "data":
            return
        now = time.time()
        self._advance_peak(now)
        if self._peak is not None:
            pk = self._peak
            tag = f"[{pk['label_peak']}]" if pk["is_peak"] else f"[{pk['label_valley']}]"
            color = PEAK_RED if pk["is_peak"] else VALLEY_GREEN
            self.lbl_peak_tag.setText(tag)
            self.lbl_peak_tag.setStyleSheet(f"color: {color}; background: transparent;")
            self._render_countdown()
        self._render_bottom()

    # ------------------------------------------------------- 刷新请求 ----
    def request_refresh(self):
        """右键"立即刷新"：写 balance_refresh_req.json（§10.2，原子 tmp+rename）。
        provider_id=当前 provider（空=全量）。宿主节流 <10s 忽略，worker 不管。"""
        rid = uuid.uuid4().hex[:8]
        try:
            p = self.bus / "balance_refresh_req.json"
            tmp = p.with_name("balance_refresh_req.json.tmp")
            tmp.write_text(json.dumps({"id": rid, "ts": time.time(),
                                       "provider_id": self.current_provider_id()},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
            self.last_refresh_req_id = rid
        except Exception:
            pass
        return rid

    # --------------------------------------------------------- 几何 ----
    def follow(self, pet_rect):
        """按 spike 算法锚定到宠物矩形（walk/climb/drag 逐帧调用）。"""
        pos, _ = bubble_pos(pet_rect, self.width(), self.height(), self._screen_rect())
        self.move(pos)
        self.raise_()

    def show_for(self, pet_rect):
        self.refresh_view()
        self.follow(pet_rect)
        self.show()
        self.raise_()
        self._on_tick()
        self._timer.start()

    def hide_bubble(self):
        self._timer.stop()
        self.hide()

    # --------------------------------------------------------- 事件 ----
    def contextMenuEvent(self, ev):
        self._menu.exec(QCursor.pos())

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(BG)
        p.drawRoundedRect(0, 0, self.width(), self.height(), RADIUS, RADIUS)
        p.end()