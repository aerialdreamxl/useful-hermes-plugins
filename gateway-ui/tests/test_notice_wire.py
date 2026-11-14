# -*- coding: utf-8 -*-
"""test_notice_wire — 事件反应收口接缝测试（main.py 三事件反应接线）。

各并行任务各测各的模块（notices/tray_flash/pet_reaction），本套测收口方的接线：
_notice_reactions 路由 / event_reactions 门控 / stopped 只闪不动 /
tick setIcon guard 不覆盖闪动 / 首轮不回放。
side-env offscreen 跑。临时总线只进 scratch/gui24-wire-*。
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402

import main as M  # noqa: E402
import tray as T  # noqa: E402

PASS_N = 0


def ok(cond, label):
    global PASS_N
    if not cond:
        print(f"FAIL {label}")
        sys.exit(1)
    PASS_N += 1
    print(f" ok {label}")


app = QApplication.instance() or QApplication(sys.argv)

# scratch 总线（绝不碰真实 cache/gateway-ui）
import tempfile  # noqa: E402
BUS = Path(tempfile.mkdtemp(prefix="gui24-wire-"))
M.META = {"bus_dir": str(BUS), "hermes_home": str(BUS)}
T.META = M.META
T.STATE = M.STATE

icon = QSystemTrayIcon(QIcon(T.tray_pixmap("green")))
flash = T.TrayFlash(icon, light_fn=lambda: "green")
M.TRAY_FLASH = flash

calls = {"flash": [], "reaction": []}


class FakeWin:
    def play_reaction(self, kind, duration_s=4.0):
        calls["reaction"].append(kind)
        return True


M.PET["win"] = FakeWin()
M.PET["cfg"] = {"enable-pet": True}
M.LAST["verbose"] = False


def feed_notice(seq, kind, ts=None, platform="desktop"):
    M.STATE["data"] = {"notice": {"seq": seq, "kind": kind,
                                  "ts": ts if ts is not None else time.time(),
                                  "platform": platform}}


# --- 1. 首轮基线不回放 ---
feed_notice(5, "done")
M._notice_reactions()
ok(calls["flash"] == [] and calls["reaction"] == [], "首轮陈旧 notice 不回放")

# --- 2. 增量 done → 闪动+桌宠动作 ---
orig_start = flash.start_flash
def spy_start(kind, duration_s=T.FLASH_DURATION_S):
    calls["flash"].append(kind)
    orig_start(kind, duration_s)
flash.start_flash = spy_start

feed_notice(6, "done")
M._notice_reactions()
ok(calls["flash"] == ["done"], "done → start_flash(done)")
ok(calls["reaction"] == ["done"], "done → play_reaction(done)")
ok(flash.active is True, "闪动已激活")

# --- 3. 同 seq 去重（state 每拍重写 notice 常驻）---
M._notice_reactions()
M._notice_reactions()
ok(calls["flash"] == ["done"] and calls["reaction"] == ["done"], "同 seq 不重复反应")

# --- 4. failed / await_input 依次 ---
feed_notice(7, "failed")
M._notice_reactions()
feed_notice(8, "await_input")
M._notice_reactions()
ok(calls["flash"] == ["done", "failed", "await_input"], "三事件闪动序列正确")
ok(calls["reaction"] == ["done", "failed", "await_input"], "三事件桌宠动作序列正确")

# --- 5. stopped 只闪托盘不做桌宠动作 ---
feed_notice(9, "stopped")
M._notice_reactions()
ok(calls["flash"][-1] == "stopped", "stopped → 托盘闪动")
ok(calls["reaction"][-1] == "await_input", "stopped 不触发桌宠动作")

# --- 6. event_reactions=False 门控 ---
M.PET["cfg"]["event_reactions"] = False
feed_notice(10, "done")
M._notice_reactions()
ok(calls["flash"][-1] == "stopped", "event_reactions=False 不闪")
ok(len(calls["reaction"]) == 3, "event_reactions=False 不动桌宠")
M.PET["cfg"]["event_reactions"] = True

# --- 7. 缺键默认 true（A1：缺键≠false）---
del M.PET["cfg"]["event_reactions"]
feed_notice(11, "done")
M._notice_reactions()
ok(calls["flash"][-1] == "done", "缺键默认 true 照常反应")

# --- 8. tick setIcon guard：闪动期间不覆盖 ---
flash.stop()
feed_notice(12, "failed")
M._notice_reactions()          # 激活闪动
ok(flash.active is True, "guard 前置：闪动激活")
before = icon.icon().cacheKey()
M.LAST["light"] = None         # 强制 light 变化路径
M.tick(app, icon, None)        # tick 一整拍
after = icon.icon().cacheKey()
ok(flash.active is True, "tick 后闪动仍激活（未被 stop）")
ok(before == after, "tick 未覆盖闪动图标（guard 生效）")

# --- 9. 闪动结束后 tick 恢复正常图标 ---
flash.stop()
ok(flash.active is False, "stop 后 active=False")
M.LAST["light"] = None
M.tick(app, icon, None)
ok(icon.icon().cacheKey() != 0, "flash 结束后 tick 正常设图标")

# --- 10. 无 TRAY_FLASH（未初始化）不炸 ---
saved = M.TRAY_FLASH
M.TRAY_FLASH = None
feed_notice(13, "done")
M._notice_reactions()
ok(True, "TRAY_FLASH=None 安全 no-op")
M.TRAY_FLASH = saved

# --- 11. 无桌宠窗口只闪不动 ---
M.PET["win"] = None
feed_notice(14, "done")
n_before = len(calls["reaction"])
M._notice_reactions()
ok(calls["flash"][-1] == "done" and len(calls["reaction"]) == n_before,
   "无窗口：只闪托盘")

# --- 12. play_reaction 抛异常不拖垮 ---
class BoomWin:
    def play_reaction(self, kind, duration_s=4.0):
        raise RuntimeError("boom")
M.PET["win"] = BoomWin()
feed_notice(15, "failed")
M._notice_reactions()
ok(calls["flash"][-1] == "failed", "play_reaction 炸了闪动仍完成")

print(f"SUMMARY {PASS_N}/{PASS_N} PASS")
print("PASS")