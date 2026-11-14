# -*- coding: utf-8 -*-
"""test_settings_window.py — 设置界面 F7 SettingsWindow（CONTRACT §6/§7）。
side-env python 跑（offscreen Qt）。全部用 scratch 总线，绝不碰真实总线/config。
- 初值 = settings_view 快照；explicit 缺键 → 默认值/未设置角标。
- 卡片改动 → request_fn(key,value,source="settings")；tray 关闭二次确认。
- scale 浮点 ×10 整数滑杆钳制；slug 库存实时扫描。
- 程序化刷新 blockSignals 不触发 request_fn（回环防护）。
- ack ok 清 pending + InfoBar 已保存；fail 回滚快照值 + InfoBar 报错。
- 活体预览：有素材 frames 非空 + QTimer 活跃；无素材占位不炸；关闭 timer stop。
- qconfig 影子文件闸：绝不写 cwd/config/config.json。
pets 库存只读：home 用真实 <HERMES_HOME>（读 sprites.installed_pets），
合成图集只写 scratch 假 pets 目录。构造两次不炸（单实例由宿主管理）。"""
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

import sprites
import settings_window as sw
from settings_window import SettingsWindow

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui23-settingswin")
BUS.mkdir(parents=True, exist_ok=True)
for f in BUS.glob("settings_*.json"):
    f.unlink(missing_ok=True)
(BUS / "pet_error.log").unlink(missing_ok=True)
(BUS / "qfw_shadow_config.json").unlink(missing_ok=True)

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

# ---- fake request_fn：记录 (key, value, source)，rid 自增 ----
CALLS = []
def fake_request(key, value, source="settings"):
    rid = f"r{len(CALLS):04d}"
    CALLS.append((key, value, source))
    return rid

META = {"bus_dir": str(BUS), "hermes_home": HOME,
        "log_dir": str(BUS), "version": "0.23.0",
        "repo": "x", "cwd": "x"}
STATE = {"data": {"pet_state": "idle", "active_sessions": 2,
                  "gateway_uptime_s": 3725.5,
                  "tray": {"light": "green", "tier": "pro",
                           "version": "0.23.0", "update_available": True,
                           "bus": True}}, "ts": time.time()}

VIEW1 = {"ts": time.time(),
         "top": {"tray_enabled": True, "bus_enabled": False,
                 "degrade_window_s": 120, "verbose_log": True,
                 "package_index_url": "https://pypi.org/simple"},
         "pet": {"enable-pet": True, "slug": "fairy", "scale": 0.5,
                 "roam": False, "topmost": True},
         "explicit": {"top": ["tray_enabled", "bus_enabled",
                              "degrade_window_s", "verbose_log",
                              "package_index_url"],
                      "pet": ["enable-pet", "slug", "scale", "roam", "topmost"]}}

def write_view(v):
    (BUS / "settings_view.json").write_text(
        json.dumps(v, ensure_ascii=False), encoding="utf-8")

def write_ack(rid, ok, key, value, error=""):
    (BUS / "settings_ack.json").write_text(json.dumps(
        {"id": rid, "ok": ok, "key": key, "value": value,
         "error": error, "ts": time.time()}, ensure_ascii=False),
        encoding="utf-8")

def make_win(home=HOME):
    return SettingsWindow(META, STATE, fake_request, home=home)

# ============ 1. 构造读快照初值 ============
write_view(VIEW1)
win = make_win()
ck("init: tray switch == view", win.tray_card.isChecked() is True)
ck("init: bus switch == view", win.bus_card.isChecked() is False)
ck("init: degrade slider == view", win.degrade_card.slider.value() == 120)
ck("init: verbose switch == view", win.verbose_card.isChecked() is True)
ck("init: pkg combo == view",
   win.pkg_card.comboBox.currentData() == "https://pypi.org/simple")
ck("init: enable-pet == view", win.enable_card.isChecked() is True)
ck("init: slug combo == view", win.slug_card.comboBox.currentData() == "fairy")
ck("init: scale slider == view (0.5->5)", win.scale_card.slider.value() == 5)
ck("init: roam == view", win.roam_card.isChecked() is False)
ck("init: topmost == view", win.topmost_card.isChecked() is True)
ck("init: no request fired on construct", len(CALLS) == 0)
ck("init: slug options == real inventory",
   [win.slug_card.comboBox.itemData(i)
    for i in range(win.slug_card.comboBox.count())]
   == sprites.installed_pets(HOME))

