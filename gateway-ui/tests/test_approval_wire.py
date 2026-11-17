# -*- coding: utf-8 -*-
"""test_approval_wire.py — 审批弹窗 main 收口接缝（PROMPT §12.4 断言 6/7，side-env offscreen）。

reload_settings 收 approval 块；_approval_enabled 门控；_ensure_approval_mgr 单例；
_sync_approval req 驱动；_show_sample_approval 样例；真窗点击写 approval_resp 而非
settings_req（A2）。全部 scratch 总线，绝不碰真实总线/config。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))
sys.path.insert(0, str(HERE))
from _env import scratch  # noqa: E402
BUS = scratch("gui28-approval-wire")
BUS.mkdir(parents=True, exist_ok=True)

import main as m  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QRect  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))


def reset(approval_cfg=None):
    m.META = {"bus_dir": str(BUS), "hermes_home": str(BUS)}
    m.PET["approval_mgr"] = None
    m.PET["approval_cfg"] = {} if approval_cfg is None else dict(approval_cfg)
    m.SETTINGS["view_mtime"] = 0.0
    m.SETTINGS["view_sig"] = None
    for f in BUS.glob("approval_*"):
        f.unlink(missing_ok=True)
    for f in BUS.glob("settings_req.json*"):
        f.unlink(missing_ok=True)


def write_view(approval):
    (BUS / "settings_view.json").write_text(
        json.dumps({"top": {}, "pet": {}, "balance": {}, "approval": approval,
                    "explicit": {"approval": list(approval.keys())}}), encoding="utf-8")


def write_req(rid="r1"):
    (BUS / "approval_req.json").write_text(json.dumps(
        {"seq": 1, "ts": time.time(), "requests": [
            {"request_id": rid, "digest": "d-" + rid, "command": "rm -rf /x",
             "description": "样例", "surface": "transport:gateway-ui",
             "session_key": "agent:ns:desktop:dm:1", "pattern_key": "rm",
             "allowed_choices": ["once", "session", "always", "deny"],
             "timeout_seconds": 300, "expires_ts": time.time() + 300}]}),
        encoding="utf-8")


# ==================================== reload_settings 收 approval 块 ====
reset()
write_view({"popup_enabled": True, "popup_sounds": False, "transport_selected": True})
m.reload_settings()
ck("reload: approval_cfg populated", m.PET["approval_cfg"].get("popup_enabled") is True)
ck("reload: popup_sounds carried", m.PET["approval_cfg"].get("popup_sounds") is False)
ck("reload: transport_selected carried", m.PET["approval_cfg"].get("transport_selected") is True)
# 缺块 → 空（门控关）
reset()
(BUS / "settings_view.json").write_text(json.dumps({"top": {}, "pet": {}}), encoding="utf-8")
m.reload_settings()
ck("reload: absent approval block -> empty", m.PET["approval_cfg"] == {})

# ==================================== 门控 ====
reset({"popup_enabled": False})
ck("gate: disabled false", m._approval_enabled() is False)
reset({"popup_enabled": True})
ck("gate: enabled true", m._approval_enabled() is True)
reset()
ck("gate: missing key default false", m._approval_enabled() is False)

# ==================================== 管理器单例 + sync ====
reset({"popup_enabled": True})
mgr1 = m._ensure_approval_mgr()
mgr2 = m._ensure_approval_mgr()
ck("mgr: singleton", mgr1 is mgr2)
ck("mgr: stored in PET", m.PET["approval_mgr"] is mgr1)
write_req("r1")
m._sync_approval()
ck("sync: req creates popup via main", "r1" in mgr1.popups)
# 门控关 sync 收窗
m.PET["approval_cfg"] = {"popup_enabled": False}
m._sync_approval()
ck("sync: gating off clears", len(mgr1.popups) == 0)

# ==================================== 点击零 settings_req（A2）====
reset({"popup_enabled": True})
write_req("r1")
m._sync_approval()
mgr = m.PET["approval_mgr"]
win = mgr.popups["r1"]
win.buttons["once"].click()
app.processEvents()
ck("A2: click writes approval_resp", (BUS / "approval_resp.json").exists())
ck("A2: click ZERO settings_req", not (BUS / "settings_req.json").exists())
resp = json.loads((BUS / "approval_resp.json").read_text(encoding="utf-8"))
ck("A2: resp choice once", resp["entries"][0]["choice"] == "once")

# ==================================== 样例钩子 ====
reset({"popup_enabled": False})   # 门控关也能弹样例
s = m._show_sample_approval()
ck("sample: hook returns window", s is not None)
ck("sample: sample mode", s.sample is True)
s.buttons["deny"].click()
app.processEvents()
ck("sample: click no approval_resp", not (BUS / "approval_resp.json").exists())
ck("sample: click no settings_req", not (BUS / "settings_req.json").exists())

# ==================================== tick 不炸（审批同步纳入）====
reset({"popup_enabled": True})
write_view({"popup_enabled": True, "popup_sounds": True})   # tick 内 reload 会读快照
write_req("r1")
# 构造最小 tray/menu 替身跑 tick（审批同步段）
class _FakeTrayIcon:
    def setIcon(self, *a): pass
    def setToolTip(self, *a): pass
class _FakeMenu:
    _actions_map = {}
m.STATE["data"] = {"pet_state": "await", "active_sessions": 0, "verbose": False,
                   "tray": {"light": "yellow", "tier": "等待人类批准 1 项"},
                   "pending_approvals": 1, "writer_pid": os.getpid(), "boot_ts": time.time()}
m.STATE["ts"] = time.time()
import tray as tray_mod
tray_mod.STATE = m.STATE
tray_mod.META = m.META
m.tray = tray_mod   # 生产由 _main_inner 赋值；测试装置补齐（tick 用 main.tray）
try:
    m.tick(app, _FakeTrayIcon(), _FakeMenu())
    tick_ok = True
except Exception:
    tick_ok = False
ck("tick: runs with approval sync (no raise)", tick_ok)
_mgr = m.PET["approval_mgr"]
ck("tick: approval popup created during tick", _mgr is not None and "r1" in _mgr.popups)

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)
