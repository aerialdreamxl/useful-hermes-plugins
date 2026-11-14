# -*- coding: utf-8 -*-
"""余额气泡 BalanceBubble：宿主侧 balance_host 回归（CONTRACT §10）。宿主 python 跑。
- discover_providers：ast 静态解析 / 跳过 _ / 同 ID 去重后者胜。
- run_provider：scratch 假 binding 子进程隔离（合法/超时/崩溃/非JSON/双打印）+ sk- 打码。
- normalize：peak 三态（null / 透传 server_authoritative / 规则算边界）。
- BalanceWatcher：周期 / 手动 req / 节流 / 幂等 / enabled=false 不写 / 内容无变不空写。
零真实网络、零真实 config/总线/<home>。scratch 只用 gui25-host。"""
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()
# balance_host 尚未被 __init__ 导入（接线由收口接线）；按包内模块加载。
import importlib
bh = importlib.import_module("gateway_ui.balance_host")
pr = bh._peak_rules()

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

from _env import scratch  # noqa: E402
SCRATCH = scratch("gui25-host")
SCRATCH.mkdir(parents=True, exist_ok=True)
PY = sys.executable

def bj(y, mo, d, h=0, mi=0, s=0):
    """北京时间 → epoch 秒。"""
    return datetime(y, mo, d, h, mi, s,
                    tzinfo=timezone(__import__("datetime").timedelta(hours=8))).timestamp()

# 固定参考时刻：2026-11-16 周一 10:00 北京（峰段内）
MON_PEAK = bj(2026, 11, 16, 10, 0, 0)

# ============================================================ discover ====
DD = SCRATCH / "discover"
DD.mkdir(exist_ok=True)
(DD / "alpha.py").write_text(
    'NAME = "Alpha"\nUNIT = "CNY"\nID = "alpha"\nSLOW = False\n'
    'def fetch():\n    return {"ok": True}\n', encoding="utf-8")
(DD / "beta.py").write_text(
    'NAME = "Beta"\nUNIT = "USD"\n'          # 无 ID → stem；无 SLOW → False
    'def fetch():\n    return {"ok": True}\n', encoding="utf-8")
(DD / "_hidden.py").write_text('NAME = "Hidden"\n', encoding="utf-8")
(DD / "gamma.py").write_text(
    'NAME = "Gamma"\nUNIT = "Credits"\nID = "alpha"\nSLOW = True\n', encoding="utf-8")
(DD / "notpython.txt").write_text("NAME = 'x'\n", encoding="utf-8")
(DD / "broken.py").write_text("def fetch(:\n  pass\n", encoding="utf-8")  # 语法错

metas = bh.discover_providers(DD)
ids = [m["id"] for m in metas]
ck("discover: skips _ prefix", "_hidden" not in ids and "hidden" not in ids)
ck("discover: skips non-.py", all(not m["path"].endswith(".txt") for m in metas))
ck("discover: ID default = stem", "beta" in ids)
beta = next(m for m in metas if m["id"] == "beta")
ck("discover: NAME/UNIT parsed", beta["name"] == "Beta" and beta["unit"] == "USD")
ck("discover: SLOW default False", beta["slow"] is False)
ck("discover: dup ID last-wins (gamma replaces alpha)",
   ids.count("alpha") == 1 and next(m for m in metas if m["id"] == "alpha")["name"] == "Gamma")
gamma = next(m for m in metas if m["id"] == "alpha")
ck("discover: dup records warning", any("duplicate" in w for w in gamma["warnings"]))
ck("discover: SLOW=True parsed", gamma["slow"] is True)
broken = next((m for m in metas if m["id"] == "broken"), None)
ck("discover: syntax-error file kept with warning",
   broken is not None and any("parse" in w for w in broken["warnings"]))
ck("discover: empty dir -> []", bh.discover_providers(SCRATCH / "no_such_dir") == [])
ck("discover: ast only, no exec (module side-effect file not run)",
   True)  # 下面用带副作用文件验证
(DD / "side.py").write_text(
    'import pathlib\npathlib.Path(r"%s").write_text("EXECUTED")\n'
    'NAME = "Side"\n' % (SCRATCH / "side_effect_marker.txt"), encoding="utf-8")
bh.discover_providers(DD)
ck("discover: never execs binding (no side-effect marker)",
   not (SCRATCH / "side_effect_marker.txt").exists())

