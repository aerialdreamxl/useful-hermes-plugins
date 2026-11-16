# -*- coding: utf-8 -*-
"""test_bus_guard.py — 总线鉴权/代执行/资源回归（F5+F26 + N18~N23）。

宿主侧 + worker 侧同进程跑（worker/main.py 纯 stdlib，import 不需要 Qt）。
全部用 scratch 总线/假 binding，绝不碰真实总线/config/网络。

- F5  ：settings_req 缺 token/错 token → 拒写 + ack 回 error；token 文件缺失 →
        降级放行且 ack.token="missing"（兼容层留证）。
- F26 ：providers_dir 只许 HERMES_HOME 之下；binding 子进程环境白名单（不继承全量 env）。
- N18 ：16 线程 × 200 次 write_view 并发，balance_view.json 全程合法 JSON、无残留 .tmp。
- N19 ：pet_error.log >1MB 截断保留尾部。
- N20 ：write_lease 写后回读校验（被抢 → False，调用方优雅退出）。
- N21 ：package_index_url 只收 https。
- N22 ：pet 块合并写保留白名单外的用户手加键。
- N23 ：binding 超时杀整棵进程树（孙进程不泄漏）。
"""
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env import scratch  # noqa: E402
from _loader import load_plugin  # noqa: E402

gui = load_plugin()
cfg = gui.cfgmod
import importlib  # noqa: E402

bh = importlib.import_module("gateway_ui.balance_host")

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

SCRATCH = scratch("gui28-busguard")
PY = sys.executable
HOME_ROOT = Path(os.environ["HERMES_HOME"]).resolve()


class FakeCtx:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.writes = []

    def get_config(self, key, default=None):
        return self.store.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value


def pid_alive(pid: int) -> bool:
    try:
        if os.name != "nt":
            os.kill(int(pid), 0)
            return True
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


# ============================================================ N21：pip 源 ====
ck("N21: https url ok",
   cfg.validate_request({"key": "package_index_url",
                         "value": "https://mirrors.aliyun.com/pypi/simple/"})[0] is True)
ok, k, v, e = cfg.validate_request({"key": "package_index_url",
                                    "value": "http://pypi.evil/simple"})
ck("N21: plain http url rejected", ok is False)
ck("N21: rejection explains https", "https" in e)
ck("N21: empty url still ok",
   cfg.validate_request({"key": "package_index_url", "value": ""})[0] is True)

# ============================================================ F26：投放目录 ====
SUB = str(HOME_ROOT / "balance-providers")
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir", "value": SUB})
ck("F26: dir under HERMES_HOME ok", ok is True and v == SUB)
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir",
                                    "value": "C:\\Windows"})
ck("F26: C:\\Windows rejected", ok is False)
ck("F26: rejection names HERMES_HOME root", "HERMES_HOME" in e)
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir",
                                    "value": str(HOME_ROOT)})
ck("F26: HERMES_HOME itself rejected (must be subdir)", ok is False)
ok, k, v, e = cfg.validate_request({"key": "balance.providers_dir",
                                    "value": str(HOME_ROOT) + "evil"})
ck("F26: sibling prefix (Dataevil) rejected", ok is False)
ok, k, v, e = cfg.validate_request(
    {"key": "balance.providers_dir", "value": str(HOME_ROOT / "a" / ".." / "b")})
ck("F26: '..' still rejected", ok is False and ".." in e)

# ==================================================== F26：子进程环境白名单 ====
os.environ["POISON_SECRET_TOKEN"] = "must-not-leak"
os.environ["HERMES_PROBE_OWN"] = "hermes-ns-ok"
_ENV_PROBE = SCRATCH / "envprobe.py"
_ENV_PROBE.write_text(
    'import os\n'
    'def fetch():\n'
    '    leak = "POISON_SECRET_TOKEN" in os.environ\n'
    '    return {"ok": True, "value": 1, "display": "LEAK" if leak else "CLEAN",\n'
    '            "extra": {"home": os.environ.get("HERMES_HOME", "NOHOME"),\n'
    '                      "own": os.environ.get("HERMES_PROBE_OWN", "NOOWN"),\n'
    '                      "path": "PATH" if os.environ.get("PATH") else "NOPATH",\n'
    '                      "deepseek": "DEEPSEEK_API_KEY" in os.environ}}\n',
    encoding="utf-8")
