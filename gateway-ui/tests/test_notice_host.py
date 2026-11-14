# -*- coding: utf-8 -*-
"""test_notice_host.py — 事件反应宿主 notice 管线集成（hook→queue→消费线程→状态机→
state.json notice 字段 + NoticeTracker 端到端）。宿主 python 跑。
只用 scratch 总线 gui24-events-*，绝不碰真实总线/config。Supervisor 换 fake 不 spawn。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()
# worker 侧纯逻辑 NoticeTracker（无 Qt，宿主 python 可直读）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from notices import NoticeTracker

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

from _env import scratch  # noqa: E402
SCRATCH = scratch("gui24-events-host")
BUS = SCRATCH / "cache" / "gateway-ui"
BUS.mkdir(parents=True, exist_ok=True)
for f in ("state.json", "events.jsonl", "writer_lease.json"):
    (BUS / f).unlink(missing_ok=True)

gui._home = lambda: SCRATCH

class FakeSup:
    def __init__(self, **kw):
        self.kw = kw
    def run(self):
        pass
    def shutdown(self):
        pass
gui.Supervisor = FakeSup

class FakeLogger:
    def info(self, fmt, *a):
        pass

class FakeCtx:
    def __init__(self, store):
        self.store = store
        self.hooks = {}
        self.logger = FakeLogger()
        self.writes = []
    def get_config(self, key, default=None):
        return self.store.get(key, default)
    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value
    def register_hook(self, name, fn):
        self.hooks[name] = fn
    def on_unload(self, fn):
        pass

argv_backup = sys.argv
sys.argv = ["hermes", "gateway", "run"]
ctx = FakeCtx({})
gui.register(ctx)

def read_state(timeout=3.0):
    """等消费线程写出 state.json（首拍）。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            return json.loads((BUS / "state.json").read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("state.json not written")

def wait_notice(pred, timeout=3.0):
    """轮询 state.notice 直到 pred 成立；返回最终 notice（可能 None）。"""
    end = time.time() + timeout
    while time.time() < end:
        st = read_state()
        if pred(st.get("notice")):
            return st["notice"]
        time.sleep(0.05)
    return read_state().get("notice")

# ============ 初始：notice null ============
st = read_state()
ck("initial notice null", st.get("notice") is None)
ck("9 hooks registered", len(ctx.hooks) == 9)

# ============ turn_start(desktop) → on_session_end(completed) → done ============
ctx.hooks["pre_api_request"](session_id="S1", task_id="T1", turn_id="U1",
                             provider="custom:main", model="m", platform="desktop",
                             base_url="https://main.example/v1")
ctx.hooks["on_session_end"](session_id="S1", task_id="T1", turn_id="U1",
                            completed=True, failed=False, interrupted=False,
                            turn_exit_reason="text_response(ok)", model="m",
                            platform="desktop")
n = wait_notice(lambda x: x and x["kind"] == "done")
ck("done notice on state.json", n is not None and n["kind"] == "done")
ck("done platform desktop", n and n["platform"] == "desktop")
ck("notice seq monotonic start 1", n and n["seq"] == 1)
ck("notice fields whitelist", n and set(n) == {"seq", "kind", "ts", "platform"})

# ============ NoticeTracker 端到端：首轮基线不回放，增量反应 ============
# tracker 启动时 state 已有 done(seq1) → 首见只记基线（不回放陈旧）
tr2 = NoticeTracker()
ck("tracker empty state -> None", tr2.feed({"notice": None}) is None)
ck("tracker first real notice = baseline (no replay)", tr2.feed(read_state()) is None)
ck("tracker dedup same state", tr2.feed(read_state()) is None)

# ============ failed → failed notice（新 turn）============
ctx.hooks["on_session_end"](session_id="S1", task_id="T1", turn_id="U2",
                            completed=False, failed=True, interrupted=False,
                            model="m", platform="desktop")
n = wait_notice(lambda x: x and x["kind"] == "failed")
ck("failed notice", n is not None and n["kind"] == "failed" and n["seq"] == 2)
n3 = tr2.feed(read_state())
ck("tracker failed increment", n3 is not None and n3["kind"] == "failed")

# ============ interrupted → stopped（不算失败）============
ctx.hooks["on_session_end"](session_id="S1", task_id="T1", turn_id="U3",
                            completed=False, failed=True, interrupted=True,
                            model="m", platform="tui")
n = wait_notice(lambda x: x and x["kind"] == "stopped")
ck("stopped notice (interrupted beats failed)", n is not None and n["kind"] == "stopped")
ck("stopped platform tui", n and n["platform"] == "tui")

# ============ 平台门控：telegram 完成不产生 notice ============
seq_before = read_state()["notice"]["seq"]
ctx.hooks["on_session_end"](session_id="STG", task_id="TT", turn_id="UTG",
                            completed=True, failed=False, interrupted=False,
                            model="m", platform="telegram")
time.sleep(0.5)
ck("telegram gated (seq unchanged)", read_state()["notice"]["seq"] == seq_before)

# ============ await_input：approval（gateway 键归因 desktop）============
ctx.hooks["pre_approval_request"](session_key="agent:main:desktop:dm:c1",
                                  surface="gateway", command="SECRET_CMD_XYZ",
                                  description="SECRET_DESC", pattern_key="rm")
n = wait_notice(lambda x: x and x["kind"] == "await_input")
ck("approval await_input notice", n is not None and n["kind"] == "await_input")
ck("approval attributed desktop", n and n["platform"] == "desktop")
ev = (BUS / "events.jsonl").read_text(encoding="utf-8")
ck("approval command not on bus", "SECRET_CMD_XYZ" not in ev)
ck("approval description not on bus", "SECRET_DESC" not in ev)

# ============ await_input：clarify（session→platform 表归因）============
ctx.hooks["pre_tool_call"](tool_name="clarify", session_id="S1",
                           args={"question": "SECRET_Q"})
n = wait_notice(lambda x: x and x["kind"] == "await_input" and x["seq"] > 4)
ck("clarify await_input notice", n is not None and n["kind"] == "await_input")
ck("clarify attributed desktop (S1 turn_start)", n and n["platform"] == "desktop")
ck("clarify question not on bus", "SECRET_Q" not in (BUS / "events.jsonl").read_text(encoding="utf-8"))

# ============ 同 turn_id 幂等（重发不增 seq）============
seq_before = read_state()["notice"]["seq"]
ctx.hooks["on_session_end"](session_id="S1", task_id="T1", turn_id="U1",
                            completed=True, failed=False, interrupted=False,
                            model="m", platform="desktop")
time.sleep(0.5)
ck("replayed turn_id idempotent", read_state()["notice"]["seq"] == seq_before)

# ============ 既有契约字段不回归 ============
st = read_state()
ck("contract fields intact", all(k in st for k in
   ("pet_state", "active_sessions", "active_source", "writer_pid",
    "boot_ts", "last_event_seq", "tray")))
ck("tray subfields intact", all(k in st["tray"] for k in
   ("light", "tier", "degrade", "degrade_window_s", "version", "update_available", "bus")))

gui._shutdown()
sys.argv = argv_backup

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)