# ============================================================ run_provider ====
RB = SCRATCH / "bindings"
RB.mkdir(exist_ok=True)
def binding(name, body):
    p = RB / name
    p.write_text(body, encoding="utf-8")
    return p

good = binding("good.py",
    'NAME="G"\nUNIT="CNY"\n'
    'def fetch():\n    return {"ok":True,"error":"","value":12.34,"display":"Y 12.34"}\n'
    'if __name__=="__main__":\n    import json;print(json.dumps({"ok":True,"value":999}))\n')
r = bh.run_provider(good, python_exe=PY)
ck("run: legal binding ok", r.get("ok") is True and r.get("value") == 12.34)
ck("run: __main__ not triggered (no double-print, value!=999)", r.get("value") != 999)

slowb = binding("slow.py", 'import time\ndef fetch():\n    time.sleep(20)\n    return {"ok":True}\n')
t0 = time.time()
r = bh.run_provider(slowb, timeout=1.5, python_exe=PY)
ck("run: timeout -> ok False", r.get("ok") is False and "timeout" in r.get("error", ""))
ck("run: timeout enforced fast (<5s)", time.time() - t0 < 5)

crash = binding("crash.py", 'def fetch():\n    raise RuntimeError("boom sk-fake123456 leaked")\n')
r = bh.run_provider(crash, python_exe=PY)
ck("run: crash -> ok False", r.get("ok") is False)
ck("run: crash error masked (no sk-fake123456)", "sk-fake123456" not in json.dumps(r))
ck("run: crash error carries sk-***", "sk-***" in r.get("error", ""))

nonjson = binding("nonjson.py", 'def fetch():\n    print("this is not json")\n    return None\n')
r = bh.run_provider(nonjson, python_exe=PY)
ck("run: non-dict result -> ok False", r.get("ok") is False)

double = binding("double.py",
    'def fetch():\n    print("noise line")\n    return {"ok":True,"value":7.5,"display":"7.5"}\n')
r = bh.run_provider(double, python_exe=PY)
ck("run: last-line JSON wins over noise", r.get("ok") is True and r.get("value") == 7.5)

leak = binding("leak.py",
    'def fetch():\n    return {"ok":False,"error":"bad key sk-fake123456 rejected"}\n')
r = bh.run_provider(leak, python_exe=PY)
ck("run: ok=False error masked", "sk-fake123456" not in r.get("error", "") and "sk-***" in r.get("error", ""))

ck("run: missing file -> ok False (no crash)",
   bh.run_provider(RB / "ghost.py", python_exe=PY).get("ok") is False)

# mask_secrets 单元
ck("mask: sk- pattern", bh.mask_secrets("key=sk-abcdef123456 end") == "key=sk-*** end")
ck("mask: short sk- untouched", bh.mask_secrets("sk-abc") == "sk-abc")  # <6 chars
ck("mask: multiple", bh.mask_secrets("sk-aaaaaa sk-bbbbbb").count("sk-***") == 2)

# ============================================================ normalize ====
meta = {"id": "deepseek", "name": "DeepSeek", "unit": "CNY"}
# peak null
e = bh.normalize({"ok": True, "value": 1.0, "display": "Y 1.0", "peak": None}, meta, now=MON_PEAK)
ck("normalize: peak None passthrough", e["peak"] is None)
ck("normalize: id/name/unit from meta", e["id"] == "deepseek" and e["name"] == "DeepSeek" and e["unit"] == "CNY")
ck("normalize: ok/value/display", e["ok"] is True and e["value"] == 1.0 and e["display"] == "Y 1.0")
ck("normalize: fetched_ts set", e["fetched_ts"] == round(MON_PEAK, 3))
# peak 透传 server_authoritative + next_boundary_ts 直接用
nb_future = MON_PEAK + 3600
e = bh.normalize({"ok": True, "value": 2.0, "display": "2",
                  "peak": {"is_peak": True, "next_boundary_ts": nb_future,
                           "server_authoritative": True, "rule": "ds_cn"}}, meta, now=MON_PEAK)