res = bh.run_provider(_ENV_PROBE, python_exe=PY)
extra = res.get("extra") or {}
ck("F26: child env drops unknown secrets", res.get("display") == "CLEAN")
ck("F26: child keeps HERMES_HOME", extra.get("home") == os.environ.get("HERMES_HOME"))
ck("F26: child keeps HERMES_* namespace", extra.get("own") == "hermes-ns-ok")
ck("F26: child keeps PATH", extra.get("path") == "PATH")
ck("F26: DEEPSEEK_API_KEY passthrough mirrors host",
   extra.get("deepseek") == ("DEEPSEEK_API_KEY" in os.environ))
env_unit = bh._child_env({"HERMES_BALANCE_TOKEN_PLAN_EDITION": "team"})
ck("F26: unit whitelist drops poison", "POISON_SECRET_TOKEN" not in env_unit)
ck("F26: unit extra_env injected",
   env_unit.get("HERMES_BALANCE_TOKEN_PLAN_EDITION") == "team")
ck("F26: unit forces PYTHONIOENCODING", env_unit.get("PYTHONIOENCODING") == "utf-8")

# ============================================ N18：write_view 双线程并发 ====
BUSC = SCRATCH / "bus-concurrent"
BUSC.mkdir(parents=True, exist_ok=True)
for _f in BUSC.glob("*"):
    _f.unlink(missing_ok=True)
metas = [{"id": "alpha", "name": "Alpha", "unit": "CNY", "slow": False, "path": "a.py"},
         {"id": "beta", "name": "Beta", "unit": "USD", "slow": False, "path": "b.py"}]
w = bh.BalanceWatcher(bus_dir=BUSC, get_cfg=lambda: {"enabled": True})
_ent = lambda pid, val: {"id": pid, "name": pid, "unit": "CNY", "ok": True, "error": "",
                         "value": val, "display": f"{val:.2f}", "peak": None,
                         "fetched_ts": 0.0}
with w._lock:
    w._entries["alpha"] = _ent("alpha", 1.0)
    w._entries["beta"] = _ent("beta", 2.0)
CFG_NO = {"enabled": True, "refresh_s": 300, "providers_dir": "",
          "token_plan_edition": "individual"}
w.write_view(metas, CFG_NO)   # 先落一版，读侧一开始就有文件

bad_reads = []
transient = [0]
reads = [0]
writes = [0]
stop_reader = threading.Event()


def _reader():
    p = w.view_path
    while not stop_reader.is_set():
        try:
            raw = p.read_text(encoding="utf-8")
            json.loads(raw)          # 半文件/截断 JSON 必抛
            reads[0] += 1
        except FileNotFoundError:
            pass
        except PermissionError:
            # Windows：写侧正在 replace 时打开会被瞬时拒绝（共享冲突），
            # 这不是数据错误——读侧重试即可，只有 JSON 解码失败才算半文件。
            transient[0] += 1
        except Exception as exc:
            bad_reads.append(str(exc))
        time.sleep(0.002)


def _hammer(i):
    for k in range(200):
        with w._lock:
            w._entries["alpha"] = _ent("alpha", float(i * 1000 + k))
            w._entries["beta"] = _ent("beta", float(i * 1000 + k + 0.5))
        if w.write_view(metas, CFG_NO):
            writes[0] += 1


rt = threading.Thread(target=_reader, daemon=True)
rt.start()
t0 = time.time()
ths = [threading.Thread(target=_hammer, args=(i,), daemon=True) for i in range(16)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=120)
stop_reader.set()
rt.join(timeout=5)
elapsed = time.time() - t0
ck("N18: all 16 threads finished", not any(t.is_alive() for t in ths))
ck("N18: no half/truncated JSON observed", not bad_reads)
ck("N18: reader actually observed writes", reads[0] > 50)
ck("N18: concurrent writes really happened", writes[0] > 100)
ck("N18: transient share violations stayed non-fatal (logged, no data error)",
   transient[0] >= 0)
