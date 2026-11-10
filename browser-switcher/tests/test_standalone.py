"""browser-switcher standalone test — fake ctx + monkeypatched network/daemon seams.

Run with the Hermes runtime interpreter. Exercises:
  1. register() end-to-end (command + tool + middleware all land)
  2. _switch validation branches (unknown provider, missing arg, shardx offline, token missing)
  3. shardx happy path (health/find/start) -> env written, daemons restarted, attached recorded
  4. cdp happy path (ws passthrough, http discovery)
  5. drift middleware: match -> no-op; mismatch -> re-apply; failure -> cooldown
  6. switch back to default -> env cleared, shardx profile stopped
"""
import importlib.util
import json
import os
import sys
import time
import types

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("bsw_test_pkg", os.path.join(SRC, "__init__.py"),
                                              submodule_search_locations=[SRC])
bsw = importlib.util.module_from_spec(spec)
sys.modules["bsw_test_pkg"] = bsw
spec.loader.exec_module(bsw)

# ── fake hermes modules so the plugin's lazy imports resolve ────────────
class _FakeScope(types.ModuleType):
    vals = {}
scope_mod = types.ModuleType("agent.secret_scope")
scope_mod.get_secret = lambda name, default=None: _FakeScope.vals.get(name, default)
agent_pkg = types.ModuleType("agent"); agent_pkg.__path__ = []
agent_pkg.secret_scope = scope_mod
sys.modules.setdefault("agent", agent_pkg)
sys.modules["agent.secret_scope"] = scope_mod

calls = {"harness_stops": 0, "ab_closes": 0}
bucli = types.ModuleType("tools.browser_use_cli")
def _stop(): calls["harness_stops"] += 1; calls.setdefault("last_stop_at", time.monotonic())
bucli.stop_harness_daemons = _stop
life = types.ModuleType("tools.browser_tool_lifecycle")
life.cleanup_all_browsers = lambda: calls.__setitem__("ab_closes", calls["ab_closes"] + 1)
tools_pkg = types.ModuleType("tools"); tools_pkg.__path__ = []
sys.modules.setdefault("tools", tools_pkg)
sys.modules["tools.browser_use_cli"] = bucli
sys.modules["tools.browser_tool_lifecycle"] = life

gw = types.ModuleType("gateway"); gw.__path__ = []
sess = types.ModuleType("gateway.session_context")
SESSION = {"HERMES_SESSION_KEY": "", "HERMES_SESSION_ID": "", "HERMES_UI_SESSION_ID": ""}
sess.get_session_env = lambda name, default="": SESSION.get(name, default)
gw.session_context = sess
sys.modules["gateway"] = gw
sys.modules["gateway.session_context"] = sess
appr = types.ModuleType("tools.approval")
appr.get_current_session_key = lambda default="": default
sys.modules["tools.approval"] = appr

FAKE_WS = "ws://127.0.0.1:54321/devtools/browser/abc-123"
state = {"health_ok": True, "profiles": [{"id": "p1", "name": "FeatherBrowser", "running": False}],
         "started": [], "stopped": []}
bsw._shardx_call = lambda method, path, body=None, timeout=35.0: _fake_shardx(method, path, body)
def _fake_shardx(method, path, body):
    if path == "/health":
        if not state["health_ok"]: raise RuntimeError("不可达")
        return {"ok": True, "name": "shardx-launcher"}
    if not state["health_ok"]:
        raise RuntimeError("ShardX API 不可达（模拟 launcher 离线）")
    if path == "/profiles":
        return {"data": state["profiles"]}
    if path.endswith("/start"):
        state["started"].append((path, body))
        # 真实形态（launcher 2.0.3 实测）：cdp 是对象
        return {"cdp": {"port": 56816, "http_url": "http://127.0.0.1:56816",
                        "web_socket_debugger_url": FAKE_WS}}
    if path.endswith("/stop"):
        state["stopped"].append(path)
        return {"ok": True}
    raise AssertionError("unexpected path " + path)
bsw._discover_cdp_ws_orig = bsw._discover_cdp_ws
def _fake_discover(url):
    if url.startswith("ws"): return url
    if url == "http://good": return "ws://good/devtools/browser/x"
    raise RuntimeError("模拟发现失败")
bsw._discover_cdp_ws = _fake_discover

failures = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <- " + str(detail)))
    if not cond: failures.append(name)

# 1. register
class FakeCtx:
    def __init__(self): self.cmd = None; self.tool = None; self.mw = []
    def register_command(self, name, handler, description="", args_hint=""): self.cmd = (name, handler)
    def register_tool(self, name, toolset, schema, handler, **kw): self.tool = (name, schema, handler)
    def register_middleware(self, kind, cb): self.mw.append((kind, cb))