# ============ 2. tray 开关二次确认（注入钩子，offscreen 不弹真窗）============
confirmed = []
win.confirm_tray_off = lambda: (confirmed.append(1), True)[1]
win.tray_card.switchButton.setChecked(False)
ck("tray off: confirm hook called", len(confirmed) == 1)
ck("tray off: request_fn(tray_enabled,False)",
   CALLS[-1] == ("tray_enabled", False, "settings"))
tray_rid = f"r{len(CALLS)-1:04d}"
ck("tray off: pending recorded", tray_rid in win._pending)
# 打开 tray 不需确认（先回 True 才能再测关闭）
win.tray_card.switchButton.setChecked(True)
ck("tray on: request_fn(tray_enabled,True)",
   CALLS[-1] == ("tray_enabled", True, "settings"))
# 取消 → 开关回弹，不发请求
win.confirm_tray_off = lambda: False
n_before = len(CALLS)
win.tray_card.switchButton.setChecked(False)
ck("tray off cancel: no request", len(CALLS) == n_before)
ck("tray off cancel: switch bounces back", win.tray_card.isChecked() is True)

# ============ 3. scale 滑杆 → 钳制浮点请求 ============
win.scale_card.slider.setValue(25)
ck("scale slider 25 -> request pet.scale 2.5",
   CALLS[-1] == ("pet.scale", 2.5, "settings"))
scale_rid = f"r{len(CALLS)-1:04d}"
win.scale_card.slider.setValue(1)
ck("scale slider 1 -> request pet.scale 0.1",
   CALLS[-1] == ("pet.scale", 0.1, "settings"))
ck("scale label shows float", win.scale_card.valueLabel.text() == "0.1")

# ============ 4. slug 组合 → 请求选中 ============
inv = sprites.installed_pets(HOME)
other = next((s for s in inv if s != "fairy"), None)
if other:
    idx = win.slug_card.comboBox.findData(other)
    win.slug_card.comboBox.setCurrentIndex(idx)
    ck("slug combo -> request pet.slug selected",
       CALLS[-1] == ("pet.slug", other, "settings"))
    slug_rid = f"r{len(CALLS)-1:04d}"
else:
    ck("slug combo -> request pet.slug selected", False)
    slug_rid = None

# ============ 5. 其它卡片信号 ============
win.bus_card.switchButton.setChecked(True)
ck("bus toggle -> request bus_enabled True",
   CALLS[-1] == ("bus_enabled", True, "settings"))
win.verbose_card.switchButton.setChecked(False)
ck("verbose toggle -> request verbose_log False",
   CALLS[-1] == ("verbose_log", False, "settings"))
win.degrade_card.slider.setValue(600)
ck("degrade slider -> request degrade_window_s 600",
   CALLS[-1] == ("degrade_window_s", 600, "settings"))
win.topmost_card.switchButton.setChecked(False)
ck("topmost toggle -> request pet.topmost False",
   CALLS[-1] == ("pet.topmost", False, "settings"))
win.roam_card.switchButton.setChecked(True)
ck("roam toggle -> request pet.roam True",
   CALLS[-1] == ("pet.roam", True, "settings"))
win.enable_card.switchButton.setChecked(False)
ck("enable toggle -> request pet.enable-pet False",
   CALLS[-1] == ("pet.enable-pet", False, "settings"))
# 包源：选预设
idx = win.pkg_card.comboBox.findData("https://mirrors.aliyun.com/pypi/simple/")
win.pkg_card.comboBox.setCurrentIndex(idx)
ck("pkg preset -> request package_index_url ali",
   CALLS[-1] == ("package_index_url",
                 "https://mirrors.aliyun.com/pypi/simple/", "settings"))
# 包源：自定义（注入钩子）
win.ask_custom_url = lambda cur: "https://my.mirror/simple"
cidx = win.pkg_card.comboBox.findData(sw.CUSTOM_SENTINEL)
win.pkg_card.comboBox.setCurrentIndex(cidx)
ck("pkg custom -> request package_index_url custom",
   CALLS[-1] == ("package_index_url", "https://my.mirror/simple", "settings"))