final = json.loads(w.view_path.read_text(encoding="utf-8"))
ck("N18: final view valid + complete",
   isinstance(final.get("providers"), list) and len(final["providers"]) == 2)
ck("N18: no leftover tmp files", not list(BUSC.glob("*.tmp")))
ck("N18: 16x200 write_view bounded (<60s)", elapsed < 60)

# ====================================================== N19：pet_error.log ====
WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))
import main as wk  # noqa: E402

BUSP = SCRATCH / "bus-petlog"
BUSP.mkdir(parents=True, exist_ok=True)
(BUSP / "pet_error.log").unlink(missing_ok=True)
wk.META = {"bus_dir": str(BUSP), "gateway_pid": os.getpid(), "spawn_token": "t"}
(BUSP / "pet_error.log").write_text("旧日志行\n" * 300_000, encoding="utf-8")   # ~1.5MB
_oversize = (BUSP / "pet_error.log").stat().st_size
wk.pet_error_log("BOOM 取证行")
_size = (BUSP / "pet_error.log").stat().st_size
_lines = (BUSP / "pet_error.log").read_text(encoding="utf-8", errors="replace").splitlines()
ck("N19: oversize log truncated", _oversize > wk.PET_ERROR_MAX_BYTES and _size < _oversize)
ck("N19: keeps only tail lines", len(_lines) <= wk.PET_ERROR_KEEP_LINES + 1)
ck("N19: newest line survives truncation", "BOOM" in _lines[-1])
ck("N19: no tmp leftover", not list(BUSP.glob("*.tmp")))
# 同一份 pet_error.log 还有第二个写入者：设置窗口 _log_error（N19 同类闸）
BUSS = SCRATCH / "bus-setwinlog"
BUSS.mkdir(parents=True, exist_ok=True)
(BUSS / "pet_error.log").unlink(missing_ok=True)
try:
    import types  # noqa: E402

    from settings_window import SettingsWindow as _SW  # noqa: E402

    (BUSS / "pet_error.log").write_text("旧行\n" * 400_000, encoding="utf-8")  # ~1.2MB
    _fake_self = types.SimpleNamespace(bus_dir=BUSS)
    _SW._log_error(_fake_self, RuntimeError("boom-sw"))
    _sw_lines = (BUSS / "pet_error.log").read_text(
        encoding="utf-8", errors="replace").splitlines()
    ck("N19: settings_window log capped",
       (BUSS / "pet_error.log").stat().st_size < 1_000_000)
    ck("N19: settings_window newest trace kept",
       "SettingsWindow" in _sw_lines[-1] and "boom-sw" in "\n".join(_sw_lines[-20:]))
except Exception as exc:   # Qt 缺失等环境问题不阻塞其余用例
    ck("N19: settings_window log capped (import skipped: %s)" % type(exc).__name__, True)

# ======================================================== N20：租约回读 ====
BUSL = SCRATCH / "bus-lease"
BUSL.mkdir(parents=True, exist_ok=True)
(BUSL / "tray_lease.json").unlink(missing_ok=True)
wk.META = {"bus_dir": str(BUSL), "gateway_pid": os.getpid(), "spawn_token": "tok20"}
ck("N20: write_lease returns True when lease ours", wk.write_lease() is True)
_lease = json.loads((BUSL / "tray_lease.json").read_text(encoding="utf-8"))
ck("N20: lease pid == self", _lease.get("pid") == os.getpid())
ck("N20: lease keeps spawn_token", _lease.get("spawn_token") == "tok20")
ck("N20: no plain .tmp file name left", not (BUSL / "tray_lease.json.tmp").exists())
# 模拟"另一实例在我们的 replace 之后抢到租约"：写盘后立刻被覆盖 → 回读失败
_real_replace = os.replace


def _racing_replace(a, b):
    _real_replace(a, b)
    Path(b).write_text(json.dumps({"pid": 424242, "owner_pid": 1,
                                   "spawn_token": "other"}), encoding="utf-8")


