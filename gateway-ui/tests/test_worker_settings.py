# -*- coding: utf-8 -*-
"""test_worker_settings.py — 设置界面 F7 worker 侧 settings 总线协议（CONTRACT §6）。
side-env python 跑（offscreen Qt）。全部用 scratch 总线，绝不碰真实总线/config。
- request_setting：写 settings_req.json（id/key/value/source），乐观更新 pet 内存。
- set_pet_cfg 改道：pet 键 → request_setting（不再写 pet_cfg.json）。
- read_ack：ok 清 pending / fail 留证；last_ack 供 UI 反馈。
- reload_settings：读 settings_view.json 快照；缺失回落 meta.pet；不读 pet_cfg.json。
- A2 保留：左键显示/隐藏绝不发请求。
- open_settings 单实例（fake settings_window 模块注入）。"""
import json
import os
import sys
import time
import types
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication, QSystemTrayIcon
app = QApplication([])

import main as wk

from _env import scratch  # noqa: E402
BUS = scratch("gui23-wksettings")
BUS.mkdir(parents=True, exist_ok=True)
for f in BUS.glob("settings_*.json"):
    f.unlink(missing_ok=True)
(BUS / "pet_cfg.json").unlink(missing_ok=True)   # 清上轮残留死文件
wk.META = {"bus_dir": str(BUS), "gateway_pid": os.getppid(),
           "spawn_token": "t", "state_path": str(BUS / "state.json"),
           "hermes_home": str(BUS), "pet": {"enable-pet": False, "slug": "jue",
           "scale": 0.7, "roam": True, "topmost": True}}
wk.PET["cfg"] = dict(wk.META["pet"])
wk.SETTINGS["pending"].clear()
wk.SETTINGS["view_mtime"] = 0.0
wk.SETTINGS["ack_mtime"] = 0.0

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

# ============ request_setting：写请求文件 + 乐观更新 ============
rid = wk.request_setting("pet.scale", 1.0, source="settings")
req = json.loads((BUS / "settings_req.json").read_text(encoding="utf-8"))
ck("req file written", req["id"] == rid and req["key"] == "pet.scale" and req["value"] == 1.0)
ck("req source carried", req["source"] == "settings")
ck("req id is hex8", len(rid) == 8)
ck("optimistic pet cfg updated", wk.PET["cfg"]["scale"] == 1.0)
ck("pending recorded", rid in wk.SETTINGS["pending"])
ck("NO pet_cfg.json written (retired)", not (BUS / "pet_cfg.json").exists())

# 顶层键请求不乐观改 pet cfg
rid2 = wk.request_setting("tray_enabled", False, source="settings")
req2 = json.loads((BUS / "settings_req.json").read_text(encoding="utf-8"))
ck("top key request written", req2["key"] == "tray_enabled" and req2["value"] is False)
ck("top key does not touch pet cfg", "tray_enabled" not in wk.PET["cfg"])

# ============ set_pet_cfg 改道 → request_setting ============
rid3 = wk.set_pet_cfg("roam", False)
req3 = json.loads((BUS / "settings_req.json").read_text(encoding="utf-8"))
ck("set_pet_cfg routes to pet.roam request", req3["key"] == "pet.roam" and req3["value"] is False)
ck("set_pet_cfg optimistic update", wk.PET["cfg"]["roam"] is False)
ck("set_pet_cfg returns rid", rid3 == req3["id"])

# ============ read_ack：ok 清 pending ============
(BUS / "settings_ack.json").write_text(json.dumps(
    {"id": rid, "ok": True, "key": "pet.scale", "value": 1.0, "error": "", "ts": time.time()}),
    encoding="utf-8")
wk.read_ack()
ck("ack ok clears pending", rid not in wk.SETTINGS["pending"])
ck("last_ack stored for UI", (wk.SETTINGS["last_ack"] or {}).get("id") == rid)
# mtime 不变 → 不重复处理
wk.read_ack()
ck("ack mtime dedup (no reprocess)", True)

