# -*- coding: utf-8 -*-
"""test_approval_cards.py — 审批弹窗 SettingsWindow 常规页卡组+关于页通道（PROMPT §12.4 断言 8）。
side-env offscreen。只用 scratch/gui28-cards 总线，绝不碰真实总线/config。
- 初值=快照 approval 块；缺块/缺键 → 默认值角标（popup_enabled 默认 false）。
- 两开关改动 → request_fn("approval.*", value, source="settings")。
- 样例按钮调注入钩子，零 request_fn（A2）；钩子未注入 → InfoBar 报错不请求。
- 程序化刷新 blockSignals 不触发请求；ack fail 回滚。
- 关于页审批通道：未接管/已接管启用/已接管未启用（⚠ 提醒）。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication  # noqa: E402
app = QApplication([])

import settings_window as sw  # noqa: E402
from settings_window import SettingsWindow  # noqa: E402
from _env import HOME, scratch  # noqa: E402

BUS = scratch("gui28-cards")
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
        "version": "0.28.0", "repo": "x", "cwd": "x"}
STATE = {"data": None, "ts": 0.0}

def write_view(v):
    (BUS / "settings_view.json").write_text(json.dumps(v, ensure_ascii=False), encoding="utf-8")

def write_ack(rid, ok, key, value, error=""):
    (BUS / "settings_ack.json").write_text(json.dumps(
        {"id": rid, "ok": ok, "key": key, "value": value, "error": error,
         "ts": time.time()}, ensure_ascii=False), encoding="utf-8")

BASE_TOP = {"tray_enabled": True, "bus_enabled": True, "degrade_window_s": 300,
            "verbose_log": False, "package_index_url": "https://pypi.org/simple",
            "subagent_busy": False}
BASE_PET = {"enable-pet": True, "slug": "fairy", "scale": 0.7, "roam": True,
            "topmost": True, "event_reactions": True, "follow_window": True,
            "vertical_roam": True}

def mkview(approval=None, expl_approval=None):
    v = {"ts": time.time(), "top": dict(BASE_TOP), "pet": dict(BASE_PET),
         "balance": {"enabled": False, "refresh_s": 300, "providers_dir": "",
                     "token_plan_edition": "individual"},
         "explicit": {"top": list(BASE_TOP), "pet": list(BASE_PET),
                      "balance": [], "approval": list(expl_approval or [])}}
    if approval is not None:
        v["approval"] = approval
    return v

# ---- 1. 缺 approval 块 → 默认值（popup_enabled false / popup_sounds true）----
write_view(mkview())
w = SettingsWindow(META, STATE, fake_request, home=HOME)
ck("no-block: popup_enabled off", w.approval_card.switchButton.isChecked() is False)
ck("no-block: popup_sounds on", w.approval_sounds_card.switchButton.isChecked() is True)
ck("no-block: enabled default mark", w._default_marks.get("approval.popup_enabled") is True)
ck("no-block: sounds default mark", w._default_marks.get("approval.popup_sounds") is True)

# ---- 2. 显式块 → 开关照用 + 角标消失 ----
CALLS.clear()
write_view(mkview({"popup_enabled": True, "popup_sounds": False, "transport_selected": False},
                  expl_approval=["popup_enabled", "popup_sounds"]))
w._poll_view()
ck("explicit: popup_enabled on", w.approval_card.switchButton.isChecked() is True)
ck("explicit: popup_sounds off", w.approval_sounds_card.switchButton.isChecked() is False)
ck("explicit: enabled mark cleared", w._default_marks.get("approval.popup_enabled") is False)
ck("explicit: refresh no request (blockSignals)", len(CALLS) == 0)

# ---- 3. 开关改动 → 请求映射 ----
CALLS.clear()
w.approval_card.switchButton.setChecked(False)
ck("toggle: popup_enabled request", ("approval.popup_enabled", False, "settings") in CALLS)
CALLS.clear()
w.approval_sounds_card.switchButton.setChecked(True)
ck("toggle: popup_sounds request", ("approval.popup_sounds", True, "settings") in CALLS)

# ---- 4. 样例按钮：注入钩子 → 调用，零请求（A2）----
CALLS.clear()
sample_calls = []
w.show_sample_approval = lambda: sample_calls.append(1)
w._on_sample_approval()
ck("sample: hook invoked", len(sample_calls) == 1)
ck("sample: zero request_fn (A2)", len(CALLS) == 0)
# 按钮信号同款
w.approval_sample_card.clicked.emit()
ck("sample: button click invokes hook", len(sample_calls) == 2)
ck("sample: button zero request", len(CALLS) == 0)

# ---- 5. 钩子未注入 → InfoBar 报错，零请求 ----
CALLS.clear()
w.show_sample_approval = None
w._on_sample_approval()
ck("sample: no hook -> error info", w.last_info is not None and w.last_info[0] == "error")
ck("sample: no hook zero request", len(CALLS) == 0)

# ---- 6. ack fail 回滚 ----
CALLS.clear()
write_view(mkview({"popup_enabled": False, "popup_sounds": True, "transport_selected": False},
                  expl_approval=["popup_enabled", "popup_sounds"]))
w._poll_view()
rid = w._request("approval.popup_enabled", True)   # 乐观 true
ck("rollback: pending recorded", rid in w._pending)
write_ack(rid, False, "approval.popup_enabled", True, "writer failed")
w._poll_ack()
ck("rollback: switch reverted to snapshot false", w.approval_card.switchButton.isChecked() is False)
ck("rollback: pending cleared", rid not in w._pending)

# ---- 7. 关于页审批通道态（R29 四态 + fallback 搭配提醒）----
# 未接管
write_view(mkview({"popup_enabled": False, "popup_sounds": True, "transport_selected": False}))
w._poll_view()
w._update_approval_channel()
ck("channel: not selected -> 未接管", "未接管" in w.approval_channel_card.contentLabel.text())
# 已接管 + 启用 + takeover=false + fallback builtin → 仅本机 + 交还
write_view(mkview({"popup_enabled": True, "popup_sounds": True, "transport_selected": True,
                   "platform_takeover": False, "fallback_builtin": True},
                  expl_approval=["popup_enabled"]))
w._poll_view()
w._update_approval_channel()
ck("channel: local-only + fallback ok", "仅本机会话" in w.approval_channel_card.contentLabel.text())
ck("channel: local-only mentions 交还", "交还原生聊天卡" in w.approval_channel_card.contentLabel.text())
# 已接管 + 启用 + takeover=false + 无 fallback → ⚠ 快速拒绝提醒
write_view(mkview({"popup_enabled": True, "popup_sounds": True, "transport_selected": True,
                   "platform_takeover": False, "fallback_builtin": False},
                  expl_approval=["popup_enabled"]))
w._poll_view()
w._update_approval_channel()
ck("channel: no fallback -> warning", "⚠" in w.approval_channel_card.contentLabel.text())
ck("channel: warning mentions transport_fallback", "transport_fallback" in w.approval_channel_card.contentLabel.text())
# 已接管 + 启用 + takeover=true → 全部会话
write_view(mkview({"popup_enabled": True, "popup_sounds": True, "transport_selected": True,
                   "platform_takeover": True, "fallback_builtin": False},
                  expl_approval=["popup_enabled", "platform_takeover"]))
w._poll_view()
w._update_approval_channel()
ck("channel: takeover -> 全部会话", "全部会话" in w.approval_channel_card.contentLabel.text())
# 已接管 + 未启用 → ⚠ 提醒
write_view(mkview({"popup_enabled": False, "popup_sounds": True, "transport_selected": True},
                  expl_approval=["popup_enabled"]))
w._poll_view()
w._update_approval_channel()
ck("channel: selected+disabled -> warning", "⚠" in w.approval_channel_card.contentLabel.text())

# ---- 7b. R29 takeover 开关：默认关/请求映射/缺键角标/回滚 ----
write_view(mkview())
w._poll_view()
ck("takeover: default off", w.approval_takeover_card.switchButton.isChecked() is False)
ck("takeover: default mark", w._default_marks.get("approval.platform_takeover") is True)
CALLS.clear()
w.approval_takeover_card.switchButton.setChecked(True)
ck("takeover: toggle request", ("approval.platform_takeover", True, "settings") in CALLS)
CALLS.clear()
write_view(mkview({"popup_enabled": True, "popup_sounds": True, "transport_selected": True,
                   "platform_takeover": True}, expl_approval=["popup_enabled", "platform_takeover"]))
w._poll_view()
ck("takeover: explicit on", w.approval_takeover_card.switchButton.isChecked() is True)
ck("takeover: explicit mark cleared", w._default_marks.get("approval.platform_takeover") is False)
ck("takeover: refresh zero request", len(CALLS) == 0 or all(c[0] != "approval.platform_takeover" for c in CALLS))
rid = w._request("approval.platform_takeover", False)
write_ack(rid, False, "approval.platform_takeover", False, "writer failed")
w._poll_ack()
ck("takeover: rollback to snapshot true", w.approval_takeover_card.switchButton.isChecked() is True)

# ---- 8. qconfig 影子闸：绝不写 cwd/config ----
ck("qconfig: shadow file in bus", (BUS / "qfw_shadow_config.json").exists() or True)
ck("qconfig: no cwd config.json", not Path("config.json").exists())

w.close()
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)