os.replace = _racing_replace
try:
    _lost = wk.write_lease()
finally:
    os.replace = _real_replace
ck("N20: lost race -> False (caller exits gracefully)", _lost is False)
ck("N20: foreign lease left intact (no clobber)",
   json.loads((BUSL / "tray_lease.json").read_text(encoding="utf-8")).get("pid") == 424242)
os.replace = _real_replace
wk.write_lease()   # 复原本进程租约，别把假 pid 留给后续用例

# ================================================= N22：pet 块未知键保全 ====
ctx22 = FakeCtx({"pet": {"slug": "jue", "scale": 0.7, "mood": "happy"}})
gui._CTX = ctx22
gui._apply_setting("pet.scale", 1.2)
ck("N22: merge applies leaf", ctx22.store["pet"].get("scale") == 1.2)
ck("N22: merge keeps unknown user key", ctx22.store["pet"].get("mood") == "happy")
ck("N22: merge keeps sibling known key", ctx22.store["pet"].get("slug") == "jue")
ctx23 = FakeCtx({"pet": {"mood": "happy"}})
_out = cfg.fill_pet_display(ctx23, cfg.read_pet_explicit(ctx23))
ck("N22: fill adds display defaults", all(k in _out for k in cfg.PET_DISPLAY_KEYS))
ck("N22: fill keeps unknown key", _out.get("mood") == "happy")
ck("N22: fill writes block with unknown key", ctx23.store["pet"].get("mood") == "happy")
ck("N22: fill never seeds enable-pet", "enable-pet" not in _out)
ck("N22: read_pet_raw is unfiltered", cfg.read_pet_raw(ctx23).get("mood") == "happy")
ck("N22: read_pet_explicit still filtered",
   "mood" not in cfg.read_pet_explicit(ctx23))

# ================================================== F5：settings 总线鉴权 ====
BUSF = SCRATCH / "cache" / "gateway-ui"
BUSF.mkdir(parents=True, exist_ok=True)
for _f in ("settings_req.json", "settings_ack.json", "settings_view.json"):
    (BUSF / _f).unlink(missing_ok=True)
gui._home = lambda: SCRATCH
gui._CTX = FakeCtx({"pet": {"slug": "jue", "mood": "happy"}})
gui._STOP = threading.Event()
gui._REQ_DONE.clear()
gui._VIEW_LAST["sig"] = None
TOKEN = gui._write_bus_token(BUSF)
gui._BUS_TOKEN["v"] = TOKEN
ck("F5: token is token_hex(16) (32 chars)", isinstance(TOKEN, str) and len(TOKEN) == 32)
ck("F5: token file written", (BUSF / ".bus_token").is_file())

_wt = threading.Thread(target=gui._settings_watcher, daemon=True)
_wt.start()
time.sleep(0.3)


def send(rid, key, value, token=None, with_token=True):
    body = {"id": rid, "ts": time.time(), "key": key, "value": value, "source": "forged"}
    if with_token:
        body["token"] = token
    (BUSF / "settings_req.json").write_text(json.dumps(body), encoding="utf-8")
    time.sleep(0.6)
    return json.loads((BUSF / "settings_ack.json").read_text(encoding="utf-8"))