ck("pkg custom: combo shows custom entry",
   win.pkg_card.comboBox.currentData() == "https://my.mirror/simple")
# 自定义取消 → 回弹快照，不发请求
win.ask_custom_url = lambda cur: None
n_before = len(CALLS)
win.pkg_card.comboBox.setCurrentIndex(
    win.pkg_card.comboBox.findData(sw.CUSTOM_SENTINEL))
ck("pkg custom cancel: no request", len(CALLS) == n_before)

# ============ 6. 程序化刷新不触发 request_fn（blockSignals 回环防护）============
# 先清掉前面累积的 pending，才能验证"刷新真的把卡片刷到快照值"。
win._pending.clear()
VIEW2 = json.loads(json.dumps(VIEW1))
VIEW2["top"]["bus_enabled"] = True
VIEW2["top"]["degrade_window_s"] = 900
VIEW2["pet"]["scale"] = 1.2
VIEW2["pet"]["roam"] = True
write_view(VIEW2)
n_before = len(CALLS)
win._poll_view()
ck("refresh: zero request_fn calls (blockSignals)", len(CALLS) == n_before)
ck("refresh: bus card updated", win.bus_card.isChecked() is True)
ck("refresh: degrade card updated", win.degrade_card.slider.value() == 900)
ck("refresh: scale card updated (1.2->12)", win.scale_card.slider.value() == 12)
ck("refresh: roam card updated", win.roam_card.isChecked() is True)
# 内容签名去重：同内容再 poll 不重复 apply
applies = win._view_apply_count
win._poll_view()
ck("refresh: same content dedup (no re-apply)",
   win._view_apply_count == applies)
# 未决键跳过：发一个 scale 请求（乐观 1.5），快照却写 2.9 → 刷新保乐观值
win.scale_card.slider.setValue(15)
scale_rid = f"r{len(CALLS)-1:04d}"
ck("pending scale recorded", scale_rid in win._pending)
VIEW3 = json.loads(json.dumps(VIEW2))
VIEW3["pet"]["scale"] = 2.9
write_view(VIEW3)
win._poll_view()
ck("refresh: pending key keeps optimistic value (15 not 29)",
   win.scale_card.slider.value() == 15)

# ============ 7. ack ok → pending 清 + InfoBar 已保存 ============
write_ack(scale_rid, True, "pet.scale", 1.5)
win._poll_ack()
ck("ack ok: pending cleared", scale_rid not in win._pending)
ck("ack ok: InfoBar success", (win.last_info or ("",))[0] == "success"
   and win.last_info[1] == "已保存")
# 他人 rid（非本窗口发出）→ 忽略
n_proc = win._ack_processed
write_ack("zzzzzz", True, "pet.roam", True)
win._poll_ack()
ck("ack foreign rid ignored", win._ack_processed == n_proc)

# ============ 8. ack fail → 卡片回滚快照值 + InfoBar 报错 ============
# slug 当前视图值 fairy；选 other 发请求再拒 → 回滚 fairy
if other:
    win.slug_card.comboBox.setCurrentIndex(
        win.slug_card.comboBox.findData(other))
    fail_rid = f"r{len(CALLS)-1:04d}"
    write_ack(fail_rid, False, "pet.slug", other, "slug not installed")
    win._poll_ack()
    ck("ack fail: pending cleared", fail_rid not in win._pending)
    ck("ack fail: InfoBar error", (win.last_info or ("",))[0] == "error"
       and win.last_info[1] == "保存失败")
    ck("ack fail: slug card rolled back to view",
       win.slug_card.comboBox.currentData() == "fairy")
# scale fail 回滚：发 1.6 请求再拒（视图 scale=2.9 → 29）
win.scale_card.slider.setValue(16)
fail_rid2 = f"r{len(CALLS)-1:04d}"
write_ack(fail_rid2, False, "pet.scale", 1.6, "writer failed")
n_before = len(CALLS)
win._poll_ack()
ck("ack fail scale: rolled back to view 2.9->29",
   win.scale_card.slider.value() == 29)
