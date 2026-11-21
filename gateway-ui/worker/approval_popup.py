# -*- coding: utf-8 -*-
"""approval_popup.py — 审批弹窗 ApprovalPopup（PROMPT §12 / CONTRACT §11，R28-3/R28-4）。

worker 进程内 Qt 组件：读宿主 approval_req.json（pending 全量快照），每条请求一窗，
屏幕右侧中部飞入（UAC 高仿形态，自绘盾牌，非系统 UAC）；底部四按钮固定一字排开
「永远放行 / 会话放行 / 单次放行 / 拒绝」，choice ∉ allowed_choices 置灰。点击 →
经本地 IPC（approval_ipc.py）送达宿主（五重校验，见 CONTRACT §11.2），审批决定
不落共享文件 → 宿主 present 取回注入 decision。消费 approval_withdrawn.jsonl 收窗
（宿主已判/撤销）。

纪律：
- 弹窗不可拖拽、Esc/弃焦无效（审批面不许被藏起来，超时归宿主判）；
- 点击零 settings_req（A2：按钮注入的是 approval decision，不是配置指令）；
- popup_enabled=false → 不弹真窗（样例窗不受门控，纯预览）；
- 样例窗（show_sample）按钮只收窗不写总线（演示用，绝不注入真信号）；
- 并发 >4 条折叠计数角标；最近者占主位（屏幕右侧中部），旧者上移堆叠。

不 import main（避免循环）；get_screen_rect 回调注入屏幕矩形（offscreen 可测）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from approval_ipc import ApprovalResponseClient

from PySide6.QtCore import (Qt, QPoint, QPointF, QRect, QTimer,
                            QPropertyAnimation, QEasingCurve)
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

# ---- 视觉/几何常量 ----
POPUP_W = 420
POPUP_H = 300
RADIUS = 8
MARGIN = 16                 # 距屏幕右缘
GAP = 12                    # 堆叠窗间距
MAX_VISIBLE = 4             # 超过折叠计数角标
SLIDE_MS = 180
PAD = 16
BTN_GAP = 8
BTN_H = 36
BG = QColor(32, 34, 40, 245)
TITLE_BG = QColor(43, 47, 56, 255)
FG = "#f2f3f5"
FG_DIM = "#9aa0a8"
ACCENT = "#4cc2ff"
DENY_RED = "#e0433f"
# 四按钮固定序（label, choice）——R28-3 主人裁定一字排开
BUTTONS = (("永远放行", "always"), ("会话放行", "session"),
           ("单次放行", "once"), ("拒绝", "deny"))
CHOICE_BY_LABEL = {label: choice for label, choice in BUTTONS}


def popup_target_pos(screen: QRect, w: int, h: int, slot: int = 0) -> QPoint:
    """屏幕右侧中部纯函数：x=右缘内缩 MARGIN，y=垂直居中再按 slot 上移堆叠。
    clamp 进屏（slot 过大上移出屏顶时钉屏顶）。screen.right()=left+w-1 口径。"""
    x = screen.right() - w - MARGIN + 1
    x = max(screen.left(), min(x, screen.right() - w + 1))
    cy = screen.top() + screen.height() // 2
    y = cy - h // 2 - slot * (h + GAP)
    y = max(screen.top(), min(y, screen.top() + screen.height() - h))
    return QPoint(x, y)


def fmt_remaining(seconds: float) -> str:
    """倒计时文案：>0 → '剩余 Ns'；<=0 → '已超时（等待宿主判定）'。"""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        s = 0
    if s > 0:
        return f"剩余 {s}s"
    return "已超时（等待宿主判定）"


def shield_pixmap(size: int = 24) -> QPixmap:
    """自绘 UAC 风格盾牌（蓝黄四象限，仅形态致敬，不提取微软版权素材）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 24.0
    # 盾形轮廓（顶宽底尖）
    shield = QPolygonF([
        QPointF(12 * s, 1 * s), QPointF(22 * s, 5 * s), QPointF(22 * s, 12 * s),
        QPointF(12 * s, 23 * s), QPointF(2 * s, 12 * s), QPointF(2 * s, 5 * s),
    ])
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0x2f, 0x6f, 0xed))   # 蓝底
    p.drawPolygon(shield)
    # 右半黄（UAC 蓝黄对角）
    p.setBrush(QColor(0xf5, 0xc2, 0x42))
    half = QPolygonF([
        QPointF(12 * s, 1 * s), QPointF(22 * s, 5 * s), QPointF(22 * s, 12 * s),
        QPointF(12 * s, 23 * s),
    ])
    p.drawPolygon(half)
    p.end()
    return pm


