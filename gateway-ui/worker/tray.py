# -*- coding: utf-8 -*-
"""gateway-ui worker 托盘层（CONTRACT §7）。

QSystemTrayIcon + RoundMenu（parent=None，托盘非 QWidget）。图标右下状态灯
绿/橙/红/灰。tooltip：gateway-ui · <pet_state> · <tier> | uptime XhYYm | 最近降级...
托盘菜单扁平 8 项（顺序固定）：版本(灰)/状态监控(灰)/桌宠▸(共享 PetMenu)/
打开日志/设置(灰,F7 预留)/设置(VSCode)/重启/退出。
重启/退出 = DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP 逃逸壳执行 meta 里的
restart_cmd/stop_cmd，showMessage 即时反馈，8s 后 app.quit()。worker 绝不 spawn worker。
菜单弹出位置：左下角对准鼠标（sizeHint 换算 y-h，屏顶翻转/屏右钳制）。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QCursor
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from qfluentwidgets import RoundMenu

LIGHT_RGB = {"green": (46, 204, 113), "orange": (243, 156, 18),
             "red": (231, 76, 60), "gray": (150, 150, 150)}
STATE_TTL = 8.0

# ---- 事件反应：会话事件托盘闪动 ----
FLASH_INTERVAL_MS = 300          # 交替周期 ~300ms
FLASH_DURATION_S = 6.0           # 默认闪动时长，到点自动恢复正常图标
# notice kind → 高亮色（done 绿 / failed 红 / await_input 琥珀 / stopped 灰蓝）。
# 未知 kind 回落绿——高亮画法=亮色描边+右下角标，offscreen 可像素级断言。
FLASH_COLORS = {"done": (46, 204, 113), "failed": (231, 76, 60),
                "await_input": (241, 196, 15), "stopped": (149, 165, 166)}

# main.py 在 _main_inner 里把同一对象接进来（META/STATE 单一事实源在 main）。
META = {}
STATE = {"data": None, "ts": 0.0}
LIFECYCLE_PENDING = {"until": 0.0}


def vlog(msg: str) -> None:
    """verbose 开关从 state.json 读（宿主落盘透传），关闭=零写入。
    WK 前缀写 bus/debug.log（桌宠物理 PET 前缀另写 pet_debug.log）。"""
    try:
        if not (STATE.get("data") or {}).get("verbose"):
            return
        f = Path(META["bus_dir"]) / "debug.log"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} WK {msg}\n")
    except Exception:
        pass


def tray_pixmap(light: str, size=32) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 32.0
    p.setPen(QColor(120, 130, 160))
    p.setBrush(QColor(52, 58, 74))
    p.drawRoundedRect(int(1 * s), int(1 * s), int(30 * s), int(30 * s),
                      int(7 * s), int(7 * s))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(210, 220, 240))
    p.drawEllipse(int(10 * s), int(8 * s), int(12 * s), int(12 * s))
    p.drawEllipse(int(13 * s), int(12 * s), int(2 * s), int(2 * s))
    p.drawEllipse(int(18 * s), int(12 * s), int(2 * s), int(2 * s))
    rgb = LIGHT_RGB.get(light, LIGHT_RGB["green"])
    p.setBrush(QColor(*rgb))
    p.setPen(QColor(255, 255, 255))
    p.drawEllipse(int(20 * s), int(20 * s), int(10 * s), int(10 * s))
    p.end()
    return pm


def state_light() -> str:
    if time.time() - STATE["ts"] > STATE_TTL:
        return "gray"
    return ((STATE["data"] or {}).get("tray") or {}).get("light", "green")


# ------------------------------------------------------- 事件反应 flash 纯函数 ----
def flash_color(kind: str) -> tuple:
    """notice kind → 高亮 RGB；未知 kind 回落 done 绿（绝不抛）。"""
    return FLASH_COLORS.get(kind, FLASH_COLORS["done"])


def flash_frame(highlight: bool, light: str, kind: str, size=32) -> QPixmap:
    """闪动单帧（纯函数，offscreen 可测）：highlight=False=正常图标；
    True=正常图标 + kind 亮色描边 + 右下角标。不改 tray_pixmap 本体。"""
    pm = tray_pixmap(light, size)
    if not highlight:
        return pm
    rgb = flash_color(kind)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 32.0
    # 亮色描边（2px 圆角框，套在原图标外缘内侧）
    p.setBrush(Qt.NoBrush)
    pen = p.pen()
    pen.setColor(QColor(*rgb))
    pen.setWidthF(max(1.5, 2.0 * s))
    p.setPen(pen)
    p.drawRoundedRect(int(1 * s), int(1 * s), int(30 * s), int(30 * s),
                      int(7 * s), int(7 * s))
    # 右下角标（实心圆 + 白描边，与状态灯错位不遮灯）
    p.setPen(QColor(255, 255, 255))
    p.setBrush(QColor(*rgb))
    p.drawEllipse(int(2 * s), int(22 * s), int(8 * s), int(8 * s))
    p.end()
    return pm


def flash_frames(light: str, kind: str, n=4, size=32) -> list:
    """交替帧序列 [正常, 高亮, 正常, 高亮, …]（纯函数，单测断言交替与数量）。"""
    return [flash_frame(i % 2 == 1, light, kind, size) for i in range(max(0, n))]


class TrayFlash:
    """托盘闪动控制器（QTimer ~300ms 交替正常/高亮，duration 后自动恢复）。
    start_flash(kind, duration_s) 重复调用=刷新（换色+重新计时）。
    tray_icon=None 时纯状态机（offscreen 单测不碰真托盘）。绝不改菜单/既有图标逻辑。"""

    def __init__(self, tray_icon=None, light_fn=None):
        self._icon = tray_icon
        self._light_fn = light_fn or state_light
        self.active = False
        self.kind = ""
        self.highlight = False
        self.ends_at = 0.0
        self._ticks = 0
        self._timer = QTimer()
        self._timer.setInterval(FLASH_INTERVAL_MS)
        self._timer.timeout.connect(self._step)

    def start_flash(self, kind: str, duration_s: float = FLASH_DURATION_S) -> None:
        self.kind = str(kind or "")
        self.active = True
        self.highlight = True      # 首帧即高亮（事件即时可见）
        self._ticks = 0
        self.ends_at = time.time() + max(0.1, float(duration_s))
        self._apply()
        self._timer.start()

    def _step(self) -> None:
        self._ticks += 1
        if time.time() >= self.ends_at:
            self.stop()
            return
        self.highlight = not self.highlight
        self._apply()

    def _apply(self) -> None:
        if self._icon is None:
            return
        try:
            light = self._light_fn()
            self._icon.setIcon(QIcon(flash_frame(self.highlight, light, self.kind)))
        except Exception:
            pass

    def stop(self) -> None:
        self._timer.stop()
        self.active = False
        self.highlight = False
        if self._icon is not None:
            try:
                self._icon.setIcon(QIcon(tray_pixmap(self._light_fn())))
            except Exception:
                pass


def tooltip() -> str:
    st = STATE["data"] or {}
    tray = st.get("tray") or {}
    seg = [f"gateway-ui · {st.get('pet_state', 'idle')} · {tray.get('tier', '')}"]
    up = st.get("gateway_uptime_s")
    if up is None:
        up = max(0.0, time.time() - float(st.get("boot_ts") or time.time()))
    seg.append(f"uptime {int(up) // 3600}h{int(up) % 3600 // 60:02d}m")
    deg = tray.get("degrade")
    if deg and time.time() - deg.get("ts", 0) <= STATE_TTL * 10:
        age = time.time() - deg.get("ts", 0)
        seg.append(f"最近降级: {deg['from']}->{deg['to']}"
                   f"（{'刚刚' if age < 90 else str(int(age // 60)) + 'min 前'}）")
    return " | ".join(seg)[:127]


# ------------------------------------------------------------- actions ----
def _detached(cmd):
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    subprocess.Popen(cmd, cwd=META.get("cwd"), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


def act_log():
    d = META.get("log_dir")
    if d and os.path.isdir(d):
        subprocess.Popen(["explorer", d] if os.name == "nt" else ["xdg-open", d])


def act_vscode():
    from shutil import which
    exe = next((which(c) for c in ("code", "code.cmd", "code.exe") if which(c)), None)
    if exe:
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen([exe, META.get("hermes_home", ".")], cwd=META.get("cwd"),
                         creationflags=DETACHED_PROCESS if os.name == "nt" else 0,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def act_restart(tray_icon):
    LIFECYCLE_PENDING["until"] = time.time() + 8.0
    try:
        _detached(META["restart_cmd"])
        tray_icon.showMessage("gateway-ui", "重启指令已发出",
                              QSystemTrayIcon.Information, 2500)
    except Exception as exc:
        tray_icon.showMessage("gateway-ui", f"发出失败: {exc}",
                              QSystemTrayIcon.Warning, 3000)


def act_stop(tray_icon):
    LIFECYCLE_PENDING["until"] = time.time() + 8.0
    try:
        _detached(META["stop_cmd"])
        tray_icon.showMessage("gateway-ui", "停止指令已发出",
                              QSystemTrayIcon.Information, 2500)
    except Exception as exc:
        tray_icon.showMessage("gateway-ui", f"发出失败: {exc}",
                              QSystemTrayIcon.Warning, 3000)


def vscode_enabled():
    from shutil import which
    return any(which(c) for c in ("code", "code.cmd", "code.exe"))


def log_enabled():
    return bool(META.get("log_dir")) and os.path.isdir(META.get("log_dir") or "")


def build_fluent_menu(app: QApplication, tray_icon: QSystemTrayIcon,
                      pet_menu=None, on_settings=None) -> RoundMenu:
    """扁平 8 项（顺序固定，CONTRACT §7）。pet_menu=共享 PetMenu 实例
    （托盘槽位与桌宠右键同一实例；绝不搬移/重建 QAction）。
    on_settings=设置项回调（设置界面 F7：打开 SettingsWindow；None 时该项禁用）。"""
    menu = RoundMenu(parent=None)  # Fluent 圆角样式（托盘非 QWidget，不能当 parent）
    a_ver = QAction(f"Hermes Gateway v{META.get('version', '?')}", menu)
    a_ver.setEnabled(False)
    a_status = QAction("状态监控: 暂无异常", menu)
    a_status.setEnabled(False)
    a_log = QAction("打开日志", menu)
    a_log.setEnabled(log_enabled())
    a_log.triggered.connect(act_log)
    a_settings = QAction("设置", menu)   # 设置界面 F7 启用：打开 Fluent 设置窗口
    a_settings.setEnabled(on_settings is not None)
    if on_settings is not None:
        a_settings.triggered.connect(lambda: on_settings())
    a_vs = QAction("设置(VSCode)", menu)
    a_vs.setEnabled(vscode_enabled())
    a_vs.triggered.connect(act_vscode)
    a_restart = QAction("重启", menu)
    a_restart.triggered.connect(lambda: act_restart(tray_icon))
    a_stop = QAction("退出", menu)
    a_stop.triggered.connect(lambda: act_stop(tray_icon))
    menu.addActions([a_ver, a_status])
    if pet_menu is not None:
        menu.addMenu(pet_menu.menu)  # 共享实例：托盘槽位与桌宠右键同一菜单
    menu.addSeparator()
    menu.addActions([a_log, a_settings, a_vs])
    menu.addSeparator()
    menu.addActions([a_restart, a_stop])
    menu._actions_map = {"status": a_status, "version": a_ver}
    return menu


def popup_bottom_left(menu):
    """弹出位置定稿：菜单弹出位置的左下角对准鼠标。Qt 的 popup(pos) 是菜单"左上角
    锚点"；先用 sizeHint 取实际高宽，把锚点换算成 pos.y - h、pos.x 左对齐，
    再手动 move 后 exec（不走 adjustPosition，防 Qt 把越界位拽回鼠标旁边）。"""
    c = QCursor.pos()
    menu.ensurePolished()
    h = menu.sizeHint().height()
    w = menu.sizeHint().width()
    x = c.x()
    y = c.y() - h
    # 屏内钳制（贴屏幕顶时翻到鼠标下方）
    screen = menu.screen() or QApplication.primaryScreen()
    if screen is not None:
        g = screen.geometry()
        if x + w > g.right():
            x = g.right() - w
        if y < g.top():
            y = c.y() + 4
    menu.move(x, y)
    menu.exec(QPoint(x, y))