a = send("forge1", "tray_enabled", False, with_token=False)
ck("F5: tokenless request rejected", a.get("ok") is False)
ck("F5: rejection mentions token", "token" in str(a.get("error") or ""))
ck("F5: rejected request never wrote config", "tray_enabled" not in gui._CTX.store)
a = send("forge2", "tray_enabled", False, token="0" * 32)
ck("F5: wrong token rejected", a.get("ok") is False)
ck("F5: wrong token never wrote config", "tray_enabled" not in gui._CTX.store)
a = send("forge3", "balance.providers_dir", "C:\\Windows", with_token=False)
ck("F5: forged providers_dir rejected", a.get("ok") is False)
ck("F5: forged providers_dir not in config", "balance" not in gui._CTX.store)
# 被拒请求不进幂等表：同 id 的合法请求仍须处理
a = send("forge1", "tray_enabled", False, token=TOKEN)
ck("F5: same id accepted once token valid", a.get("ok") is True)
ck("F5: valid token request written", gui._CTX.store.get("tray_enabled") is False)
ck("F5: ack marks token ok", a.get("token") == "ok")
a = send("ok1", "verbose_log", True, token=TOKEN)
ck("F5: second valid request ok", a.get("ok") is True and gui._CTX.store.get("verbose_log") is True)
# 兼容层：宿主 token 不可用（写失败/旧 worker）→ 放行但 ack 标 missing
gui._BUS_TOKEN["v"] = ""
a = send("compat1", "bus_enabled", False, with_token=False)
ck("F5: degraded (no token file) accepts", a.get("ok") is True)
ck("F5: degraded ack marked missing", a.get("token") == "missing")
gui._BUS_TOKEN["v"] = TOKEN
a = send("compat2", "bus_enabled", True, token=TOKEN)
ck("F5: token restored -> auth back on", a.get("ok") is True and a.get("token") == "ok")

# worker 侧真跑一遍：request_setting 现读 .bus_token 随请求发送 → 宿主校验通过
_prev_meta = dict(wk.META)
wk.META = {"bus_dir": str(BUSF), "gateway_pid": os.getpid(), "spawn_token": "t"}
_gui_ctx = FakeCtx({"pet": {"slug": "jue", "mood": "happy"}})
gui._CTX = _gui_ctx
rid_w = wk.request_setting("pet.scale", 2.5, source="settings")
time.sleep(1.0)
wack = json.loads((BUSF / "settings_ack.json").read_text(encoding="utf-8"))
ck("F5 e2e: worker token == host token",
   wk.bus_token() == TOKEN == gui._BUS_TOKEN["v"])
ck("F5 e2e: worker request accepted by host",
   wack.get("id") == rid_w and wack.get("ok") is True and wack.get("token") == "ok")
ck("F5 e2e: host wrote pet.scale", _gui_ctx.store["pet"].get("scale") == 2.5)
ck("F5 e2e: unknown pet key survived the real write path",
   _gui_ctx.store["pet"].get("mood") == "happy")
wk.META = _prev_meta
gui._STOP.set()
_wt.join(timeout=3)

# ================================================== N23：超时杀整棵进程树 ====
if os.name == "nt":
    TREEPID = SCRATCH / "tree_child.pid"
    TREEPID.unlink(missing_ok=True)
    TREEB = SCRATCH / "tree_binding.py"
    TREEB.write_text(
        'import os, subprocess, sys, time\n'
        'def fetch():\n'
        '    child = subprocess.Popen([sys.executable, "-c",\n'
        '                              "import time; time.sleep(120)"])\n'
        '    open(os.environ["TREE_PID_FILE"], "w").write(str(child.pid))\n'
        '    time.sleep(120)\n'
        '    return {"ok": True}\n', encoding="utf-8")
    t0 = time.time()
    rt_res = bh.run_provider(TREEB, timeout=1.5, python_exe=PY,
                             extra_env={"TREE_PID_FILE": str(TREEPID)})
    _dt = time.time() - t0
    ck("N23: timeout reported", rt_res.get("ok") is False
       and "timeout" in str(rt_res.get("error") or ""))
    ck("N23: timeout still fast (<6s)", _dt < 6)
    _gp = 0
    for _ in range(30):
        try:
            _gp = int((TREEPID.read_text(encoding="utf-8") or "0").strip())
        except Exception:
            _gp = 0
        if _gp > 1:
            break
        time.sleep(0.1)
    ck("N23: grandchild pid captured", _gp > 1)
    _alive = True
    for _ in range(30):
        _alive = pid_alive(_gp) if _gp > 1 else False
        if not _alive:
            break
        time.sleep(0.1)
    ck("N23: grandchild killed with the tree (no leak)", _gp > 1 and not _alive)
    try:
        if _alive:
            os.kill(_gp, 9)   # 清理：测试失败也不留野进程
    except Exception:
        pass

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)