class ApprovalPopupWindow(QWidget):
    """单条审批请求的 UAC 式弹窗。构造(req, on_choice, sample, get_screen_rect, animate)。
    on_choice(rid, digest, choice) 由管理器注入；sample=True 时按钮只收窗不写总线。"""

    def __init__(self, req: dict, on_choice=None, sample: bool = False,
                 get_screen_rect=None, animate: bool = True):
        super().__init__()
        self.req = dict(req or {})
        self.rid = str(self.req.get("request_id") or "")
        self.digest = str(self.req.get("digest") or "")
        self.sample = bool(sample)
        self._on_choice = on_choice
        self._get_screen_rect = get_screen_rect
        self._animate = bool(animate)
        self._slot = 0
        self._closing = False
        self.last_choice = None       # 样例/取证可读
        self._anim = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(POPUP_W, POPUP_H)
        self.setToolTip("gateway-ui 审批窗（非系统 UAC）")

        allowed = self.req.get("allowed_choices") or ["once", "deny"]
        try:
            allowed = [str(c) for c in allowed]
        except TypeError:
            allowed = ["once", "deny"]

        # ---- 标题栏 ----
        self.lbl_shield = QLabel(self)
        self.lbl_shield.setPixmap(shield_pixmap(24))
        self.lbl_shield.setGeometry(12, 8, 24, 24)
        self.lbl_shield.setStyleSheet("background: transparent;")
        self.lbl_title = QLabel("用户账户控制", self)
        self.lbl_title.setGeometry(44, 0, POPUP_W - 60, 40)
        f = QFont(self.lbl_title.font()); f.setPixelSize(15); f.setBold(True)
        self.lbl_title.setFont(f)
        self.lbl_title.setStyleSheet(f"color: {FG}; background: transparent;")

        # ---- 正文 ----
        self.lbl_question = QLabel("你要允许此应用对你的设备进行更改吗？", self)
        self.lbl_question.setGeometry(PAD, 48, POPUP_W - 2 * PAD, 22)
        self._set_font(self.lbl_question, 13, False, FG)

        self.lbl_command = QLabel(str(self.req.get("command") or ""), self)
        self.lbl_command.setGeometry(PAD, 76, POPUP_W - 2 * PAD, 64)
        self.lbl_command.setWordWrap(True)
        self.lbl_command.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        fc = QFont("Consolas"); fc.setPixelSize(13)
        self.lbl_command.setFont(fc)
        self.lbl_command.setStyleSheet(
            f"color: {FG}; background: rgba(0,0,0,90); border-radius: 4px; padding: 4px;")

        self.lbl_desc = QLabel(str(self.req.get("description") or ""), self)
        self.lbl_desc.setGeometry(PAD, 148, POPUP_W - 2 * PAD, 40)
        self.lbl_desc.setWordWrap(True)
        self._set_font(self.lbl_desc, 12, False, FG_DIM)

        surf = str(self.req.get("surface") or "?")
        sk = str(self.req.get("session_key") or "")[:8]
        self.lbl_source = QLabel(f"来源: {surf} · 会话 {sk}", self)
        self.lbl_source.setGeometry(PAD, 192, POPUP_W - 2 * PAD, 18)
        self._set_font(self.lbl_source, 11, False, FG_DIM)

        self.lbl_countdown = QLabel("", self)
        self.lbl_countdown.setGeometry(PAD, 212, POPUP_W - 2 * PAD, 18)
        self._set_font(self.lbl_countdown, 11, False, ACCENT)

        self.lbl_badge = QLabel("", self)
        self.lbl_badge.setGeometry(POPUP_W - 60, 212, 44, 18)
        self.lbl_badge.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._set_font(self.lbl_badge, 11, True, FG_DIM)

        # ---- 四按钮（固定序，置灰按 allowed_choices）----
        self.buttons = {}
        bw = (POPUP_W - 2 * PAD - 3 * BTN_GAP) // 4
        by = POPUP_H - PAD - BTN_H
        for i, (label, choice) in enumerate(BUTTONS):
            btn = QPushButton(label, self)
            btn.setGeometry(PAD + i * (bw + BTN_GAP), by, bw, BTN_H)
            btn.setEnabled(choice in allowed)
            btn.setCursor(Qt.PointingHandCursor if choice in allowed else Qt.ArrowCursor)
            if choice == "deny":
                btn.setStyleSheet(self._btn_qss(DENY_RED))
            else:
                btn.setStyleSheet(self._btn_qss(ACCENT))
            btn.clicked.connect(lambda _=False, c=choice: self._choose(c))
            self.buttons[choice] = btn

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_countdown)
        self._tick_countdown()

    @staticmethod
    def _set_font(lbl, px, bold, color):
        f = QFont(lbl.font()); f.setPixelSize(px); f.setBold(bold)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color: {color}; background: transparent;")

    @staticmethod
    def _btn_qss(accent):
        return (
            "QPushButton {"
            f" color: {FG}; background: rgba(255,255,255,28); border: 1px solid {accent};"
            " border-radius: 4px; font-size: 13px; }"
            "QPushButton:hover { background: rgba(255,255,255,50); }"
            "QPushButton:disabled { color: #6a6f78; background: rgba(255,255,255,10);"
            " border: 1px solid #4a4f58; }")

    # ------------------------------------------------------------ 几何 ----
    def _screen_rect(self) -> QRect:
        if self._get_screen_rect is not None:
            try:
                r = self._get_screen_rect()
                if isinstance(r, QRect):
                    return r
            except Exception:
                pass
        scr = QApplication.primaryScreen()
        return scr.availableGeometry() if scr is not None else QRect(0, 0, 1920, 1080)

    def target_pos(self) -> QPoint:
        return popup_target_pos(self._screen_rect(), self.width(), self.height(), self._slot)

    def set_slot(self, slot: int):
        self._slot = int(slot)
        if self.isVisible() and not self._closing:
            self.move(self.target_pos())

    def show_slide(self):
        """飞入：从屏右缘外滑到目标位（animate=False 直接落位，offscreen 测试用）。"""
        target = self.target_pos()
        scr = self._screen_rect()
        start = QPoint(scr.right() + 1, target.y())
        self.move(start)
        self.show()
        self.raise_()
        self._timer.start()
        if not self._animate:
            self.move(target)
            return
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setStartValue(start)
        self._anim.setEndValue(target)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()

    def _finish_slide(self):
        """测试钩子：停动画直接落目标位。"""
        if self._anim is not None:
            self._anim.stop()
        self.move(self.target_pos())

    def slide_out(self):
        """滑出收窗（animate=False 立即 hide）。"""
        if self._closing:
            return
        self._closing = True
        self._timer.stop()
        if not self._animate:
            self.hide()
            return
        scr = self._screen_rect()
        end = QPoint(scr.right() + 1, self.y())
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.finished.connect(self.hide)
        self._anim.start()

    # ------------------------------------------------------------ 交互 ----
    def _choose(self, choice: str):
        if self._closing:
            return
        self.last_choice = choice
        if self._on_choice is not None:
            try:
                self._on_choice(self.rid, self.digest, choice)
            except Exception:
                pass
        self.slide_out()

    def _tick_countdown(self):
        try:
            exp = float(self.req.get("expires_ts") or 0)
        except (TypeError, ValueError):
            exp = 0
        self.lbl_countdown.setText(fmt_remaining(exp - time.time()))

    def set_badge(self, text: str):
        self.lbl_badge.setText(text)

    def keyPressEvent(self, ev):
        # 审批面不响应 Esc/弃焦（超时归宿主判）——忽略按键，绝不收窗。
        ev.ignore()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(BG)
        p.drawRoundedRect(0, 0, self.width(), self.height(), RADIUS, RADIUS)
        p.setBrush(TITLE_BG)
        p.drawRoundedRect(0, 0, self.width(), 40, RADIUS, RADIUS)
        p.drawRect(0, 20, self.width(), 20)   # 标题栏下半补方角
        p.end()

    def closeEvent(self, ev):
        self._timer.stop()
        super().closeEvent(ev)