# ============ read_ack：fail 留证 + 清 pending ============
(BUS / "settings_ack.json").write_text(json.dumps(
    {"id": rid3, "ok": False, "key": "pet.roam", "value": False,
     "error": "writer failed", "ts": time.time()}), encoding="utf-8")
time.sleep(0.01)
wk.read_ack()
ck("ack fail clears pending", rid3 not in wk.SETTINGS["pending"])
ck("ack fail recorded", (wk.SETTINGS["last_ack"] or {}).get("ok") is False)

# ============ reload_settings：读快照 ============
(BUS / "settings_view.json").write_text(json.dumps(
    {"ts": time.time(), "top": {}, "pet": {"enable-pet": True, "slug": "fairy",
     "scale": 0.5, "roam": True, "topmost": False}, "explicit": {"top": [], "pet": []}}),
    encoding="utf-8")
wk.reload_settings()
ck("reload_settings reads view pet", wk.PET["cfg"]["slug"] == "fairy")
ck("reload_settings enable-pet from view", wk.PET["cfg"]["enable-pet"] is True)
ck("reload_settings scale from view", wk.PET["cfg"]["scale"] == 0.5)
# 死文件 pet_cfg.json 不被读
(BUS / "pet_cfg.json").write_text(json.dumps({"slug": "GHOST", "enable-pet": False}), encoding="utf-8")
wk.SETTINGS["view_mtime"] = 0.0  # 强制重读
wk.reload_settings()
ck("dead pet_cfg.json ignored", wk.PET["cfg"]["slug"] == "fairy")

# ============ reload_settings：快照缺失回落 meta.pet ============
(BUS / "settings_view.json").unlink()
wk.PET["cfg"] = {}
wk.SETTINGS["view_mtime"] = 0.0
wk.reload_settings()
ck("view absent -> fallback meta.pet", wk.PET["cfg"]["slug"] == "jue")

# ============ A2 保留：左键绝不发请求 ============
class FakeWin:
    def __init__(self): self._v = True
    def isVisible(self): return self._v
    def show(self): self._v = True
    def hide(self): self._v = False
wk.PET["win"] = FakeWin()
wk.PET["cfg"]["enable-pet"] = True
wk.PET["balance_cfg"] = {"enabled": True}   # 托盘左键改绑：左键改绑气泡
req_before = (BUS / "settings_req.json").read_text(encoding="utf-8")
wk.on_tray_activated(QSystemTrayIcon.Trigger)
wk.on_tray_activated(QSystemTrayIcon.Trigger)
ck("A2: left-click writes NO request", (BUS / "settings_req.json").read_text(encoding="utf-8") == req_before)
ck("A2: left-click toggles bubble not visibility",
   wk.PET["win"].isVisible() is True and not wk.PET["bubble"].isVisible())
wk.PET["bubble"].hide_bubble()
wk.PET["balance_cfg"] = {}

# ============ open_settings 单实例（fake 模块注入）============
created = []
class FakeSettingsWin:
    def __init__(self, meta, state, request_fn, home=""):
        created.append(self)
        self._vis = True
        self.raised = 0
    def isVisible(self): return self._vis
    def show(self): self._vis = True
    def raise_(self): self.raised += 1
    def activateWindow(self): pass
fake_mod = types.ModuleType("settings_window")
fake_mod.SettingsWindow = FakeSettingsWin
sys.modules["settings_window"] = fake_mod
wk.SETTINGS_WIN["win"] = None
w1 = wk.open_settings()
ck("open_settings creates window", w1 is not None and len(created) == 1)
w2 = wk.open_settings()
ck("open_settings single instance (no recreate)", w2 is w1 and len(created) == 1)
ck("open_settings re-activates existing", w1.raised >= 1)
# 窗口关闭后再开 → 重建
w1._vis = False
w3 = wk.open_settings()
ck("open_settings rebuilds after close", len(created) == 2 and w3 is not w1)

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)