ck("ack fail rollback: no request fired", len(CALLS) == n_before)

# ============ 9. explicit 缺键 → 默认值/未设置角标 ============
VIEW4 = {"ts": time.time(),
         "top": {"tray_enabled": True, "bus_enabled": True,
                 "degrade_window_s": 300, "verbose_log": False,
                 "package_index_url":
                     "https://pypi.tuna.tsinghua.edu.cn/simple"},
         "pet": {"enable-pet": False, "slug": "fairy", "scale": 0.7,
                 "roam": True, "topmost": True},
         "explicit": {"top": ["tray_enabled"], "pet": ["slug"]}}
write_view(VIEW4)
win._pending.clear()
win._poll_view()
ck("missing bus_enabled: default mark",
   sw.MARK_DEFAULT in win.bus_card.contentLabel.text())
ck("missing tray_enabled present: no mark",
   sw.MARK_DEFAULT not in win.tray_card.contentLabel.text())
ck("missing enable-pet: unset mark (not false-label)",
   sw.MARK_UNSET in win.enable_card.contentLabel.text())
ck("missing scale: default mark",
   sw.MARK_DEFAULT in win.scale_card.contentLabel.text())
ck("explicit slug: no mark",
   sw.MARK_DEFAULT not in win.slug_card.contentLabel.text())
ck("missing-key flags recorded", win._default_marks.get("pet.enable-pet") is True
   and win._default_marks.get("tray_enabled") is False)
# 缺键值仍按快照显示（enable-pet 运行时 false）
ck("missing enable-pet shows runtime false", win.enable_card.isChecked() is False)

