# -*- coding: utf-8 -*-
"""test_approval_popup.py — 审批弹窗窗体+管理器（PROMPT §12.4 断言 6/7，side-env offscreen）。

纯函数几何/倒计时/盾牌；四按钮固定序+allowed_choices 置灰；样例窗零总线写（A2）；
Esc 无效；管理器 req 驱动/门控/堆叠折叠/withdrawn 收窗/resp 修剪/answered 防重建。
全部 scratch 总线，绝不碰真实总线/config。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))
sys.path.insert(0, str(HERE.parent))   # approval_ipc（宿主侧包根）
sys.path.insert(0, str(HERE))
from _env import scratch  # noqa: E402
BUS = scratch("gui28-approval-popup")
BUS.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QRect, Qt, QPoint  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
import approval_popup as ap  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
SCREEN = QRect(0, 0, 1536, 864)
def screen_rect():
    return SCREEN

R = []
def ck(name, ok):
    R.append((name, bool(ok)))


def clean():
    for f in BUS.glob("approval_*"):
        f.unlink(missing_ok=True)
    SENT.clear()


# IPC 替身：真窗点击不得写共享目录，只能发本地 IPC
SENT = []
def _fake_send(endpoint, payload, timeout=1.5):
    SENT.append({"endpoint": endpoint, "payload": payload})
    return {"ok": True}


ap.ApprovalResponseClient = type("R", (), {"send": staticmethod(_fake_send)})


def make_req(rid="r1", choices=("once", "session", "always", "deny"), expires=None):
    return {"request_id": rid, "digest": "d-" + rid, "command": "rm -rf /x",
            "description": "样例", "surface": "transport:gateway-ui",
            "session_key": "agent:ns:desktop:dm:1", "pattern_key": "rm",
            "allowed_choices": list(choices), "timeout_seconds": 300,
            "expires_ts": expires if expires is not None else time.time() + 300}


def write_req(reqs):
    (BUS / "approval_req.json").write_text(
        json.dumps({"seq": 1, "ts": time.time(), "requests": reqs}), encoding="utf-8")


# ==================================================== 纯函数 ====
pos = ap.popup_target_pos(SCREEN, ap.POPUP_W, ap.POPUP_H, 0)
ck("geom: slot0 right edge", pos.x() == SCREEN.right() - ap.POPUP_W - ap.MARGIN + 1)
ck("geom: slot0 vertical center", pos.y() == SCREEN.top() + SCREEN.height() // 2 - ap.POPUP_H // 2)
pos1 = ap.popup_target_pos(SCREEN, ap.POPUP_W, ap.POPUP_H, 1)
ck("geom: slot1 stacks upward", pos1.y() < pos.y())
ck("geom: slot1 same x", pos1.x() == pos.x())
pos_big = ap.popup_target_pos(SCREEN, ap.POPUP_W, ap.POPUP_H, 50)
ck("geom: huge slot clamped to screen top", pos_big.y() >= SCREEN.top())
ck("geom: target fully in screen", pos.x() + ap.POPUP_W <= SCREEN.right() + 1)

ck("countdown: positive", ap.fmt_remaining(45) == "剩余 45s")
ck("countdown: zero -> timeout text", "超时" in ap.fmt_remaining(0))
ck("countdown: negative -> timeout text", "超时" in ap.fmt_remaining(-5))
ck("countdown: bad input safe", "超时" in ap.fmt_remaining("x"))

pm = ap.shield_pixmap(24)
ck("shield: non-null pixmap", not pm.isNull())
ck("shield: size 24", pm.width() == 24 and pm.height() == 24)

ck("buttons: fixed order", [c for _, c in ap.BUTTONS] == ["always", "session", "once", "deny"])
ck("buttons: label map", ap.CHOICE_BY_LABEL["永远放行"] == "always")

# ==================================================== 窗体 ====
clean()
win = ap.ApprovalPopupWindow(make_req(), get_screen_rect=screen_rect, animate=False)
ck("window: four buttons", len(win.buttons) == 4)
ck("window: button order in layout",
   [win.buttons[c].text() for c in ("always", "session", "once", "deny")]
   == ["永远放行", "会话放行", "单次放行", "拒绝"])
ck("window: all enabled for full choices", all(b.isEnabled() for b in win.buttons.values()))
ck("window: deny always enabled", win.buttons["deny"].isEnabled())
win.show_slide()
app.processEvents()
ck("window: target pos applied", win.pos() == win.target_pos())
ck("window: countdown shows remaining", "剩余" in win.lbl_countdown.text())
# Esc 无效（仍可见）
win.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
app.processEvents()
ck("window: Esc does not close", not win._closing)
win.close()

# allowed_choices 收缩（smart-DENY → once/deny）置灰
win2 = ap.ApprovalPopupWindow(make_req(choices=("once", "deny")),
                              get_screen_rect=screen_rect, animate=False)
ck("window: session disabled when not allowed", not win2.buttons["session"].isEnabled())
ck("window: always disabled when not allowed", not win2.buttons["always"].isEnabled())
ck("window: once enabled", win2.buttons["once"].isEnabled())
ck("window: deny enabled (收缩恒在)", win2.buttons["deny"].isEnabled())
win2.close()

# 样例窗：点击只收窗不写总线（A2）
clean()
calls = []
win3 = ap.ApprovalPopupWindow(make_req(rid="sample"), on_choice=lambda *a: calls.append(a),
                              sample=True, get_screen_rect=screen_rect, animate=False)
win3.show_slide()
win3.buttons["once"].click()
app.processEvents()
ck("sample: on_choice invoked", len(calls) == 1)
ck("sample: last_choice recorded", win3.last_choice == "once")
ck("sample: closing triggered", win3._closing)
ck("sample: zero bus write", not (BUS / "approval_resp.json").exists())
win3.close()

# ==================================================== 管理器 ====
clean()
mgr = ap.ApprovalPopupManager({}, BUS, get_cfg=lambda: {"popup_enabled": True},
                              get_screen_rect=screen_rect, animate=False)
write_req([make_req("r1")])
mgr.sync()
ck("mgr: req creates popup", "r1" in mgr.popups)
ck("mgr: one popup", len(mgr.popups) == 1)

# 门控关 → 收窗
mgr._get_cfg = lambda: {"popup_enabled": False}
mgr.sync()
ck("mgr: gating off clears popups", len(mgr.popups) == 0)
mgr._get_cfg = lambda: {"popup_enabled": True}

# 多条堆叠 + 折叠角标（>4）
clean()
mgr = ap.ApprovalPopupManager({}, BUS, get_cfg=lambda: {"popup_enabled": True},
                              get_screen_rect=screen_rect, animate=False)
write_req([make_req(f"r{i}") for i in range(6)])
mgr.sync()
ck("mgr: 6 popups tracked", len(mgr.popups) == 6)
visible = [w for w in mgr.popups.values() if w.isVisible()]
ck("mgr: max 4 visible", len(visible) <= ap.MAX_VISIBLE)
# 最新者（reversed → r5）占主位 slot0 且带 +2 角标
newest = mgr.popups.get("r5")
ck("mgr: newest r5 present", newest is not None)
ck("mgr: newest badge +2", newest.lbl_badge.text() == "+2")
ck("mgr: newest slot0", newest._slot == 0)

# 点击注入 → resp 写入 + 收窗 + answered
clean()
mgr = ap.ApprovalPopupManager({}, BUS, get_cfg=lambda: {"popup_enabled": True},
                              get_screen_rect=screen_rect, animate=False)
write_req([make_req("r1")])
mgr.sync()
mgr._on_choice("r1", "d-r1", "once")
app.processEvents()
ck("mgr: choice removes popup", "r1" not in mgr.popups)
ck("mgr: answered marked", "r1" in mgr._answered)
ck("mgr: resp never touches shared dir", not (BUS / "approval_resp.json").exists())
ck("mgr: resp sent over IPC once", len(SENT) == 1)
p = SENT[0]["payload"]
ck("mgr: ipc choice once", p["choice"] == "once")
ck("mgr: ipc digest echoed", p["digest"] == "d-r1")
ck("mgr: ipc request_id", p["request_id"] == "r1")
ck("mgr: ipc carries handshake fields", "token" in p and "spawn_token" in p)
# host 未消费前 sync 不重建（answered 防重建）
mgr.sync()
ck("mgr: answered prevents rebuild", "r1" not in mgr.popups)
# host 消费（req 清空）→ answered 清除
write_req([])
mgr.sync()
ck("mgr: answered cleared after host consume", "r1" not in mgr._answered)

# resp 修剪：host 已消费的旧条目下次写入时剔除
clean()
mgr = ap.ApprovalPopupManager({}, BUS, get_cfg=lambda: {"popup_enabled": True},
                              get_screen_rect=screen_rect, animate=False)
write_req([make_req("r1"), make_req("r2")])
mgr.sync()
mgr._on_choice("r1", "d-r1", "once")     # resp 含 r1
write_req([make_req("r2")])               # host 消费 r1（离开 req）
mgr.sync()
mgr._on_choice("r2", "d-r2", "deny")     # 写 r2 时修剪 r1
ids = [s["payload"]["request_id"] for s in SENT]
ck("mgr: each click sends exactly its own request", ids == ["r1", "r2"])
ck("mgr: shared resp file still absent", not (BUS / "approval_resp.json").exists())

# withdrawn 收窗
clean()
mgr = ap.ApprovalPopupManager({}, BUS, get_cfg=lambda: {"popup_enabled": True},
                              get_screen_rect=screen_rect, animate=False)
write_req([make_req("r1")])
mgr.sync()
ck("mgr: popup before withdrawn", "r1" in mgr.popups)
with (BUS / "approval_withdrawn.jsonl").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"request_id": "r1", "reason": "completed", "ts": time.time()}) + "\n")
mgr.sync()
ck("mgr: withdrawn closes popup", "r1" not in mgr.popups)

# 样例窗独立（不受门控/req 同步影响）
clean()
mgr = ap.ApprovalPopupManager({}, BUS, get_cfg=lambda: {"popup_enabled": False},
                              get_screen_rect=screen_rect, animate=False)
s = mgr.show_sample()
ck("mgr: sample created despite gating off", s is not None)
ck("mgr: sample is sample mode", s.sample is True)
mgr.sync()   # 门控关 sync 不应收样例窗
ck("mgr: sample survives gating sync", mgr._sample is not None)
s.buttons["deny"].click()
app.processEvents()
ck("mgr: sample click no bus write", not (BUS / "approval_resp.json").exists())
ck("mgr: sample cleared after click", mgr._sample is None)

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)