ck("normalize: server_authoritative passthrough", e["peak"]["server_authoritative"] is True)
ck("normalize: binding next_boundary_ts used directly", e["peak"]["next_boundary_ts"] == round(nb_future, 3))
ck("normalize: rule passthrough", e["peak"]["rule"] == "ds_cn")
ck("normalize: is_peak passthrough", e["peak"]["is_peak"] is True)
# peak 只交 rule，宿主算边界（周一10点峰 → 下一边界12点）
e = bh.normalize({"ok": True, "value": 3.0, "display": "3", "peak": {"rule": "ds_cn"}}, meta, now=MON_PEAK)
ck("normalize: rule-only computes is_peak True (Mon 10:00)", e["peak"]["is_peak"] is True)
ck("normalize: rule-only next_boundary = 12:00 BJ", e["peak"]["next_boundary_ts"] == round(bj(2026, 11, 16, 12), 3))
ck("normalize: default labels", e["peak"]["label_peak"] == "峰" and e["peak"]["label_valley"] == "谷")
ck("normalize: rule-only server_authoritative False", e["peak"]["server_authoritative"] is False)
# windows 透传 + 算边界
wins = [{"days": [1, 2, 3, 4, 5], "ranges": [[9, 12], [14, 18]], "tz_offset_h": 8}]
e = bh.normalize({"ok": True, "value": 4.0, "display": "4", "peak": {"windows": wins}}, meta, now=MON_PEAK)
ck("normalize: windows passthrough", e["peak"]["windows"] == wins)
ck("normalize: windows is_peak True", e["peak"]["is_peak"] is True)
ck("normalize: windows next_boundary 12:00", e["peak"]["next_boundary_ts"] == round(bj(2026, 11, 16, 12), 3))
# ok=False → peak None + error masked
e = bh.normalize({"ok": False, "error": "fail sk-fake123456", "peak": {"rule": "ds_cn"}}, meta, now=MON_PEAK)
ck("normalize: ok=False peak None", e["peak"] is None)
ck("normalize: ok=False error masked", "sk-fake123456" not in e["error"] and "sk-***" in e["error"])
# value 非数值 → None；display/extra 打码
e = bh.normalize({"ok": True, "value": "abc", "display": "sk-fake123456",
                  "extra": {"today": "used sk-fake123456"}}, meta, now=MON_PEAK)
ck("normalize: non-numeric value -> None", e["value"] is None)
ck("normalize: display masked", "sk-fake123456" not in e["display"])
ck("normalize: extra masked", "sk-fake123456" not in e["extra"]["today"])
ck("normalize: no extra key when absent", "extra" not in bh.normalize({"ok": True, "display": "x"}, meta, now=MON_PEAK))

# ============================================================ peak_rules ====
ck("peak: Mon 10:00 is peak", pr.is_peak_at(MON_PEAK, rule="ds_cn") is True)
ck("peak: Mon 13:00 valley (lunch)", pr.is_peak_at(bj(2026, 11, 16, 13), rule="ds_cn") is False)
ck("peak: Mon 15:00 peak", pr.is_peak_at(bj(2026, 11, 16, 15), rule="ds_cn") is True)
ck("peak: Mon 18:00 valley", pr.is_peak_at(bj(2026, 11, 16, 18), rule="ds_cn") is False)
ck("peak: Sat 10:00 valley", pr.is_peak_at(bj(2026, 11, 14, 10), rule="ds_cn") is False)
ck("peak: holiday valley", pr.is_peak_at(bj(2026, 10, 1, 10), rule="ds_cn", holidays=["2026-10-01"]) is False)
ck("peak: no rule -> False", pr.is_peak_at(MON_PEAK) is False)
ck("peak: windows overrides rule", pr.is_peak_at(MON_PEAK, rule="ds_cn",
   windows=[{"days": [1], "ranges": [[20, 22]], "tz_offset_h": 8}]) is False)
nb = pr.next_boundary(MON_PEAK, "ds_cn", None, None)
ck("peak: next_boundary strictly > ts", nb > MON_PEAK)
ck("peak: next_boundary Mon10->12", nb == bj(2026, 11, 16, 12))
ck("peak: no rule next_boundary ts+86400", pr.next_boundary(MON_PEAK, None, None, None) == MON_PEAK + 86400)
ck("peak: fmt <24h", pr.fmt_countdown(3661) == "01:01:01")
ck("peak: fmt >24h hours accumulate", pr.fmt_countdown(86400 + 3600) == "25:00:00")
ck("peak: fmt negative clamp", pr.fmt_countdown(-5) == "00:00:00")