# ============ 10. 活体预览（真实库存只读 + scratch 合成图集）============
ck("preview: real inventory frames non-empty", len(win._preview_frames) > 0)
ck("preview: timer active with asset", win._preview_timer.isActive())
ck("preview: interval == LOOP_MS/FRAMES_PER_STATE",
   win._preview_timer.interval() == sprites.LOOP_MS // sprites.FRAMES_PER_STATE)
# 轮播推进
win._preview_step()
ck("preview: step advances frame", win._preview_idx == 1)
# scratch 合成图集（8 列 x 9 行 Codex，idle 行画满 6 帧）×2 只假宠
from PIL import Image, ImageDraw
FAKE_HOME = BUS / "fakehome"
cols, rows = 8, 9
for slug in ("testpet", "testpet2"):
    petdir = FAKE_HOME / "pets" / slug
    petdir.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGBA", (cols * sprites.FRAME_W, rows * sprites.FRAME_H),
                   (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(sprites.FRAMES_PER_STATE):
        d.rectangle([i * sprites.FRAME_W + 20, 20,
                     i * sprites.FRAME_W + 100, 150], fill=(200, 60, 60, 255))
    im.save(petdir / "spritesheet.png")
VIEW5 = json.loads(json.dumps(VIEW4))
VIEW5["pet"]["slug"] = "testpet"
write_view(VIEW5)
win2 = make_win(home=str(FAKE_HOME))
ck("preview: synthetic sheet frames == 6", len(win2._preview_frames) == 6)
ck("preview: synthetic timer active", win2._preview_timer.isActive())
# 换宠即时重建：选 testpet2 → preview key 变
win2.slug_card.comboBox.setCurrentIndex(
    win2.slug_card.comboBox.findData("testpet2"))
ck("preview: slug change rebuilds", win2._preview_key[0] == "testpet2")
# 拉大小即时重建
win2.scale_card.slider.setValue(20)
ck("preview: scale change rebuilds", win2._preview_key[1] == 2.0)
ck("preview: scaled frame width == 192*2",
   win2._preview_frames[0].width() == sprites.FRAME_W * 2)
# 无素材占位不炸
win3 = make_win(home=str(BUS / "nohome"))
ck("preview: missing asset placeholder text",
   "素材缺失" in win3.preview_label.text())
ck("preview: missing asset no frames", len(win3._preview_frames) == 0)
ck("preview: missing asset timer stopped", not win3._preview_timer.isActive())

# ============ 11. 关闭 → 预览/轮询 timer stop ============
ck("before close: poll timer active", win._poll_timer.isActive())
win.close()
ck("close: preview timer stopped", not win._preview_timer.isActive())
ck("close: poll timer stopped", not win._poll_timer.isActive())
win2.close()
win3.close()

# ============ 12. 构造两次不炸 + 快照缺失兜底 ============
w_a = make_win()
w_b = make_win()
ck("double construct: both alive", w_a is not w_b)
ck("double construct: second reads view too",
   w_b.bus_card.isChecked() is True)
w_a.close()
w_b.close()
(BUS / "settings_view.json").unlink(missing_ok=True)
w_c = make_win()
ck("view absent: fallback defaults, no crash",
   w_c.tray_card.isChecked() is True
   and w_c.scale_card.slider.value() == 7)
w_c.close()

# ============ 12b. 事件反应 event_reactions 卡片（收口接线补） ============
# VIEW1 无 event_reactions 键 → 缺键默认 true + MARK_DEFAULT 角标（非"未设置"）
w_r = make_win()
ck("reactions: missing key defaults true", w_r.reactions_card.isChecked() is True)
ck("reactions: missing key MARK_DEFAULT badge",
   sw.MARK_DEFAULT in w_r.reactions_card.contentLabel.text()
   and sw.MARK_UNSET not in w_r.reactions_card.contentLabel.text())
ck("reactions: default mark recorded",
   w_r._default_marks.get("pet.event_reactions") is True)
# 显式 false 快照 → 卡片 false + 角标消失
VIEW_R = json.loads(json.dumps(VIEW1))
VIEW_R["pet"]["event_reactions"] = False
VIEW_R["explicit"]["pet"].append("event_reactions")
write_view(VIEW_R)
n_before = len(CALLS)
w_r._poll_view()
ck("reactions: explicit false applied", w_r.reactions_card.isChecked() is False)
ck("reactions: explicit badge cleared",
   sw.MARK_DEFAULT not in w_r.reactions_card.contentLabel.text())
ck("reactions: refresh zero requests (blockSignals)", len(CALLS) == n_before)
# 用户拨开 → 发请求 pet.event_reactions=True
w_r.reactions_card.switchButton.setChecked(True)
ck("reactions: toggle fires request",
   CALLS[-1] == ("pet.event_reactions", True, "settings"))
rid_r = f"r{len(CALLS)-1:04d}"
ck("reactions: pending recorded", rid_r in w_r._pending)
# ack ok → pending 清 + 已保存
write_ack(rid_r, True, "pet.event_reactions", True)
w_r._poll_ack()
ck("reactions: ack ok pending cleared", rid_r not in w_r._pending)
ck("reactions: ack ok InfoBar", (w_r.last_info or ("",))[0] == "success")
# ack fail → 回滚快照值 false，且回滚不再发请求
# （ack ok 后卡片已是 True；须先拨回 False 再拨 True 才产生 checkedChanged，
#  否则 setChecked(True) 是 no-op 不发请求——与既有 tray 关闭测试同款时序）
w_r.reactions_card.switchButton.setChecked(False)
w_r.reactions_card.switchButton.setChecked(True)
rid_f = f"r{len(CALLS)-1:04d}"
ck("reactions: fail-case pending recorded", rid_f in w_r._pending)
write_ack(rid_f, False, "pet.event_reactions", True, "writer failed")
n_before = len(CALLS)
w_r._poll_ack()
ck("reactions: ack fail rolled back to view false",
   w_r.reactions_card.isChecked() is False)
ck("reactions: fail rollback no request fired", len(CALLS) == n_before)
w_r.close()

# ============ 13. qconfig 影子闸：绝不写 cwd/config.json ============
ck("qconfig file redirected to scratch shadow",
   str(sw.qconfig.file).startswith(str(BUS)))
ck("no cwd config/config.json written",
   not (Path.cwd() / "config" / "config.json").exists())
ck("no config.yaml touched in scratch", not (BUS / "config.yaml").exists())
ck("no pet_cfg.json written", not (BUS / "pet_cfg.json").exists())
err_log = BUS / "pet_error.log"
ck("no unexpected error log", not err_log.exists()
   or err_log.stat().st_size == 0)

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS"
      + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(0 if not fails else 1)
