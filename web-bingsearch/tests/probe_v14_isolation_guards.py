"""Acceptance probe for web-bingsearch v1.4 fixes (N2 / N3 / N4 / F17 / F18).

Run with the same interpreter the gateway uses (store python), no network needed:
  * F18  : blocked scheme / private IP / localhost refused in the parent, per-item error
  * N2   : extract runs in a child process; hard wall clock, parent keeps ticking
  * N3   : explicit 0 is honoured (clamped to 1), missing key falls back to default
  * N4   : cachePut under 4 concurrent writers, no exception, size <= 128
  * F17  : zero hits -> success=False + error + debug (allow_empty=True keeps old shape)
"""

import importlib.util
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # plugin dir (this file lives in tests/)
if not os.path.isfile(os.path.join(HERE, "__init__.py")):
    HERE = r"D:\01-AI\Hermes-Agent\Data\plugins\custom-plugins\web-bingsearch"
INIT = os.path.join(HERE, "__init__.py")
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("   | " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


def load(tag):
    spec = importlib.util.spec_from_file_location("bing_probe_" + tag, INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self, settings=None):
        self.s = dict(settings or {})

    def get_config(self, key, default=None):
        return self.s.get(key, default)

    def set_config(self, key, value):
        self.s[key] = value


print("=== [0] load / parent must not import primp ===")
mod = load("a")
check("primp not in sys.modules after plugin import", "primp" not in sys.modules)

print()
print("=== [1] F18: parent-side scheme / private-net guard (no child spawned) ===")
mod._pluginCtx = FakeCtx({"subprocess_isolation": True})
bad = [
    "http://10.0.0.1/",
    "http://127.0.0.1/",
    "file:///etc/passwd",
    "http://localhost:8080/x",
    "http://192.168.1.1/admin",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://0.0.0.0/",
    "https://example.com@10.0.0.1/",
    "gopher://example.com/",
]
t0 = time.monotonic()
rows = mod.runExtractBounded(bad)
dt = time.monotonic() - t0
for r in rows:
    err = r.get("error") or ""
    print("   %-38s -> %s" % (r.get("url"), err[:96]))
check("every blocked url carries error", all(r.get("error") for r in rows))
check("no blocked url got content", all(not r.get("raw_content") for r in rows))
check("blocked batch returned fast (<2s, no spawn)", dt < 2.0, "%.3fs" % dt)
check("keys kept for blocked rows",
      all(set(("url", "title", "content", "raw_content", "error")) <= set(r) for r in rows))
check("userinfo trick (example.com@10.0.0.1) blocked",
      "blocked private/loopback host" in (rows[8].get("error") or ""), rows[8].get("error"))

check("guard allows public https", mod.checkExtractUrlAllowed("https://example.com/x") is None)
check("guard allows public http", mod.checkExtractUrlAllowed("http://93.184.216.34/") is None)
os.environ["BING_ALLOW_PRIVATE_FETCH"] = "1"
check("BING_ALLOW_PRIVATE_FETCH=1 overrides guard",
      mod.checkExtractUrlAllowed("http://127.0.0.1/") is None)
del os.environ["BING_ALLOW_PRIVATE_FETCH"]
check("primp STILL not imported in parent after blocked extract", "primp" not in sys.modules)

print()
print("=== [2] N3: explicit 0 must survive (clamped to 1), missing -> default ===")
cases = [
    (None, 45), (0, 1), (0, 1), ("0", 1), (7, 7), ("12", 12), (None, 45), ("", 45), ("abc", 45), (-5, 1),
]
for key_val, want in cases:
    ctx = FakeCtx({} if key_val is None else {"search_timeout_secs": key_val})
    mod._pluginCtx = ctx
    got = mod.readIntSetting("search_timeout_secs", 45)
    check("readIntSetting(%r) == %r" % (key_val, want), got == want, "got %r" % (got,))
mod._pluginCtx = FakeCtx({"cache_ttl_secs": 0})
check("cache_ttl_secs=0 -> 1", mod.readIntSetting("cache_ttl_secs", 300) == 1)
mod._pluginCtx = FakeCtx({})
check("cache_ttl_secs missing -> 300", mod.readIntSetting("cache_ttl_secs", 300) == 300)
check("extract_timeout_secs default 30", mod.readIntSetting("extract_timeout_secs", 30) == 30)

print()
print("=== [3] N4: cachePut under 4 concurrent writers ===")
modc = load("c")
errs = []


def hammer(idx):
    try:
        for n in range(400):
            modc.cachePut("q-%d-%d" % (idx, n), [{"title": "t", "url": "u", "description": "d"}])
            modc.cacheGet("q-%d-%d" % (idx, n))
    except BaseException as exc:  # noqa: BLE001 - we want the exact failure class
        errs.append("%s: %r" % (type(exc).__name__, exc))


modc._pluginCtx = FakeCtx({"cache_ttl_secs": 300})
ths = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
t0 = time.monotonic()
for t in ths:
    t.start()
for t in ths:
    t.join()
dt = time.monotonic() - t0
check("no exception in 4 concurrent writers", not errs, errs[:2])
check("cache size <= 128", len(modc._resultCache) <= 128, "size=%d" % len(modc._resultCache))
modc.cachePut("survivor", [{"title": "t"}])
check("cache round-trip after hammering", modc.cacheGet("survivor") == [{"title": "t"}], "%.2fs" % dt)

print()
print("=== [4] N2: extract child process, hard timeout, parent stays responsive ===")
osing = os.environ.get("HERMES_BING_ALLOW_TEST_HOOKS")
os.environ["HERMES_BING_ALLOW_TEST_HOOKS"] = "1"
modt = load("t")
modt._testHook = "gil"  # child holds the GIL in a native call for 30s
modt._pluginCtx = FakeCtx({"subprocess_isolation": True, "extract_timeout_secs": 2})
ticker = {"n": 0}
stop = threading.Event()


def tick():
    while not stop.is_set():
        ticker["n"] += 1
        time.sleep(0.05)


th = threading.Thread(target=tick, daemon=True)
th.start()
t0 = time.monotonic()
rows = modt.runExtractBounded(["https://example.com/slow-a", "https://example.com/slow-b"])
dt = time.monotonic() - t0
stop.set()
time.sleep(0.1)
print("   elapsed=%.2fs  parent ticks=%d" % (dt, ticker["n"]))
for r in rows:
    print("   %-34s -> %s" % (r.get("url"), (r.get("error") or "")[:80]))
check("both urls errored, none fatal-batch", len(rows) == 2 and all(r.get("error") for r in rows))
check("timeout message names extract", all("Bing extract timed out after 2s" in (r.get("error") or "") for r in rows))
budget = 2 * 2 + 2  # 2 urls * wall + 2s slack (acceptance)
check("returned within wall+2s budget", dt <= budget, "%.2fs <= %ds" % (dt, budget))
check("parent kept running (>=20 ticks during wait)", ticker["n"] >= 20, "ticks=%d" % ticker["n"])

# provider.extract must route through the same isolated path
prov = modt.BingCrawlerWebSearchProvider()
t0 = time.monotonic()
out = prov.extract(["https://example.com/one"])
dt2 = time.monotonic() - t0
check("provider.extract returns list", isinstance(out, list) and len(out) == 1)
check("provider.extract marks timeout error", "timed out" in (out[0].get("error") or ""), out[0].get("error"))
check("provider.extract within budget", dt2 <= 2 + 2, "%.2fs" % dt2)

# one blocked + one slow: blocked returns instantly, second still bounded
rows = modt.runExtractBounded(["http://10.0.0.1/", "https://example.com/slow-c"])
check("mixed batch: blocked has guard error", "blocked" in (rows[0].get("error") or ""))
check("mixed batch: slow one timed out", "timed out" in (rows[1].get("error") or ""))

print()
print("=== [5] F17: zero hits must not be silent success ===")
mode = load("e")
mode._testHook = "empty"  # child returns {"ok": true, "results": []}
mode._pluginCtx = FakeCtx({"subprocess_isolation": True, "allow_empty": False})
resp = mode.BingCrawlerWebSearchProvider().search("zzzz no such query", limit=5)
print("   resp=%r" % (resp,))
check("zero hits -> success False", resp.get("success") is False)
check("zero hits -> explicit error", "no results parsed" in (resp.get("error") or ""))
check("zero hits -> debug dict", isinstance(resp.get("debug"), dict))

mode._pluginCtx = FakeCtx({"subprocess_isolation": True, "allow_empty": True})
resp2 = mode.BingCrawlerWebSearchProvider().search("zzzz no such query", limit=5)
print("   allow_empty resp=%r" % (resp2,))
check("allow_empty=True keeps success True + empty web",
      resp2.get("success") is True and resp2.get("data", {}).get("web") == [])

print()
print("=== [6] regression: search path still works through the shared runner ===")
check("runHtmlSearchBounded with empty hook -> []",
      mode.runHtmlSearchBounded("x") == [])
mods2 = load("s")
mods2._testHook = "gil"
mods2._pluginCtx = FakeCtx({"subprocess_isolation": True, "search_timeout_secs": 2})
t0 = time.monotonic()
try:
    mods2.runHtmlSearchBounded("x")
    err = None
except Exception as exc:  # noqa: BLE001
    err = "%s: %s" % (type(exc).__name__, exc)
dt3 = time.monotonic() - t0
check("search timeout still raises TimeoutError", isinstance(err, str) and err.startswith("TimeoutError"))
check("search timeout message unchanged", err and "Bing search timed out after 2s" in err, err)
check("search timeout within budget", dt3 <= 2 + 2, "%.2fs" % dt3)

if osing is None:
    del os.environ["HERMES_BING_ALLOW_TEST_HOOKS"]
else:
    os.environ["HERMES_BING_ALLOW_TEST_HOOKS"] = osing

print()
print("=== [7] extract path never imports primp in the parent (this process) ===")
check("'primp' absent from sys.modules at end", "primp" not in sys.modules)

print()
print("=== [8] worker dispatch: op routing, and the child really loads plugin+primp ===")
import json
import subprocess

WORKER = os.path.join(HERE, "_search_worker.py")


def call_worker(req, timeout=90):
    p = subprocess.run(
        [sys.executable, WORKER], input=json.dumps(req), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    try:
        return p.returncode, json.loads((p.stdout or "").strip() or "null"), (p.stdout or "").strip()
    except Exception:
        return p.returncode, None, (p.stdout or "").strip()


code, env, raw = call_worker({"op": "extract", "url": "", "settings": {}})
check("worker: extract with empty url -> ok:false / empty url",
      isinstance(env, dict) and env.get("ok") is False and "empty url" in (env.get("error") or ""), raw[:120])

code, env, raw = call_worker({"op": "bogus", "settings": {}})
check("worker: unknown op rejected", isinstance(env, dict) and "unknown op" in (env.get("error") or ""), raw[:120])

code, env, raw = call_worker({"queue": ""})
check("worker: v1.3 request (no op) still routes to search",
      isinstance(env, dict) and "empty queue" in (env.get("error") or ""), raw[:120])

# loopback:port 1 is closed -> primp fails instantly, no external network. Proves the child
# loads the plugin module + primp and its error comes back as a clean envelope.
t0 = time.monotonic()
code, env, raw = call_worker({"op": "extract", "url": "http://127.0.0.1:1/", "settings": {}})
dt = time.monotonic() - t0
print("   loopback fetch probe: code=%s %.2fs -> %s" % (code, dt, raw[:140]))
check("child loaded plugin+primp, failure surfaced as envelope",
      isinstance(env, dict) and env.get("ok") is False and bool(env.get("error")), raw[:160])
check("child failure is not a raw traceback", "Traceback" not in raw, raw[:160])
check("child plugin+primp load under 10s", dt < 10, "%.2fs" % dt)

print()
print("=== [9] search contract: success shape unchanged, debug plumbed from child ===")
import json as _json


class FakeProc:
    """Minimal Popen stand-in so we can feed runHtmlSearchBounded a canned envelope."""

    def __init__(self, payload):
        self.payload = payload

    def communicate(self, data=None):
        return (self.payload, "")

    def poll(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


modk = load("k")
modk._pluginCtx = FakeCtx({"subprocess_isolation": True, "allow_empty": False})
modk._spawnSearchWorker = lambda env: FakeProc(_json.dumps({
    "ok": True, "results": [],
    "debug": {"tiers_tried": 2, "final_query": "x", "final_url": "https://cn.bing.com/search?q=x&setlang=zh-CN&ensearch=0"},
}))
resp = modk.BingCrawlerWebSearchProvider().search("x", limit=5)
print("   zero-hit resp=%r" % (resp,))
check("debug plumbed from child envelope", (resp.get("debug") or {}).get("tiers_tried") == 2)
check("debug carries final_url", "cn.bing.com" in ((resp.get("debug") or {}).get("final_url") or ""))

modk._spawnSearchWorker = lambda env: FakeProc(_json.dumps({
    "ok": True,
    "results": [{"title": "T1", "url": "https://e.com/1", "description": "D1"}],
    "debug": {},
}))
ok = modk.BingCrawlerWebSearchProvider().search("x", limit=5)
print("   success resp=%r" % (ok,))
check("success shape keeps success/data.web", ok.get("success") is True and isinstance(ok.get("data"), dict))
item = (ok.get("data") or {}).get("web") or [{}]
check("web item keys unchanged", set(item[0]) == set(("title", "url", "description", "position")), sorted(item[0]))
check("position starts at 1", item[0].get("position") == 1)

print()
print("SUMMARY: %d checks failed" % len(FAILS))
if FAILS:
    for f in FAILS:
        print("  FAILED: " + f)
sys.exit(1 if FAILS else 0)