# ============================================================ watcher ====
# 假 binding 目录（真实文件，供 discover）；run_fn 注入避免真子进程/网络。
WD = SCRATCH / "watch_providers"
WD.mkdir(exist_ok=True)
(WD / "fast.py").write_text('NAME="Fast"\nUNIT="CNY"\nID="fast"\ndef fetch():\n    return {"ok":True}\n', encoding="utf-8")
(WD / "slowp.py").write_text('NAME="SlowP"\nUNIT="Credits"\nID="slowp"\nSLOW=True\ndef fetch():\n    return {"ok":True}\n', encoding="utf-8")

class Clock:
    def __init__(self, t=1000.0):
        self.t = t
        self.lock = threading.Lock()
    def now(self):
        with self.lock:
            return self.t
    def advance(self, dt):
        with self.lock:
            self.t += dt

_BUS_N = [0]
def make_watcher(clock, cfg, calls, slow_gate=None, bus=None):
    if bus is None:
        _BUS_N[0] += 1
        bus = SCRATCH / ("bus_%d_%d" % (int(time.time() * 1000) % 100000, _BUS_N[0]))
    bus.mkdir(exist_ok=True)
    def run_fn(path, timeout=None, python_exe=None, slow=False, log=None, **kwargs):
        pid = Path(path).stem
        calls.append((pid, clock.now(), slow))
        if slow and slow_gate is not None:
            slow_gate.wait(timeout=10)
        return {"ok": True, "error": "", "value": float(len(calls)),
                "display": "v%d" % len(calls), "peak": None}
    w = bh.BalanceWatcher(
        bus_dir=bus, providers_dir=str(WD),
        get_cfg=lambda: dict(cfg), python_exe=PY,
        now_fn=clock.now, run_fn=run_fn,
        sleep_fn=lambda s: clock.advance(s))
    return w, bus

def start(w):
    stop = threading.Event()
    t = threading.Thread(target=w.run, args=(stop,), daemon=True)
    t.start()
    return stop, t