class ApprovalPopupManager:
    """审批弹窗管理器（main tick 每拍 sync）。读 req/withdrawn，维护弹窗堆叠，
    写 resp（worker 单写者）。get_cfg() 返回 approval 配置块（popup_enabled 门控）。"""

    def __init__(self, meta, bus_dir, get_cfg, get_screen_rect=None, animate=True):
        self.meta = meta or {}
        self.bus = Path(bus_dir)
        self._get_cfg = get_cfg
        self._get_screen_rect = get_screen_rect
        self._animate = bool(animate)
        self.popups = {}            # rid -> ApprovalPopupWindow（真窗）
        self._sample = None         # 样例窗（独立，不受门控/req 同步影响）
        self._answered = set()      # 已点过的 rid（防 host 消费前 sync 重建）
        self._req_mtime = 0.0
        self._req_sig = None
        self._req_ids = set()
        self._withdrawn_pos = 0
        self.last_resp = None       # 取证：最近写的 resp dict

    # ------------------------------------------------------------ 同步 ----
    def sync(self):
        try:
            enabled = bool((self._get_cfg() or {}).get("popup_enabled", False))
            requests = self._read_req()
            self._req_ids = {str(r.get("request_id")) for r in requests}
            # 已答复且 host 已消费（离开 req）→ 清 answered 标记
            self._answered &= self._req_ids
            if not enabled:
                for w in list(self.popups.values()):
                    w.slide_out()
                self.popups.clear()
            else:
                self._sync_popups(requests)
            self._consume_withdrawn()
        except Exception:
            pass

    def _read_req(self) -> list:
        p = self.bus / "approval_req.json"
        if not p.is_file():
            self._req_mtime, self._req_sig = 0.0, None
            return []
        mt = p.stat().st_mtime
        raw = p.read_text(encoding="utf-8")
        if mt == self._req_mtime and raw == self._req_sig:
            return self._last_requests()
        self._req_mtime, self._req_sig = mt, raw
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        reqs = data.get("requests") if isinstance(data, dict) else None
        self._req_cache = [r for r in (reqs or []) if isinstance(r, dict)]
        return self._req_cache

    def _last_requests(self) -> list:
        return getattr(self, "_req_cache", [])

    def _sync_popups(self, requests: list):
        # 最近者占主位：host 按插入序写（新者末），反转使新者 slot 0
        ordered = list(reversed(requests))
        keep = set()
        for i, req in enumerate(ordered):
            rid = str(req.get("request_id") or "")
            if not rid or rid in self._answered:
                continue
            keep.add(rid)
            win = self.popups.get(rid)
            if win is None:
                win = ApprovalPopupWindow(
                    req, on_choice=self._on_choice, sample=False,
                    get_screen_rect=self._get_screen_rect, animate=self._animate)
                self.popups[rid] = win
                win.set_slot(i)
                win.show_slide()
            else:
                win.set_slot(i)
            win.setVisible(i < MAX_VISIBLE)
        # 离开 req 的窗收掉
        for rid in list(self.popups):
            if rid not in keep:
                self.popups[rid].slide_out()
                del self.popups[rid]
        # 折叠计数角标（>MAX_VISIBLE 时最新窗显示 +N）
        extra = len(ordered) - MAX_VISIBLE
        if ordered:
            newest_rid = str(ordered[0].get("request_id") or "")
            w = self.popups.get(newest_rid)
            if w is not None:
                w.set_badge(f"+{extra}" if extra > 0 else "")

    def _consume_withdrawn(self):
        p = self.bus / "approval_withdrawn.jsonl"
        if not p.is_file():
            self._withdrawn_pos = 0
            return
        try:
            size = p.stat().st_size
            if size < self._withdrawn_pos:
                self._withdrawn_pos = 0   # 文件被截断/轮转：重头读
            with p.open("r", encoding="utf-8") as f:
                f.seek(self._withdrawn_pos)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    rid = str(row.get("request_id") or "")
                    w = self.popups.pop(rid, None)
                    if w is not None:
                        w.slide_out()
                    self._answered.discard(rid)
                self._withdrawn_pos = f.tell()
        except Exception:
            pass

    # ------------------------------------------------------------ 注入 ----
    def _on_choice(self, rid: str, digest: str, choice: str):
        """真窗按钮 → 写 resp（worker 单写者，按 req 快照修剪陈旧条目）+ 收窗。"""
        self._answered.add(rid)
        w = self.popups.pop(rid, None)
        if w is not None:
            w.slide_out()
        self._write_resp(rid, digest, choice)

    def _write_resp(self, rid: str, digest: str, choice: str):
        # 审批决定不落共享目录：同用户进程可伪造/重放 approval_resp.json。
        # endpoint 可公开，token 只由宿主经 stdin 握手注入 worker 内存。
        endpoint = str(self.meta.get("approval_endpoint") or "")
        token = str(self.meta.get("approval_handshake") or "")
        spawn_token = str(self.meta.get("spawn_token") or "")
        payload = {"request_id": rid, "digest": digest, "choice": choice,
                   "token": token, "spawn_token": spawn_token}
        try:
            result = ApprovalResponseClient.send(endpoint, payload)
            self.last_resp = {"request_id": rid, "choice": choice,
                              "ok": bool(result.get("ok"))}
        except Exception:
            self.last_resp = {"request_id": rid, "choice": choice, "ok": False}

    # ------------------------------------------------------------ 样例 ----
    def show_sample(self):
        """设置页「弹出样例窗口」：纯预览，按钮只收窗不写总线（A2）。"""
        if self._sample is not None:
            try:
                self._sample.slide_out()
            except Exception:
                pass
        req = {
            "request_id": "sample",
            "digest": "sample-digest",
            "command": "rm -rf /path/to/project  （样例命令，不会执行）",
            "description": "这是一个样例审批弹窗，用于预览 UAC 式形态与四按钮。"
                           "点击任意按钮只会关闭本窗，不注入任何真实信号。",
            "surface": "sample",
            "session_key": "sample00",
            "pattern_key": "sample",
            "allowed_choices": ["once", "session", "always", "deny"],
            "timeout_seconds": 300,
            "expires_ts": time.time() + 300,
        }
        self._sample = ApprovalPopupWindow(
            req, on_choice=self._on_sample_choice, sample=True,
            get_screen_rect=self._get_screen_rect, animate=self._animate)
        self._sample.set_slot(0)
        self._sample.show_slide()
        return self._sample

    def _on_sample_choice(self, rid, digest, choice):
        # 样例：只记录 + 收窗，绝不写总线。
        self._sample = None

    def close_all(self):
        for w in list(self.popups.values()):
            w.slide_out()
        self.popups.clear()
        if self._sample is not None:
            self._sample.slide_out()
            self._sample = None
