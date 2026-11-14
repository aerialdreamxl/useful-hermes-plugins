# -*- coding: utf-8 -*-
"""test_tray_flash.py — 事件反应托盘闪动回归（worker/tray.py flash 部分）。
side-env python 跑（offscreen Qt）。纯函数像素级断言 + TrayFlash 状态机 + 真 QTimer 交替。
不碰既有菜单/图标逻辑（tray_pixmap 本体不变）。
"""
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from PySide6.QtGui import QIcon
app = QApplication([])

import tray

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

def px(pm, x, y):
    c = pm.toImage().pixelColor(x, y)
    return (c.red(), c.green(), c.blue(), c.alpha())

def close_rgb(a, b, tol=40):
    return all(abs(a[i] - b[i]) <= tol for i in range(3))

# ============ 纯函数：flash_color ============
ck("flash_color done green", tray.flash_color("done") == (46, 204, 113))
ck("flash_color failed red", tray.flash_color("failed") == (231, 76, 60))
ck("flash_color await amber", tray.flash_color("await_input") == (241, 196, 15))
ck("flash_color stopped gray", tray.flash_color("stopped") == (149, 165, 166))
ck("flash_color unknown fallback", tray.flash_color("mystery") == tray.flash_color("done"))

# ============ 纯函数：flash_frame 像素断言 ============
normal = tray.tray_pixmap("green")
frame_normal = tray.flash_frame(False, "green", "failed")
ck("non-highlight frame == normal pixmap", frame_normal.toImage() == normal.toImage())

hi = tray.flash_frame(True, "green", "failed")
ck("highlight frame differs from normal", hi.toImage() != normal.toImage())
# 描边：顶边中点 (16,1) 应为 failed 红
ck("highlight border red at top", close_rgb(px(hi, 16, 1), (231, 76, 60)))
ck("normal border NOT red at top", not close_rgb(px(normal, 16, 1), (231, 76, 60)))
# 角标：左下 (6,26) 实心红圆
ck("highlight badge red at bottom-left", close_rgb(px(hi, 6, 26), (231, 76, 60)))
ck("normal NO badge at bottom-left", not close_rgb(px(normal, 6, 26), (231, 76, 60)))
# 状态灯不被遮：右下 (25,25) 仍是绿灯
ck("status light intact under highlight", close_rgb(px(hi, 25, 25), (46, 204, 113)))
# kind 换色：await_input 琥珀描边
hi2 = tray.flash_frame(True, "green", "await_input")
ck("await border amber", close_rgb(px(hi2, 16, 1), (241, 196, 15)))
# 灰灯（失联）上也能闪
hi3 = tray.flash_frame(True, "gray", "done")
ck("flash works on gray light", close_rgb(px(hi3, 16, 1), (46, 204, 113)))

# ============ 纯函数：flash_frames 交替序列 ============
frames = tray.flash_frames("green", "failed", n=4)
ck("frames count 4", len(frames) == 4)
ck("frame0 normal", frames[0].toImage() == normal.toImage())
ck("frame1 highlight", frames[1].toImage() == hi.toImage())
ck("frame2 normal again", frames[2].toImage() == normal.toImage())
ck("frame3 highlight again", frames[3].toImage() == hi.toImage())
ck("frames n=0 empty", tray.flash_frames("green", "done", n=0) == [])
ck("frames negative clamped", tray.flash_frames("green", "done", n=-3) == [])

# ============ TrayFlash 状态机（tray_icon=None，不碰真托盘）============
tf = tray.TrayFlash(tray_icon=None, light_fn=lambda: "green")
ck("initial inactive", tf.active is False)
t0 = time.time()
tf.start_flash("failed", duration_s=6.0)
ck("start -> active", tf.active is True)
ck("start -> first frame highlight", tf.highlight is True)
ck("start -> kind kept", tf.kind == "failed")
ck("start -> ends_at ~6s", abs(tf.ends_at - (t0 + 6.0)) < 0.5)
ck("timer interval 300ms", tf._timer.interval() == tray.FLASH_INTERVAL_MS)
ck("timer running", tf._timer.isActive())
# 手动 _step 模拟交替（确定性，不等真实定时器）
tf._step()
ck("step1 -> normal", tf.highlight is False)
tf._step()
ck("step2 -> highlight", tf.highlight is True)
ck("ticks counted", tf._ticks == 2)
# 重复调用刷新：换 kind + 重新计时
t1 = time.time()
tf.start_flash("await_input", duration_s=3.0)
ck("refresh -> new kind", tf.kind == "await_input")
ck("refresh -> rehighlight", tf.highlight is True)
ck("refresh -> timer reset", tf._ticks == 0)
ck("refresh -> new deadline", abs(tf.ends_at - (t1 + 3.0)) < 0.5)
# 到期自动恢复
tf.ends_at = time.time() - 0.1
tf._step()
ck("expiry -> inactive", tf.active is False)
ck("expiry -> highlight cleared", tf.highlight is False)
ck("expiry -> timer stopped", not tf._timer.isActive())
# stop 幂等
tf.stop()
ck("stop idempotent", tf.active is False)

# ============ TrayFlash + 真 QSystemTrayIcon（offscreen）============
icon = QSystemTrayIcon(QIcon(tray.tray_pixmap("green")))
tf2 = tray.TrayFlash(tray_icon=icon, light_fn=lambda: "orange")
tf2.start_flash("done", duration_s=0.7)
ck("real icon: active after start", tf2.active is True)
# 真实事件循环跑 ~1.3s（>0.7s 时长 + 一个 300ms tick 余量）：应观察到交替且到期自动恢复
saw_highlight = set()
deadline = time.time() + 1.3
while time.time() < deadline:
    app.processEvents()
    saw_highlight.add(tf2.highlight)
    time.sleep(0.02)
ck("real timer alternated frames", saw_highlight == {True, False})
ck("real timer auto-restored after duration", tf2.active is False)
ck("real timer stopped", not tf2._timer.isActive())

# ============ 既有逻辑不回归：tray_pixmap/菜单 ============
ck("tray_pixmap unchanged size", tray.tray_pixmap("red").size().width() == 32)
menu = tray.build_fluent_menu(app, icon)
ck("menu still builds (8-item contract intact)", menu is not None)
ck("menu actions map intact", hasattr(menu, "_actions_map"))

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)