# -*- coding: utf-8 -*-
"""宿主总装冒烟：fake gateway ctx 走完整 register()，验证 hook 接线、
播种对账、门控、shutdown。只用 scratch 总线，绝不碰真实总线/config。宿主 python 跑。"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

from _env import scratch  # noqa: E402
SCRATCH = scratch("gui24-events-register")
SCRATCH.mkdir(parents=True, exist_ok=True)
BUS = SCRATCH / "cache" / "gateway-ui"
BUS.mkdir(parents=True, exist_ok=True)

# 隔离：_home 指向 scratch；Supervisor 换 fake（不 spawn 真进程）
gui._home = lambda: SCRATCH


class FakeSup:
    last = None

    def __init__(self, **kw):
        FakeSup.last = self
        self.kw = kw
        self.started = False

    def run(self):
        self.started = True

    def shutdown(self):
        self.stopped = True


gui.Supervisor = FakeSup


class FakeLogger:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *a):
        self.lines.append(fmt % a if a else fmt)


class FakeCtx:
    def __init__(self, store):
        self.store = store
        self.hooks = {}
        self.logger = FakeLogger()
        self.unload = None
        self.writes = []

    def get_config(self, key, default=None):
        return self.store.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def on_unload(self, fn):
        self.unload = fn


# ============ 门控四形态（站立判定） ============
argv_backup = sys.argv
mods_backup = sys.modules.pop("gateway.run", None)
try:
    sys.argv = ["hermes", "gateway", "run"]
    ck("gate: gateway run argv", gui._is_gateway_host() is True)
    sys.argv = ["hermes", "gateway", "start"]
    ck("gate: gateway start argv", gui._is_gateway_host() is True)
    sys.argv = ["hermes", "dashboard"]
    ck("gate: dashboard argv inert", gui._is_gateway_host() is False)
    sys.argv = ["hermes"]
    ck("gate: cli argv inert", gui._is_gateway_host() is False)
    import types
    sys.modules["gateway.run"] = types.ModuleType("gateway.run")
    ck("gate: gateway.run imported -> active", gui._is_gateway_host() is True)
    del sys.modules["gateway.run"]
finally:
    sys.argv = argv_backup
    if mods_backup is not None:
        sys.modules["gateway.run"] = mods_backup

# ============ 非 gateway 宿主 register = noop ============
sys.argv = ["hermes", "dashboard"]
ctx0 = FakeCtx({})
gui.register(ctx0)
ck("inert host: no hooks", not ctx0.hooks)
ck("inert host: logged", any("inert" in ln for ln in ctx0.logger.lines))

# ============ gateway 宿主 register 全流程（设置界面 settings 协议）============
# 干净总线：清掉上一轮残留的 settings_*（watcher 启动基线也会忽略陈旧请求，双保险）
for _f in BUS.glob("settings_*.json"):
    _f.unlink(missing_ok=True)
# 场景：config pet 块缺 enable-pet 键（A1 现场）；旧 pet_cfg.json 留盘=死文件不读
(BUS / "pet_cfg.json").write_text(json.dumps(
    {"enable-pet": True, "slug": "cyrene", "scale": 0.7, "roam": True, "topmost": True}),
    encoding="utf-8")
sys.argv = ["hermes", "gateway", "run"]
ctx = FakeCtx({"pet": {"slug": "cyrene", "scale": 0.7, "roam": True, "topmost": True}})
gui.register(ctx)   # register 自建独立 stop token + 清 _REQ_DONE/_VIEW_LAST

want_hooks = {"pre_gateway_dispatch", "pre_api_request", "post_api_request",
              "post_llm_call", "pre_tool_call", "api_request_error", "agent_loop_stopped",
              "on_session_end", "pre_approval_request"}   # 事件反应 7→9
ck("9 hooks registered", set(ctx.hooks) == want_hooks)
ck("supervisor constructed", FakeSup.last is not None)
ck("supervisor worker path = worker/main.py",
   str(FakeSup.last.kw["worker_path"]).replace("\\", "/").endswith("gateway-ui/worker/main.py"))
ck("supervisor bus dir = scratch", "gui24-events-register" in str(FakeSup.last.kw["bus_dir"]))

# 设置界面：pet_cfg.json 退役=死文件，register 不再读它播种；config 缺 enable-pet 保持缺
time.sleep(0.6)  # 消费线程出首拍快照
view = json.loads((BUS / "settings_view.json").read_text(encoding="utf-8"))
ck("view snapshot written", view.get("pet", {}).get("slug") == "cyrene")
ck("view enable-pet runtime false (config missing, NOT from dead pet_cfg)",
   view["pet"]["enable-pet"] is False)
ck("view explicit lists real keys only", "enable-pet" not in view["explicit"]["pet"]
   and "slug" in view["explicit"]["pet"])
ck("config NOT polluted with default false", "enable-pet" not in ctx.store["pet"])
ck("top defaults filled", ctx.store.get("tray_enabled") is True
   and ctx.store.get("degrade_window_s") == 300)

# hooks O(1)：直接调回调不抛（载荷按 fire-site 实锤形状）
ctx.hooks["pre_api_request"](session_id="S1", task_id="T", turn_id="U",
                             provider="custom:main", model="m", platform="desktop",
                             base_url="https://main.example/v1")
ctx.hooks["pre_tool_call"](tool_name="terminal", session_id="S1")
ctx.hooks["post_llm_call"](session_id="S1", task_id="T", turn_id="U", model="m",
                           platform="desktop")
ck("hooks callable without raise", True)
time.sleep(0.4)  # 消费线程跑几拍
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("state.json written with contract fields",
   all(k in st for k in ("pet_state", "active_sessions", "boot_ts",
                         "last_event_seq", "writer_pid", "tray")))
ck("events.jsonl written", (BUS / "events.jsonl").is_file())
ck("writer lease written", (BUS / "writer_lease.json").is_file())
ck("notice field present (null before any turn end)", "notice" in st and st["notice"] is None)

# ============ 事件反应 notice 管线（新 hook → 状态机 → state.notice）============
# 先给 S1 记一条 desktop turn_start（_sess_plat 归因表；后续 clarify 用）
ctx.hooks["pre_api_request"](session_id="S1", task_id="T", turn_id="U0",
                             provider="custom:main", model="m", platform="desktop",
                             base_url="https://main.example/v1")
# on_session_end：desktop 完成 → notice done；同 turn_id 重发幂等
ctx.hooks["on_session_end"](session_id="S1", task_id="T", turn_id="U1",
                            completed=True, failed=False, interrupted=False,
                            turn_exit_reason="text_response(ok)", model="m",
                            platform="desktop")
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
n = st.get("notice")
ck("notice done derived", isinstance(n, dict) and n["kind"] == "done"
   and n["platform"] == "desktop" and n["seq"] == 1)
ck("notice whitelist fields only", isinstance(n, dict) and set(n) == {"seq", "kind", "ts", "platform"})
seq1 = n["seq"]
ctx.hooks["on_session_end"](session_id="S1", task_id="T", turn_id="U1",
                            completed=True, failed=False, interrupted=False,
                            model="m", platform="desktop")
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("same turn_id idempotent", st["notice"]["seq"] == seq1)
# telegram 平台门控：不产生 notice（seq 不动）
ctx.hooks["on_session_end"](session_id="S9", task_id="T9", turn_id="U9",
                            completed=True, failed=False, interrupted=False,
                            model="m", platform="telegram")
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("telegram gated out", st["notice"]["seq"] == seq1)
# failed → notice failed（新 turn）
ctx.hooks["on_session_end"](session_id="S1", task_id="T", turn_id="U2",
                            completed=False, failed=True, interrupted=False,
                            model="m", platform="cli")
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("notice failed derived", st["notice"]["kind"] == "failed" and st["notice"]["seq"] == seq1 + 1)
# interrupted → stopped（不算失败）
ctx.hooks["on_session_end"](session_id="S1", task_id="T", turn_id="U3",
                            completed=False, failed=False, interrupted=True,
                            model="m", platform="tui")
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("interrupted -> stopped not failed", st["notice"]["kind"] == "stopped")
# pre_approval_request：gateway 键 platform 段归因（desktop）；command 正文绝不入总线
ctx.hooks["pre_approval_request"](session_key="agent:main:desktop:dm:chat1",
                                  surface="gateway", command="rm -rf SECRET_CMD",
                                  description="dangerous", pattern_key="rm")
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("approval await_input attributed desktop", st["notice"]["kind"] == "await_input"
   and st["notice"]["platform"] == "desktop")
ev_text = (BUS / "events.jsonl").read_text(encoding="utf-8")
ck("approval command never on bus", "SECRET_CMD" not in ev_text and "dangerous" not in ev_text)
# clarify：pre_tool_call 截 tool_name==clarify，session→platform 表归因（S1=desktop）
ctx.hooks["pre_tool_call"](tool_name="clarify", session_id="S1",
                           args={"question": "SECRET_QUESTION"})
time.sleep(0.4)
st = json.loads((BUS / "state.json").read_text(encoding="utf-8"))
ck("clarify await_input attributed desktop", st["notice"]["kind"] == "await_input"
   and st["notice"]["platform"] == "desktop")
ck("clarify question never on bus", "SECRET_QUESTION" not in (BUS / "events.jsonl").read_text(encoding="utf-8"))

# settings watcher 回写（设置界面）：请求 enable-pet true → 官方 writer 落 config + 回执
def send_req(rid, key, value):
    (BUS / "settings_req.json").write_text(json.dumps(
        {"id": rid, "ts": time.time(), "key": key, "value": value, "source": "settings"}),
        encoding="utf-8")

send_req("req001", "pet.enable-pet", True)
time.sleep(0.8)
ck("watcher persisted menu true to config", ctx.store["pet"].get("enable-pet") is True)
ack = json.loads((BUS / "settings_ack.json").read_text(encoding="utf-8"))
ck("ack ok for req001", ack["id"] == "req001" and ack["ok"] is True)

# 请求翻 false（菜单关闭）→ watcher 写 false
send_req("req002", "pet.enable-pet", False)
time.sleep(0.8)
ck("watcher persisted menu false to config", ctx.store["pet"].get("enable-pet") is False)

# 非法 key 拒绝：不写 config，回执 error
send_req("req003", "model.api_key", "evil")
time.sleep(0.8)
ack = json.loads((BUS / "settings_ack.json").read_text(encoding="utf-8"))
ck("illegal key rejected", ack["id"] == "req003" and ack["ok"] is False and ack["error"])
ck("illegal key never touched config", "model" not in ctx.store)

# 幂等：同 id 重发不重复落盘（writer 调用计数）
writes_before = len(ctx.writes)
send_req("req002", "pet.enable-pet", True)   # 复用已处理 id
time.sleep(0.8)
ck("duplicate id idempotent (no extra write)", len(ctx.writes) == writes_before)

# shutdown
gui._shutdown()
ck("stop event set", gui._STOP.is_set())
ck("supervisor shutdown called", getattr(FakeSup.last, "stopped", False))
ck("unload registered", ctx.unload is not None)
sys.argv = argv_backup

fails = [n for n, ok in R if not ok]
print("PASS" if not fails else "FAIL(" + str(len(fails)) + "): " + "; ".join(fails))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
sys.exit(1 if fails else 0)