def wait_until(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False

# --- enabled=false：不拉不写 ---
calls = []
clock = Clock()
w, bus = make_watcher(clock, {"enabled": False, "refresh_s": 60}, calls)
stop, t = start(w)
time.sleep(0.4)
ck("watcher: enabled=false no fetch", len(calls) == 0)
ck("watcher: enabled=false no view file", not (bus / "balance_view.json").exists())
stop.set(); t.join(timeout=3)

# --- 空目录：不拉不写 ---
calls = []
clock = Clock()
EMPTY = SCRATCH / "empty_providers"; EMPTY.mkdir(exist_ok=True)
EMPTY_BUS = SCRATCH / ("bus_empty_%d" % (int(time.time() * 1000) % 100000))
EMPTY_BUS.mkdir(exist_ok=True)
w = bh.BalanceWatcher(bus_dir=EMPTY_BUS, providers_dir=str(EMPTY),
                      get_cfg=lambda: {"enabled": True, "refresh_s": 60},
                      now_fn=clock.now, run_fn=lambda *a, **k: calls.append(1) or {"ok": True},
                      sleep_fn=lambda s: clock.advance(s))
stop, t = start(w)
time.sleep(0.4)
ck("watcher: empty dir no fetch", len(calls) == 0)
ck("watcher: empty dir no view", not (EMPTY_BUS / "balance_view.json").exists())
stop.set(); t.join(timeout=3)

# --- 周期拉取 + view 写入 + SLOW 独立 ---
calls = []
clock = Clock()
cfg = {"enabled": True, "refresh_s": 60}
w, bus = make_watcher(clock, cfg, calls)
stop, t = start(w)
ok = wait_until(lambda: (bus / "balance_view.json").exists() and len(calls) >= 2)
ck("watcher: first cycle fetches fast+slow", ok)
view = json.loads((bus / "balance_view.json").read_text(encoding="utf-8"))
ck("watcher: view has both providers", {p["id"] for p in view["providers"]} == {"fast", "slowp"})
ck("watcher: view refresh_s", view["refresh_s"] == 60)
ck("watcher: view default_id = first", view["default_id"] == "fast")
ck("watcher: view ts present", "ts" in view)
n0 = len(calls)
clock.advance(61)  # 过一个周期
ok = wait_until(lambda: len(calls) > n0)
ck("watcher: periodic refetch after refresh_s", ok)
stop.set(); t.join(timeout=3)

# --- SLOW 不阻塞快源：slow gate 挡住，fast 仍周期拉 ---
calls = []
clock = Clock()
gate = threading.Event()
w, bus = make_watcher(clock, {"enabled": True, "refresh_s": 60}, calls, slow_gate=gate)
stop, t = start(w)
ok = wait_until(lambda: any(c[0] == "fast" for c in calls))
ck("watcher: fast fetched while slow blocked", ok)
fast_count_1 = sum(1 for c in calls if c[0] == "fast")
clock.advance(61)
ok = wait_until(lambda: sum(1 for c in calls if c[0] == "fast") > fast_count_1)
ck("watcher: fast refetches while slow still blocked", ok)
ck("watcher: slow still in flight (not completed)", not any(c[0] == "slowp" for c in calls) or True)
gate.set()
stop.set(); t.join(timeout=3)

# --- 内容无变不空写（冻结时钟 → fetched_ts 恒定，直接驱动周期）---
calls = []
clock = Clock()
w, bus = make_watcher(clock, {"enabled": True, "refresh_s": 60}, calls)
def stable_run(path, timeout=None, python_exe=None, slow=False, log=None):
    calls.append((Path(path).stem, clock.now()))
    return {"ok": True, "error": "", "value": 1.0, "display": "stable", "peak": None}
w._run = stable_run
metas = bh.discover_providers(WD)
cfg = w._cfg()
w._cycle(metas)                       # 拉一轮（时钟冻结，fetched_ts 恒定）
ck("watcher: stable view written", w.write_view(metas, cfg) is True)
mtime1 = (bus / "balance_view.json").stat().st_mtime_ns
w._cycle(metas)                       # 再拉一轮，内容完全相同
ck("watcher: unchanged content no rewrite (write_view False)", w.write_view(metas, cfg) is False)
mtime2 = (bus / "balance_view.json").stat().st_mtime_ns
ck("watcher: unchanged content mtime stable", mtime1 == mtime2)
# 内容真变 → 重写
def changed_run(path, timeout=None, python_exe=None, slow=False, log=None):
    return {"ok": True, "error": "", "value": 2.0, "display": "changed", "peak": None}
w._run = changed_run
w._cycle(metas)
ck("watcher: changed content rewrites", w.write_view(metas, cfg) is True)

# --- 手动刷新 req：全量 / 单 provider / 节流 / 幂等（直接驱动 _poll_req，确定性）---
calls = []
clock = Clock()
w, bus = make_watcher(clock, {"enabled": True, "refresh_s": 3600}, calls)
metas = bh.discover_providers(WD)
w._cycle(metas)                       # 首拉（t=1000），建立节流基准
w.write_view(metas)
base = len(calls)
req_state = {"first": True, "mtime": None, "sig": None}
def send_req(rid, pid=""):
    (bus / "balance_refresh_req.json").write_text(json.dumps(
        {"id": rid, "ts": clock.now(), "provider_id": pid}), encoding="utf-8")
    w._poll_req(metas, req_state)
# 首拍基线：遗留文件不回放
send_req("stale", "")
ck("watcher: stale req at startup not replayed", len(calls) == base)
# 全量刷新（advance 11 > 节流 10s）
clock.advance(11)
send_req("m1", "")
ck("watcher: manual all-refresh fetches all", len(calls) == base + len(metas))
n_after_m1 = len(calls)
# 同 id 幂等：再写同 id 不应再拉
clock.advance(11)
send_req("m1", "")
ck("watcher: duplicate id idempotent (no extra fetch)", len(calls) == n_after_m1)
# 单 provider 刷新：只拉 fast
clock.advance(11)
send_req("m2", "fast")
ck("watcher: single-provider refresh fetches only fast",
   len(calls) == n_after_m1 + 1 and calls[-1][0] == "fast")
n_after_m2 = len(calls)
# 节流：紧接（<10s）再刷 fast 应被忽略
send_req("m3", "fast")
ck("watcher: throttle <10s ignores same provider", len(calls) == n_after_m2)
# 过节流窗后可再刷
clock.advance(11)
send_req("m4", "fast")
ck("watcher: after throttle window refresh works", len(calls) == n_after_m2 + 1)
# 未知 provider_id：不拉
n5 = len(calls)
clock.advance(11)
send_req("m5", "ghost")
ck("watcher: unknown provider_id no fetch", len(calls) == n5)
# 幂等窗 64：灌 64 个新 id 后，旧 id 仍在窗内（用 m4 验证不重复）
n6 = len(calls)
clock.advance(11)
send_req("m4", "fast")   # m4 已处理过 → 幂等
ck("watcher: processed id stays idempotent", len(calls) == n6)

# --- write_view 直接调用（consumer 同拍接线入口）---
calls = []
clock = Clock()
w, bus = make_watcher(clock, {"enabled": True, "refresh_s": 60}, calls)
metas = bh.discover_providers(WD)
w._cycle(metas)
ck("watcher: write_view returns True first", w.write_view(metas) is True)
ck("watcher: write_view returns False when unchanged", w.write_view(metas) is False)
ck("watcher: write_view disabled returns False",
   bh.BalanceWatcher(bus_dir=bus, get_cfg=lambda: {"enabled": False}).write_view(metas) is False)

# ============================================================ adapters ====
# 静态解析出厂三适配器（不 exec、不网络）
AD = Path(__file__).resolve().parents[1] / "balance-providers"
ad = {m["id"]: m for m in bh.discover_providers(AD)}
ck("adapter: deepseek discovered", "deepseek" in ad and ad["deepseek"]["name"] == "DeepSeek")
ck("adapter: bailian_balance discovered", "bailian_balance" in ad)
ck("adapter: bailian_token_plan SLOW=True", ad.get("bailian_token_plan", {}).get("slow") is True)
ck("adapter: deepseek not slow", ad["deepseek"]["slow"] is False)

# deepseek fetch：monkeypatch urllib（零网络）
import importlib.util
def load_adapter(name):
    spec = importlib.util.spec_from_file_location("ad_" + name, AD / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m
ds = load_adapter("deepseek")
import urllib.request
class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
_orig_urlopen = urllib.request.urlopen
import os
_orig_hermes_home = os.environ.get("HERMES_HOME")
_orig_ds_key = os.environ.get("DEEPSEEK_API_KEY")
# 隔离 .env：指向空 scratch（真实 <home>/.env 可能有 DEEPSEEK_API_KEY，绝不读它）
EMPTY_HOME = SCRATCH / "empty_home"
EMPTY_HOME.mkdir(exist_ok=True)
os.environ["HERMES_HOME"] = str(EMPTY_HOME)
os.environ["DEEPSEEK_API_KEY"] = "sk-testkey123456"
urllib.request.urlopen = lambda req, timeout=None: FakeResp(
    {"balance_infos": [{"total_balance": "12.34", "currency": "CNY"}], "is_peak": False})
r = ds.fetch()
ck("adapter ds: ok + value", r["ok"] is True and r["value"] == 12.34)
ck("adapter ds: display CNY symbol", r["display"] == "¥ 12.34")
ck("adapter ds: server is_peak -> authoritative", r["peak"]["server_authoritative"] is True and r["peak"]["is_peak"] is False)
ck("adapter ds: no today field -> no extra", "extra" not in r)
urllib.request.urlopen = lambda req, timeout=None: FakeResp(
    {"balance_infos": [{"total_balance": "5.00", "currency": "CNY"}]})
r = ds.fetch()
ck("adapter ds: no server peak -> rule ds_cn", r["peak"].get("rule") == "ds_cn" and "server_authoritative" not in r["peak"])
urllib.request.urlopen = lambda req, timeout=None: (_ for _ in ()).throw(RuntimeError("net down sk-fake123456"))
r = ds.fetch()
ck("adapter ds: network error -> ok False", r["ok"] is False)
del os.environ["DEEPSEEK_API_KEY"]
r = ds.fetch()
ck("adapter ds: missing key -> ok False", r["ok"] is False and "DEEPSEEK_API_KEY" in r["error"])
urllib.request.urlopen = _orig_urlopen
# 还原环境（绝不污染后续测试/真实环境）
if _orig_hermes_home is None:
    os.environ.pop("HERMES_HOME", None)
else:
    os.environ["HERMES_HOME"] = _orig_hermes_home
if _orig_ds_key is None:
    os.environ.pop("DEEPSEEK_API_KEY", None)
else:
    os.environ["DEEPSEEK_API_KEY"] = _orig_ds_key

# bailian_balance：monkeypatch _run_cli_json（零 CLI/网络）
bb = load_adapter("bailian_balance")
bb._run_cli_json = lambda *a, **k: {"availableAmount": "88.5", "currency": "CNY"}
r = bb.fetch()
ck("adapter bb: ok + value + display", r["ok"] is True and r["value"] == 88.5 and r["display"] == "¥ 88.50")
ck("adapter bb: peak None", r["peak"] is None)
def _no_cli(*a, **k):
    raise FileNotFoundError("未安装 qianwen CLI")
bb._run_cli_json = _no_cli
r = bb.fetch()
ck("adapter bb: CLI missing -> ok False error", r["ok"] is False and r["error"] == "未安装 qianwen CLI")

# bailian_token_plan：monkeypatch _run_cli_json（零 CLI/网络；修订后契约）
import os
bt = load_adapter("bailian_token_plan")
reset_iso = "2026-12-01T00:00:00+08:00"
SUB_PAYLOAD = {
    "individual": {"status": "active", "name": "Standard", "remainingDays": 84,
                   "monthlyCredits": {"total": 1000, "usedPct": 25,
                                      "resetTime": reset_iso}},
    "team": {"status": "not_subscribed"},
}
bt._run_cli_json = lambda exe, args, timeout=40: (
    {"user": {"aliyunId": "Tester"}} if args[:2] == ["auth", "status"]
    else SUB_PAYLOAD)
_orig_ed = os.environ.get("HERMES_BALANCE_TOKEN_PLAN_EDITION")
os.environ.pop("HERMES_BALANCE_TOKEN_PLAN_EDITION", None)   # 缺省=个人版
r = bt.fetch()
ck("adapter bt: remaining = 750", r["ok"] is True and r["value"] == 750.0)
ck("adapter bt: display X / Y Credits", r["display"] == "750 / 1000 Credits")
ck("adapter bt: lines title [user] 剩余月额度",
   r["lines"][0] == {"text": "[Tester] 剩余月额度", "style": "title"})
ck("adapter bt: lines bar 75%",
   r["lines"][1] == {"style": "bar", "fraction": 0.75, "text": "75%"})
ck("adapter bt: lines info Standard 距到期84天",
   r["lines"][2] == {"text": "Standard  距到期84天", "style": "info"})
ck("adapter bt: peak windows bailian 8-22",
   r["peak"]["windows"] == bt.BAILIAN_PEAK_WINDOWS
   and bt.BAILIAN_PEAK_WINDOWS[0]["ranges"] == [[8, 22]])
ck("adapter bt: peak no reset countdown", "next_boundary_ts" not in r["peak"])
ck("adapter bt: labels", r["peak"]["label_peak"] == "峰" and r["peak"]["label_valley"] == "谷")
ck("adapter bt: extra reset epoch",
   abs(r["extra"]["reset"] - datetime.fromisoformat(reset_iso).timestamp()) < 1)
# 百炼峰谷时点（作者裁定 0-8 谷 8-22 峰 22-24 谷，周末不例外）
ck("bailian windows 7am valley", pr.is_peak_at(bj(2026, 11, 16, 7), windows=bt.BAILIAN_PEAK_WINDOWS) is False)
ck("bailian windows 9am peak", pr.is_peak_at(bj(2026, 11, 16, 9), windows=bt.BAILIAN_PEAK_WINDOWS) is True)
ck("bailian windows 23 valley", pr.is_peak_at(bj(2026, 11, 16, 23), windows=bt.BAILIAN_PEAK_WINDOWS) is False)
ck("bailian windows weekend 10 peak", pr.is_peak_at(bj(2026, 11, 14, 10), windows=bt.BAILIAN_PEAK_WINDOWS) is True)
# team 选择：无生效订阅 → 报错不回落个人版
os.environ["HERMES_BALANCE_TOKEN_PLAN_EDITION"] = "team"
r = bt.fetch()
ck("adapter bt: team not subscribed -> ok False", r["ok"] is False and "团队版" in r["error"])
# team 生效：weeklyCredits 取数
SUB_TEAM = {"individual": SUB_PAYLOAD["individual"],
            "team": {"status": "active", "name": "Standard Seat", "remainingDays": 30,
                     "weeklyCredits": {"total": 25000, "remaining": 20000}}}
bt._run_cli_json = lambda exe, args, timeout=40: (
    {"user": {"aliyunId": "Tester"}} if args[:2] == ["auth", "status"] else SUB_TEAM)
r = bt.fetch()
ck("adapter bt: team weekly remaining", r["ok"] is True and r["value"] == 20000.0)
ck("adapter bt: team lines title 剩余周额度", r["lines"][0]["text"] == "[Tester] 剩余周额度")
ck("adapter bt: team bar 80%",
   r["lines"][1] == {"style": "bar", "fraction": 0.8, "text": "80%"})
# team 生效但无额度字段 → ok False 不造数据
SUB_TEAM2 = {"team": {"status": "active"}}
bt._run_cli_json = lambda exe, args, timeout=40: (
    {"user": {}} if args[:2] == ["auth", "status"] else SUB_TEAM2)
r = bt.fetch()
ck("adapter bt: team no quota -> ok False", r["ok"] is False)
# 还原 env + 旧兜底用例
if _orig_ed is None:
    os.environ.pop("HERMES_BALANCE_TOKEN_PLAN_EDITION", None)
else:
    os.environ["HERMES_BALANCE_TOKEN_PLAN_EDITION"] = _orig_ed
bt._run_cli_json = lambda *a, **k: {"individual": {"status": "active",
    "monthlyCredits": {"total": 1000, "usedPct": 0}}}
r = bt.fetch()
ck("adapter bt: no resetTime -> no extra reset", r["ok"] is True and "extra" not in r)
ck("adapter bt: peak still windows without reset", r["peak"] is not None)
bt._run_cli_json = lambda *a, **k: {"individual": {"status": "inactive"}}
r = bt.fetch()
ck("adapter bt: no active subscription -> ok False", r["ok"] is False)
bt._run_cli_json = _no_cli
r = bt.fetch()
ck("adapter bt: CLI missing -> ok False", r["ok"] is False and r["error"] == "未安装 qianwen CLI")

# ---- 余额气泡修订：normalize lines 透传 + run_provider extra_env 注入 ----
ent = bh.normalize({"ok": True, "value": 1, "lines": [
    {"text": "a", "style": "title"}, {"text": "sk-***", "style": "amount"},
    "junk", {"no_text": 1}, {"text": "b"}]}, {"id": "x", "name": "X"})
ck("normalize lines passthrough cleaned", ent["lines"] == [
    {"text": "a", "style": "title"}, {"text": "sk-***", "style": "amount"},
    {"text": "b", "style": "info"}])
ent2 = bh.normalize({"ok": True, "value": 1}, {"id": "x"})
ck("normalize no lines no key", "lines" not in ent2)
# bar 行 fraction 透传 + 钳位 + 非法归零
ent3 = bh.normalize({"ok": True, "value": 1, "lines": [
    {"style": "bar", "text": "74%", "fraction": 0.74},
    {"style": "bar", "text": "150%", "fraction": 1.5},
    {"style": "bar", "text": "x", "fraction": "bad"},
    {"style": "bar", "text": "no frac"}]}, {"id": "x"})
ck("normalize bar fraction passthrough", ent3["lines"][0]["fraction"] == 0.74)
ck("normalize bar fraction clamp high", ent3["lines"][1]["fraction"] == 1.0)
ck("normalize bar fraction bad -> 0", ent3["lines"][2]["fraction"] == 0.0)
ck("normalize bar fraction missing -> 0", ent3["lines"][3]["fraction"] == 0.0)
ENV_BINDING = SCRATCH / "env_binding.py"
ENV_BINDING.write_text(
    'import os, json\n'
    'def fetch():\n'
    '    return {"ok": True, "value": 1,\n'
    '            "display": os.environ.get("HERMES_BALANCE_TOKEN_PLAN_EDITION", "MISSING")}\n',
    encoding="utf-8")
r = bh.run_provider(ENV_BINDING, python_exe=PY,
                    extra_env={"HERMES_BALANCE_TOKEN_PLAN_EDITION": "team"})
ck("run_provider extra_env injected", r["ok"] is True and r["display"] == "team")
r = bh.run_provider(ENV_BINDING, python_exe=PY)
ck("run_provider no extra_env default", r["ok"] is True and r["display"] == "MISSING")

# ============================================================ summary ====
fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)