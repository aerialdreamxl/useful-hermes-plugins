# -*- coding: utf-8 -*-
"""设置界面 F7：settings 总线协议回归（CONTRACT §6）。宿主 python 跑。
- validate_request 纯函数：白名单 key / 类型范围钳制 / 畸形输入收敛拒绝。
- build_view 快照：缺键补默认仅进快照、explicit 键表、enable-pet 三态。
- _settings_watcher 集成：req→官方 writer(fake ctx)→ack；幂等；非法拒写。
只用 scratch 总线，绝不碰真实总线/config。"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()
cfg = gui.cfgmod

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

SLUGS = ("fairy", "jue", "cyrene")

# ============ validate_request：白名单 ============
ck("top key allowed", cfg.validate_request({"key": "tray_enabled", "value": False})[0] is True)
ck("pet key allowed", cfg.validate_request({"key": "pet.scale", "value": 1.0})[0] is True)
ck("event_reactions key allowed (事件反应)",
   cfg.validate_request({"key": "pet.event_reactions", "value": False})[0] is True)
ck("event_reactions in REQ_KEYS", "pet.event_reactions" in cfg.REQ_KEYS)
ck("foreign key rejected", cfg.validate_request({"key": "model.api_key", "value": "x"})[0] is False)
ck("nested escape rejected", cfg.validate_request({"key": "pet.slug.evil", "value": "x"})[0] is False)
ck("non-dict rejected", cfg.validate_request(None)[0] is False)
ck("missing value rejected", cfg.validate_request({"key": "pet.roam"})[0] is False)
ck("non-str key rejected", cfg.validate_request({"key": 123, "value": True})[0] is False)

# ============ validate_request：类型/范围钳制 ============
ok, k, v, e = cfg.validate_request({"key": "pet.scale", "value": 9.9})
ck("scale clamped high", ok and v == 3.0)
ok, k, v, e = cfg.validate_request({"key": "pet.scale", "value": 0.001})
ck("scale clamped low", ok and v == 0.1)
ok, k, v, e = cfg.validate_request({"key": "degrade_window_s", "value": 99999})
ck("window clamped high", ok and v == 1800)
ok, k, v, e = cfg.validate_request({"key": "degrade_window_s", "value": 1})
ck("window clamped low", ok and v == 60)
ok, k, v, e = cfg.validate_request({"key": "pet.scale", "value": "abc"})
ck("scale non-numeric rejected", ok is False and e)
ok, k, v, e = cfg.validate_request({"key": "tray_enabled", "value": 1})
ck("bool coerced from int", ok and v is True)
ok, k, v, e = cfg.validate_request({"key": "pet.enable-pet", "value": 0})
ck("enable-pet bool coerced", ok and v is False)
ok, k, v, e = cfg.validate_request({"key": "pet.event_reactions", "value": 1})
ck("event_reactions bool coerced from int", ok and v is True)
ok, k, v, e = cfg.validate_request({"key": "pet.event_reactions", "value": 0})
ck("event_reactions false honored", ok and v is False)

# ============ validate_request：slug 库存 / url ============
ck("slug installed ok", cfg.validate_request({"key": "pet.slug", "value": "jue"}, SLUGS)[0] is True)
ck("slug empty ok", cfg.validate_request({"key": "pet.slug", "value": ""}, SLUGS)[0] is True)
ok, k, v, e = cfg.validate_request({"key": "pet.slug", "value": "ghost"}, SLUGS)
ck("slug not installed rejected", ok is False and "not installed" in e)
ck("url https ok", cfg.validate_request({"key": "package_index_url", "value": "https://x/simple"})[0] is True)
ok, k, v, e = cfg.validate_request({"key": "package_index_url", "value": "ftp://x"})
ck("url non-http rejected", ok is False and e)
ck("url empty ok", cfg.validate_request({"key": "package_index_url", "value": ""})[0] is True)

# ============ build_view：快照 + explicit + 三态 ============
class FakeCtx:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.writes = []
    def get_config(self, key, default=None):
        return self.store.get(key, default)
    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value

# config 缺 enable-pet（A1 现场）+ 缺部分顶层键
ctx = FakeCtx({"pet": {"slug": "jue", "scale": 0.7, "roam": True, "topmost": True},
               "tray_enabled": True})
view = cfg.build_view(ctx, SLUGS)
ck("view pet enable-pet runtime false (missing)", view["pet"]["enable-pet"] is False)
ck("view explicit pet excludes enable-pet", "enable-pet" not in view["explicit"]["pet"])
ck("view explicit pet includes slug", "slug" in view["explicit"]["pet"])
ck("view explicit top only real keys", view["explicit"]["top"] == ["tray_enabled"])
ck("view top fills missing default (runtime only)", view["top"]["degrade_window_s"] == 300)
ck("build_view never writes config", len(ctx.writes) == 0)
# 事件反应 event_reactions：缺键运行时 true（作者点名默认开），绝不落 config
ck("view event_reactions runtime true (missing)", view["pet"]["event_reactions"] is True)
ck("view explicit pet excludes event_reactions", "event_reactions" not in view["explicit"]["pet"])
ck("event_reactions not auto-persisted (A1)",
   all("event_reactions" not in v for k, v in ctx.writes if k == "pet"))
ck("event_reactions not a display-fill key", "event_reactions" not in cfg.PET_DISPLAY_KEYS)
# 显式 event_reactions false 照用
ctx_er = FakeCtx({"pet": {"event_reactions": False}})
v_er = cfg.build_view(ctx_er, SLUGS)
ck("view explicit event_reactions false honored", v_er["pet"]["event_reactions"] is False)
ck("view explicit lists event_reactions when present", "event_reactions" in v_er["explicit"]["pet"])
# 显式 enable-pet true 照用
ctx2 = FakeCtx({"pet": {"enable-pet": True, "slug": "fairy"}})
v2 = cfg.build_view(ctx2, SLUGS)
ck("view explicit enable-pet true honored", v2["pet"]["enable-pet"] is True)
ck("view explicit lists enable-pet when present", "enable-pet" in v2["explicit"]["pet"])

# ============ _settings_watcher 集成（fake ctx + scratch 总线）============
import threading
from _env import scratch  # noqa: E402
SCRATCH = scratch("gui24-events-settings")
BUS = SCRATCH / "cache" / "gateway-ui"
BUS.mkdir(parents=True, exist_ok=True)
for f in ("settings_req.json", "settings_ack.json", "settings_view.json"):
    (BUS / f).unlink(missing_ok=True)

gui._home = lambda: SCRATCH
gui._CTX = FakeCtx({"pet": {"slug": "jue", "scale": 0.7, "roam": True, "topmost": True}})
gui._STOP = threading.Event()
gui._REQ_DONE.clear()
gui._VIEW_LAST["sig"] = None
# 假库存：scratch 下建 pets/jue（spritesheet 占位）
(BUS.parent.parent / "pets" / "jue").mkdir(parents=True, exist_ok=True)
(BUS.parent.parent / "pets" / "jue" / "spritesheet.webp").write_bytes(b"RIFF")
t = threading.Thread(target=gui._settings_watcher, daemon=True)
t.start()

def send(rid, key, value):
    (BUS / "settings_req.json").write_text(json.dumps(
        {"id": rid, "ts": time.time(), "key": key, "value": value, "source": "settings"}),
        encoding="utf-8")
    time.sleep(0.6)
    return json.loads((BUS / "settings_ack.json").read_text(encoding="utf-8"))

a = send("w1", "pet.enable-pet", True)
ck("watcher: enable-pet true persisted", gui._CTX.store["pet"].get("enable-pet") is True)
ck("watcher: ack ok w1", a["id"] == "w1" and a["ok"] is True)
a = send("w2", "pet.scale", 5.0)
ck("watcher: scale clamped to 3.0 in config", gui._CTX.store["pet"].get("scale") == 3.0)
ck("watcher: ack carries clamped value", a["value"] == 3.0)
a = send("w3", "pet.slug", "ghost")
ck("watcher: bad slug rejected", a["ok"] is False)
ck("watcher: bad slug not written", gui._CTX.store["pet"].get("slug") == "jue")
a = send("w4", "bus_enabled", False)
ck("watcher: top key persisted", gui._CTX.store.get("bus_enabled") is False)
# 事件反应：event_reactions 显式关闭 → 官方 writer 落盘（用户显式操作才写）
a = send("w5", "pet.event_reactions", False)
ck("watcher: event_reactions false persisted", gui._CTX.store["pet"].get("event_reactions") is False)
ck("watcher: ack ok w5", a["id"] == "w5" and a["ok"] is True)
n_before = len(gui._CTX.writes)
a = send("w2", "pet.scale", 0.5)   # 复用 id
ck("watcher: duplicate id no extra write", len(gui._CTX.writes) == n_before)
gui._STOP.set()
t.join(timeout=2)

# ============ 余额气泡 balance 三键（CONTRACT §10.4）============
# --- 白名单 ---
ck("balance.enabled in REQ_KEYS", "balance.enabled" in cfg.REQ_KEYS)
ck("balance.refresh_s in REQ_KEYS", "balance.refresh_s" in cfg.REQ_KEYS)
ck("balance.providers_dir in REQ_KEYS", "balance.providers_dir" in cfg.REQ_KEYS)
ck("balance.enabled allowed", cfg.validate_request({"key": "balance.enabled", "value": True})[0] is True)
ck("balance foreign subkey rejected", cfg.validate_request({"key": "balance.secret", "value": "x"})[0] is False)
ck("balance nested escape rejected", cfg.validate_request({"key": "balance.enabled.evil", "value": True})[0] is False)
# --- enabled bool 强转 ---
ok, k, v, e = cfg.validate_request({"key": "balance.enabled", "value": 1})
ck("balance.enabled bool coerced from int", ok and v is True)
ok, k, v, e = cfg.validate_request({"key": "balance.enabled", "value": 0})
ck("balance.enabled false honored", ok and v is False)
# --- refresh_s 钳 60-3600 ---
ok, k, v, e = cfg.validate_request({"key": "balance.refresh_s", "value": 99999})
ck("balance.refresh_s clamped high 3600", ok and v == 3600)
ok, k, v, e = cfg.validate_request({"key": "balance.refresh_s", "value": 1})
ck("balance.refresh_s clamped low 60", ok and v == 60)
ok, k, v, e = cfg.validate_request({"key": "balance.refresh_s", "value": "abc"})
ck("balance.refresh_s non-numeric rejected", ok is False and e)
ok, k, v, e = cfg.validate_request({"key": "balance.refresh_s", "value": 120})
ck("balance.refresh_s in-range honored", ok and v == 120)
# --- providers_dir 绝对路径 / 不含 .. / 允许空 ---
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": ""})
ck("balance.providers_dir empty ok", ok and v == "")
# F26 收窄：目录里的 *.py 会被宿主执行 → 只许 HERMES_HOME 之下（旧用例的
# "任意绝对路径都放行"是修复前的契约，已作废）
HOME_DIR = str(Path(os.environ["HERMES_HOME"]) / "balance-providers")
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": HOME_DIR})
ck("balance.providers_dir under HERMES_HOME ok", ok and v == HOME_DIR)
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": "D:/custom/balance"})
ck("balance.providers_dir outside HERMES_HOME rejected",
   ok is False and "HERMES_HOME" in e)
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": "relative/path"})
ck("balance.providers_dir relative rejected", ok is False and "absolute" in e)
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": "D:/a/../b"})
ck("balance.providers_dir .. rejected", ok is False and ".." in e)
# --- read_balance_explicit 纯读 ---
ctx_b = FakeCtx({"balance": {"enabled": True, "refresh_s": 120, "ghost": "x"}})
eb = cfg.read_balance_explicit(ctx_b)
ck("read_balance_explicit filters unknown keys", eb == {"enabled": True, "refresh_s": 120})
ck("read_balance_explicit no writes", len(ctx_b.writes) == 0)
ck("read_balance_explicit missing block -> {}", cfg.read_balance_explicit(FakeCtx({})) == {})
ck("read_balance_explicit non-dict block -> {}", cfg.read_balance_explicit(FakeCtx({"balance": "x"})) == {})
# --- build_view balance 快照 + explicit ---
ctx_bv = FakeCtx({"pet": {"slug": "jue"}})   # 无 balance 块
vb = cfg.build_view(ctx_bv, SLUGS)
ck("view balance defaults when missing", vb["balance"] == {"enabled": False, "refresh_s": 300, "providers_dir": "", "token_plan_edition": "individual"})
ck("view explicit balance empty when missing", vb["explicit"]["balance"] == [])
ck("build_view balance never writes config", len(ctx_bv.writes) == 0)
ck("balance not a display-fill key (no seed path)", not hasattr(cfg, "BALANCE_DISPLAY_KEYS"))
# 显式值照用 + explicit 列出
ctx_bv2 = FakeCtx({"balance": {"enabled": True, "refresh_s": 90}})
vb2 = cfg.build_view(ctx_bv2, SLUGS)
ck("view explicit balance.enabled honored", vb2["balance"]["enabled"] is True)
ck("view explicit balance.refresh_s honored", vb2["balance"]["refresh_s"] == 90)
ck("view explicit balance lists real keys", set(vb2["explicit"]["balance"]) == {"enabled", "refresh_s"})
ck("view balance.providers_dir default when absent", vb2["balance"]["providers_dir"] == "")
# 显式 refresh_s 越界 → 快照层钳制（不落 config）
ctx_bv3 = FakeCtx({"balance": {"refresh_s": 99999}})
vb3 = cfg.build_view(ctx_bv3, SLUGS)
ck("view balance.refresh_s clamped in snapshot", vb3["balance"]["refresh_s"] == 3600)
ck("view balance clamp no write", len(ctx_bv3.writes) == 0)
# --- balance 块合并落盘语义（纯函数；__init__._apply_setting 的 balance 分支由收口接线）---
# 契约：balance.* 与 pet.* 同款整块合并——读显式块→改 leaf→整体 set_config("balance", block)。
def apply_balance(ctx, key, value):
    assert key.startswith("balance.")
    block = dict(cfg.read_balance_explicit(ctx))
    block[key[len("balance."):]] = value
    ctx.set_config("balance", block)
ctx_bw = FakeCtx({"balance": {"enabled": False}})
apply_balance(ctx_bw, "balance.enabled", True)
ck("balance merge: enabled toggled in block", ctx_bw.store["balance"]["enabled"] is True)
apply_balance(ctx_bw, "balance.refresh_s", 60)
ck("balance merge: preserves sibling keys", ctx_bw.store["balance"] == {"enabled": True, "refresh_s": 60})
ck("balance merge: writes whole block not dotted key",
   all(k == "balance" for k, v in ctx_bw.writes) and "balance.enabled" not in ctx_bw.store)
# validate→merge 全链路：非法 providers_dir 不落盘
ctx_bw2 = FakeCtx({"balance": {"enabled": True}})
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": "relative/x"})
ck("balance chain: invalid providers_dir rejected pre-write", ok is False)
if ok:
    apply_balance(ctx_bw2, k, v)
ck("balance chain: rejected value never merged", "providers_dir" not in ctx_bw2.store["balance"])
ok, k, v, e = cfg.validate_request({"key": "balance.refresh_s", "value": 5})
apply_balance(ctx_bw2, k, v)
ck("balance chain: clamped refresh_s merged", ctx_bw2.store["balance"]["refresh_s"] == 60)

# ============ 余额气泡修订 balance.token_plan_edition ============
ck("balance.token_plan_edition in REQ_KEYS", "balance.token_plan_edition" in cfg.REQ_KEYS)
ok, k, v, e = cfg.validate_request({"key": "balance.token_plan_edition", "value": "team"})
ck("edition team ok", ok and v == "team")
ok, k, v, e = cfg.validate_request({"key": "balance.token_plan_edition", "value": "INDIVIDUAL"})
ck("edition case normalized", ok and v == "individual")
ok, k, v, e = cfg.validate_request({"key": "balance.token_plan_edition", "value": "bogus"})
ck("edition bogus rejected", ok is False and e)
ctx_ed = FakeCtx({"balance": {"enabled": True}})
apply_balance(ctx_ed, "balance.token_plan_edition", "team")
ck("edition merge whole block", ctx_ed.store["balance"] == {"enabled": True, "token_plan_edition": "team"})
v_ed = cfg.build_view(FakeCtx({}))
ck("edition default individual in view", v_ed["balance"]["token_plan_edition"] == "individual")
ck("edition default not explicit", "token_plan_edition" not in v_ed["explicit"]["balance"])
v_ed2 = cfg.build_view(FakeCtx({"balance": {"token_plan_edition": "team"}}))
ck("edition explicit team in view", v_ed2["balance"]["token_plan_edition"] == "team"
   and "token_plan_edition" in v_ed2["explicit"]["balance"])
v_ed3 = cfg.build_view(FakeCtx({"balance": {"token_plan_edition": "bogus"}}))
ck("edition bogus explicit falls back individual", v_ed3["balance"]["token_plan_edition"] == "individual")

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)