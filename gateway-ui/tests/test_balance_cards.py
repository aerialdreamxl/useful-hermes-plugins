# -*- coding: utf-8 -*-
"""test_balance_cards.py — 余额气泡 SettingsWindow 余额页（CONTRACT §10/§6）。
side-env offscreen。只用 scratch/gui25-cards 总线，绝不碰真实总线/config。
- 初值=快照 balance 块；缺块/缺键 → 默认值角标（enabled 默认 false）。
- 三卡片改动 → request_fn("balance.*", value, source="settings")。
- refresh_s 滑杆钳 60~3600；providers_dir 弹文件夹（可注入），空=回落默认。
- 程序化刷新 blockSignals 不触发 request_fn（回环防护）。
- ack fail 回滚快照值；ack ok 清 pending。
- qconfig 影子闸：绝不写 cwd/config。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from PySide6.QtWidgets import QApplication
app = QApplication([])

import settings_window as sw
from settings_window import SettingsWindow

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui25-cards")
BUS.mkdir(parents=True, exist_ok=True)
for f in BUS.glob("settings_*.json"):
    f.unlink(missing_ok=True)
(BUS / "pet_error.log").unlink(missing_ok=True)
(BUS / "qfw_shadow_config.json").unlink(missing_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

CALLS = []
def fake_request(key, value, source="settings"):
    rid = f"r{len(CALLS):04d}"
    CALLS.append((key, value, source))
    return rid

META = {"bus_dir": str(BUS), "hermes_home": HOME, "log_dir": str(BUS),
        "version": "0.25.0", "repo": "x", "cwd": "x"}
STATE = {"data": None, "ts": 0.0}

def write_view(v):
    (BUS / "settings_view.json").write_text(
        json.dumps(v, ensure_ascii=False), encoding="utf-8")

def write_ack(rid, ok, key, value, error=""):
    (BUS / "settings_ack.json").write_text(json.dumps(
        {"id": rid, "ok": ok, "key": key, "value": value,
         "error": error, "ts": time.time()}, ensure_ascii=False), encoding="utf-8")

BASE_TOP = {"tray_enabled": True, "bus_enabled": True, "degrade_window_s": 300,
            "verbose_log": False, "package_index_url": "https://pypi.org/simple"}
BASE_PET = {"enable-pet": True, "slug": "fairy", "scale": 0.7,
            "roam": True, "topmost": True, "event_reactions": True}

def mkview(balance=None, expl_balance=None):
    v = {"ts": time.time(), "top": dict(BASE_TOP), "pet": dict(BASE_PET),
         "explicit": {"top": list(BASE_TOP), "pet": list(BASE_PET),
                      "balance": list(expl_balance or [])}}
    if balance is not None:
        v["balance"] = balance
    return v

# ---- 1. 缺 balance 块 → 默认值（enabled false / refresh 300 / dir 空回落）----
write_view(mkview())
w = SettingsWindow(META, STATE, fake_request, home=HOME)
ck("no-block-switch-off", w.balance_card.switchButton.isChecked() is False)
ck("no-block-refresh-300", w.balance_refresh_card.slider.value() == 300)
ck("no-block-enabled-mark", w._default_marks.get("balance.enabled") is True)
ck("no-block-refresh-mark", w._default_marks.get("balance.refresh_s") is True)
ck("no-block-dir-mark", w._default_marks.get("balance.providers_dir") is True)
ck("dir-fallback-home", w._effective_balance_dir() == str(Path(HOME) / "balance-providers"))

# ---- 2. 快照有 balance 块（显式）→ 卡片同步 + 无默认角标 ----
CALLS.clear()
write_view(mkview({"enabled": True, "refresh_s": 120, "providers_dir": r"D:\custom\bal"},
                  ["enabled", "refresh_s", "providers_dir"]))
w._poll_view()
ck("block-switch-on", w.balance_card.switchButton.isChecked() is True)
ck("block-refresh-120", w.balance_refresh_card.slider.value() == 120)
ck("block-enabled-explicit", w._default_marks.get("balance.enabled") is False)
ck("block-dir-custom", w._effective_balance_dir() == r"D:\custom\bal")
ck("block-dir-card-text", r"D:\custom\bal" in w.balance_dir_card.contentLabel.text())
ck("apply-no-request", len(CALLS) == 0)   # 程序化刷新绝不发请求

# ---- 3. 开关 toggle → balance.enabled 请求 ----
CALLS.clear()
w.balance_card.switchButton.setChecked(False)
ck("toggle-off-request", ("balance.enabled", False, "settings") in CALLS)
CALLS.clear()
w.balance_card.switchButton.setChecked(True)
ck("toggle-on-request", ("balance.enabled", True, "settings") in CALLS)

# ---- 4. refresh 滑杆 → balance.refresh_s 钳位请求 ----
CALLS.clear()
w.balance_refresh_card.slider.setValue(600)
ck("refresh-request", ("balance.refresh_s", 600, "settings") in CALLS)
ck("refresh-label", w.balance_refresh_card.valueLabel.text() == "600")

# ---- 5. providers_dir 弹文件夹（注入）→ 请求；取消不动 ----
CALLS.clear()
w.ask_providers_dir = lambda cur: r"D:\new\providers"
w._on_balance_dir()
ck("dir-request", ("balance.providers_dir", r"D:\new\providers", "settings") in CALLS)
CALLS.clear()
w.ask_providers_dir = lambda cur: None   # 取消
w._on_balance_dir()
ck("dir-cancel-noop", len(CALLS) == 0)
# 空串=恢复默认（显式写 ""）
CALLS.clear()
w.ask_providers_dir = lambda cur: "  "
w._on_balance_dir()
ck("dir-empty-restore", ("balance.providers_dir", "", "settings") in CALLS)

# ---- 6. ack ok 清 pending ----
CALLS.clear()
rid = w._request("balance.enabled", True)
ck("pending-recorded", rid in w._pending)
write_ack(rid, True, "balance.enabled", True)
w._poll_ack()
ck("ack-ok-clears", rid not in w._pending)
ck("ack-ok-info", w.last_info is not None and w.last_info[0] == "success")

# ---- 7. ack fail 回滚快照值 ----
write_view(mkview({"enabled": True, "refresh_s": 300, "providers_dir": ""},
                  ["enabled", "refresh_s"]))
w._poll_view()
CALLS.clear()
rid = w._request("balance.enabled", False)   # 乐观关
w.balance_card.switchButton.setChecked(False)
write_ack(rid, False, "balance.enabled", False, "writer failed")
w._poll_ack()
ck("ack-fail-rollback-on", w.balance_card.switchButton.isChecked() is True)  # 回滚到快照 true
ck("ack-fail-info", w.last_info is not None and w.last_info[0] == "error")
ck("ack-fail-clears", rid not in w._pending)

# ---- 8. refresh fail 回滚 ----
write_view(mkview({"enabled": True, "refresh_s": 300, "providers_dir": ""},
                  ["enabled", "refresh_s"]))
w._poll_view()
rid = w._request("balance.refresh_s", 900)
w.balance_refresh_card.slider.setValue(900)
write_ack(rid, False, "balance.refresh_s", 900, "bad")
w._poll_ack()
ck("refresh-fail-rollback", w.balance_refresh_card.slider.value() == 300)

# ---- 9. 乐观值：pending 期间 _effective 取 pending ----
CALLS.clear()
rid = w._request("balance.refresh_s", 777)
ck("effective-pending", w._effective("balance", "refresh_s") == 777)
write_ack(rid, True, "balance.refresh_s", 777)
w._poll_ack()

# ---- 10. qconfig 影子闸：cwd 无 config 写 ----
ck("qconfig-shadow", (BUS / "qfw_shadow_config.json").exists()
   or not Path("config.json").exists())
ck("no-cwd-config-dir", not Path("config").exists())

# ---- 11. 余额气泡修订：Token Plan 版本选择卡 ----
write_view(mkview())
w._poll_view()
ck("edition-default-individual",
   w.balance_edition_card.comboBox.currentData() == "individual")
ck("edition-default-mark", w._default_marks.get("balance.token_plan_edition") is True)
CALLS.clear()
w.balance_edition_card.comboBox.setCurrentIndex(1)   # 团队版
ck("edition-change-request", ("balance.token_plan_edition", "team", "settings") in CALLS)
rid_ed = CALLS and w._pending and list(w._pending)[-1]
write_view(mkview({"enabled": False, "refresh_s": 300, "providers_dir": "",
                   "token_plan_edition": "team"},
                  ["enabled", "refresh_s", "providers_dir", "token_plan_edition"]))
w._poll_view()
ck("edition-snapshot-team", w.balance_edition_card.comboBox.currentData() == "team")
ck("edition-explicit-no-mark", w._default_marks.get("balance.token_plan_edition") is False)
# ack fail 回滚到快照 team
CALLS.clear()
rid = w._request("balance.token_plan_edition", "individual")
w.balance_edition_card.comboBox.setCurrentIndex(0)
write_ack(rid, False, "balance.token_plan_edition", "individual", "writer failed")
w._poll_ack()
ck("edition-fail-rollback-team", w.balance_edition_card.comboBox.currentData() == "team")
# 程序化刷新不触发 request（回环防护）
CALLS.clear()
write_view(mkview({"enabled": False, "refresh_s": 300, "providers_dir": "",
                   "token_plan_edition": "individual"}, ["token_plan_edition"]))
w._poll_view()
ck("edition-programmatic-no-request", len(CALLS) == 0)

w.close()
n_ok = sum(1 for _, ok in R if ok)
for name, ok in R:
    print(("ok " if ok else "FAIL ") + name)
print(f"\nSUMMARY: {n_ok}/{len(R)} PASS")
print("PASS" if n_ok == len(R) else "FAIL")
sys.exit(0 if n_ok == len(R) else 1)