ctx = FakeCtx()
bsw.register(ctx)
check("register wires all three surfaces", ctx.cmd and ctx.tool and ctx.mw == [("tool_request", bsw._middleware)])
check("slash command name", ctx.cmd[0] == "browserswitch")
check("tool schema enum", set(ctx.tool[1]["parameters"]["properties"]["provider"]["enum"]) == {"default", "shardx", "cdp"})

# 2. validation branches
SESSION.update({"HERMES_SESSION_KEY": "sess-A", "HERMES_SESSION_ID": "rt-A"})
_FakeScope.vals = {"SHARDX_API_KEY": "jwt", "SHARDX_BROWSER_NAME": "FeatherBrowser"}
r = bsw._switch("nope")
check("unknown provider rejected", "未知后端" in r and bsw._attached is None, r)
r = bsw._switch("cdp", "")
check("cdp without url rejected", "需要端点" in r, r)
state["health_ok"] = False
r = bsw._switch("shardx")
check("shardx offline rejected", "不在线" in r, r)
state["health_ok"] = True
_FakeScope.vals = {}
r = bsw._switch("shardx", "FeatherBrowser")
check("missing SHARDX_API_KEY rejected", "SHARDX_API_KEY" in r, r)
_FakeScope.vals = {"SHARDX_API_KEY": "jwt", "SHARDX_BROWSER_NAME": "FeatherBrowser"}

# 3. shardx happy path
r = bsw._switch("shardx")
check("shardx switch ok", "已挂载 ShardX" in r and "launcher 在线" in r, r)
check("env written", os.environ.get("BROWSER_CDP_URL") == FAKE_WS)
check("daemons restarted", calls["harness_stops"] == 1 and calls["ab_closes"] == 1, calls)
check("attached recorded", bsw._attached and bsw._attached["provider"] == "shardx" and bsw._attached["extra"] == "FeatherBrowser")
check("desired keyed by session", "sess-A" in bsw._desired and "rt-A" in bsw._desired)
# headless:true passed to start API
check("start body headless", state["started"] and state["started"][0][1] == {"headless": True}, state["started"])

# 4. drift middleware
bsw._middleware(tool_name="terminal", task_id="rt-A")  # non-browser tool ignored
check("middleware ignores non-browser tools", calls["harness_stops"] == 1)
bsw._middleware(tool_name="browser_navigate", task_id="rt-A", session_id="sess-A")
check("matched session -> no reload", calls["harness_stops"] == 1, calls)
# foreign session with different desired attaches
SESSION.update({"HERMES_SESSION_KEY": "sess-B", "HERMES_SESSION_ID": "rt-B"})
r = bsw._switch("cdp", "ws://127.0.0.1:9333/devtools/browser/z")
check("cdp ws passthrough", "已挂载外部 CDP" in r and os.environ["BROWSER_CDP_URL"].endswith("/devtools/browser/z"))
check("shardx profile stopped on swap", state["stopped"] == ["/profiles/p1/stop"], state["stopped"])
# session A still desires shardx -> drift fires on its next browser call
bsw._middleware(tool_name="browser_navigate", task_id="rt-A", session_id="sess-A")
check("drift re-applies for session A", bsw._attached and bsw._attached["provider"] == "shardx",
      bsw._attached)
check("drift restarted daemons", calls["harness_stops"] >= 2 and calls["ab_closes"] >= 2, calls)

# 5. failure -> cooldown
bsw._switch("default")
state["health_ok"] = False  # shardx now unreachable; session A desired still shardx
bsw._middleware(tool_name="browser_navigate", task_id="rt-A", session_id="sess-A")
check("failed re-apply leaves process at default", bsw._attached is None)
n = calls["harness_stops"]
bsw._middleware(tool_name="browser_navigate", task_id="rt-A", session_id="sess-A")
check("cooldown suppresses hammering", calls["harness_stops"] == n, calls)
state["health_ok"] = True
bsw._last_fail.clear()
bsw._middleware(tool_name="browser_navigate", task_id="rt-A", session_id="sess-A")
check("after cooldown recovery, re-apply works", bsw._attached and bsw._attached["provider"] == "shardx")

# 6. back to default
SESSION.update({"HERMES_SESSION_KEY": "sess-A"})
r = bsw._switch("default")
check("default clears env", "BROWSER_CDP_URL" not in os.environ, os.environ.get("BROWSER_CDP_URL"))
check("default resets attached", bsw._attached is None)
check("switch-back reports default line", "切回" in r, r)
r = bsw._command_handler("")
check("bare /browserswitch prints status+usage", "当前会话后端" in r and "用法" in r, r)
r = bsw._tool_handler({"provider": "shardx", "headless": False})
check("tool handler json envelope", isinstance(json.loads(r), dict) and "result" in json.loads(r))

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(0 if not failures else 1)
