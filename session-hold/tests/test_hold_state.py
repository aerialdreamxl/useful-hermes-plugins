"""Offline state-machine tests for session-hold v2, ISOLATED HERMES_HOME.

v1 tool-block path: arm -> block-at-next-tool (armed->held) -> sticky -> /unhold
clears; restart durability; resume-note one-shot; arm TTL.
v2 park path: armed -> llm_execution middleware snapshots + parks the turn
thread; /unhold wakes it and the SAME request goes through next_call.
v2 replay path: restart after park -> /unhold arms replay="pending"; the
llm_request middleware swaps frame 1 for the frozen payload and recombines
frozen + appended on later frames; the post_api_request observer closes the
episode on the model's final answer.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="hold_home_"))
os.environ["HERMES_HOME"] = str(home)
SRC = Path(r"D:\01-AI\Hermes-Agent\Install-Windows")
sys.path.insert(0, str(SRC))
sys.modules.pop("hermes_constants", None)  # re-bind under the isolated home
# Stub the TUI server so _tui_server() resolves to "no live desktop sessions"
# without importing the real one (its import has process-level side effects).
_tg = types.ModuleType("tui_gateway")
_tg_server = types.ModuleType("tui_gateway.server")  # no _sessions attr
sys.modules["tui_gateway"] = _tg
sys.modules["tui_gateway.server"] = _tg_server

PLUGIN_DIR = Path(r"D:\01-AI\Hermes-Agent\Data\plugins\session-hold\__init__.py")
sys.path.insert(0, str(PLUGIN_DIR.parent))  # so the bare `import freeze` fallback resolves


def phase_of(state, key):
    """GC in _persist_locked drops terminal entries; 'gone' == released-clean."""
    return state.get(key, {}).get("phase") or "gone"


def load_plugin(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sh = load_plugin("session_hold")


class FakeCtx:
    def __init__(self):
        self.hooks, self.cmds, self.mw = {}, {}, {}
    def register_hook(self, name, cb): self.hooks.setdefault(name, []).append(cb)
    def register_command(self, name, handler, **kw): self.cmds[name] = handler
    def register_middleware(self, kind, cb): self.mw.setdefault(kind, []).append(cb)


ctx = FakeCtx()
sh.register(ctx)
pre_tool = ctx.hooks["pre_tool_call"][0]
pre_llm = ctx.hooks["pre_llm_call"][0]
post_api = ctx.hooks["post_api_request"][0]
hold, unhold = ctx.cmds["hold"], ctx.cmds["unhold"]
park_mw = ctx.mw["llm_execution"][0]
replay_mw = ctx.mw["llm_request"][0]

KEY = "session:test-1"
fails = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        fails.append(label)


def make_request(n_msgs=3):
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "working"}][:n_msgs]
    return {"model": "m", "messages": list(msgs)}


def arm(key, mode=None):
    """Hold via the explicit-key command form; then force the mode (park tests
    exercise the default; tool-block tests override it like a degraded entry)."""
    hold(key)
    if mode:
        with sh._lock:
            sh._state[key]["mode"] = mode
    out = make_request()
    return out


# ================= v1 TOOL-BLOCK path (mode override) =================

check("idle passthrough", pre_tool(tool_name="terminal", session_id=KEY, task_id=KEY, turn_id="t1") is None)

arm(KEY, mode="toolblock")
check("toolblock: armed -> first tool blocked",
      (pre_tool(tool_name="terminal", session_id=KEY, task_id=KEY, turn_id="t1") or {}).get("action") == "block")
check("toolblock: phase now held", sh._state[KEY]["phase"] == "held")
check("toolblock: held blocks new turn too",
      (pre_tool(tool_name="write_file", session_id=KEY, task_id=KEY, turn_id="t2") or {}).get("action") == "block")
check("other session unaffected",
      pre_tool(tool_name="terminal", session_id="session:other", task_id="session:other", turn_id="t9") is None)
pre_llm(session_id=KEY, task_id=KEY, user_message="rewrite the parser module")
check("task_note captured", sh._state[KEY].get("task_note") == "rewrite the parser module")

# park middleware must NOT touch tool-block entries
passed = []
park_mw(request=make_request(), next_call=passed.append, task_id=KEY, session_id=KEY)
check("toolblock: park middleware ignores it", len(passed) == 1)

# restart durability (fresh module, same state file)
sh2 = load_plugin("session_hold_2")
sh2._load()
check("held survives restart", phase_of(sh2._state, KEY) == "held")
check("held still blocks after restart",
      (sh2._make_pre_tool_call()(tool_name="terminal", session_id=KEY, task_id=KEY, turn_id="t3") or {})
      .get("action") == "block")

# unhold (toolblock) clears + queues a one-shot resume note
out = unhold(KEY)
check("unhold releases", "queued" in out.lower() or "resuming" in out.lower())
r = pre_llm(session_id=KEY, task_id=KEY, user_message="/unhold")
check("resume note injected once", isinstance(r, dict) and "resume it immediately" in r.get("context", ""))
r2 = pre_llm(session_id=KEY, task_id=KEY, user_message="next message")
check("resume note one-shot", r2 is None)

# /hold off disarms (explicit form)
arm(KEY, mode="toolblock")
out = hold(f"off {KEY}")
check("/hold off <key> disarms", "disarmed" in out.lower() and sh._entry(KEY) is None)

# arm TTL expiry
hold(KEY)
sh._state[KEY]["armed_at"] = time.time() - sh._ARM_TTL_SECS - 10
check("stale arm expires", sh._entry(KEY) is None)

# ladder match: ephemeral task_id hits a session_id hold
KEY2 = "session:by-session-id"
arm(KEY2, mode="toolblock")
check("ladder: ephemeral task_id hits the hold",
      (pre_tool(tool_name="terminal", session_id=KEY2, task_id="uuid-ephemeral", turn_id="t1") or {})
      .get("action") == "block")
hold(f"off {KEY2}")

# ================= v2 PARK path =================

PK = "session:park-1"
arm(PK)  # default mode=park
req = make_request()
sent = []

def fake_next(r):
    sent.append(r)

t = threading.Thread(target=park_mw, kwargs={"request": req, "next_call": fake_next,
                                             "task_id": PK, "session_id": PK},
                     daemon=True)
t.start()
time.sleep(1.0)
check("park: request NOT yet sent", not sent)
check("park: thread parked", PK in sh._freeze.PARKS.parked_keys())
check("park: phase held", sh._state[PK]["phase"] == "held")
check("park: snapshot durable", sh._SNAPSTORE.load(PK) is not None)

out = unhold(PK)
t.join(timeout=5)
check("park: unhold woke the thread", not t.is_alive())
check("park: frozen request sent on release", sent == [req])
check("park: hold released", phase_of(sh._state, PK) in {"released", "gone"})
check("park: snapshot drained", sh._SNAPSTORE.load(PK) is None)

# interrupt drains the park to idle
arm(PK)
class FakeAgent:
    _interrupt_requested = False
agent = FakeAgent()
sh._freeze.set_agent_finder(lambda key: agent if key == PK else None)
t = threading.Thread(target=park_mw, kwargs={"request": req, "next_call": fake_next,
                                             "task_id": PK, "session_id": PK}, daemon=True)
sent.clear()
t.start()
time.sleep(0.8)
agent._interrupt_requested = True
t.join(timeout=5)
sh._freeze.set_agent_finder(lambda key: None)
check("park: interrupt woke the thread", not t.is_alive())
check("park: request passed through after interrupt", sent == [req])
check("park: entry drained to idle", phase_of(sh._state, PK) in {"idle", "released", "gone"})
check("park: interrupt discards snapshot", sh._SNAPSTORE.load(PK) is None)

# replay stamp: a swapped frame is never re-parked
sent.clear()
req2 = dict(make_request())
req2["extra_headers"] = {sh._freeze.REPLAY_HEADER: "1"}
park_mw(request=req2, next_call=fake_next, task_id=PK, session_id=PK)
check("park: replay-stamped frame passes straight through", sent == [req2])

# ================= v2 REPLAY path (restart after park) =================

RK = "session:replay-1"
arm(RK)
# simulate: parked (snapshot saved pre-death, phase held persisted), then the
# process DIED with the thread asleep — fresh module == fresh process
sh._SNAPSTORE.save(RK, make_request())
sh._state[RK]["phase"] = "held"
sh._persist_locked()
sh3 = load_plugin("session_hold_3")
sh3._load()
check("replay setup: held restored", phase_of(sh3._state, RK) == "held")
check("replay setup: snapshot survived on disk", sh3._SNAPSTORE.load(RK) is not None)
ctx3 = FakeCtx()
sh3.register(ctx3)
hold3, unhold3 = ctx3.cmds["hold"], ctx3.cmds["unhold"]
pre_tool3 = ctx3.hooks["pre_tool_call"][0]
post_api3 = ctx3.hooks["post_api_request"][0]
replay3 = ctx3.mw["llm_request"][0]
park3 = ctx3.mw["llm_execution"][0]

# /unhold before any turn: arms the replay episode (no kick channel here)
out = unhold3(RK)
check("replay: unhold armed the episode", sh3._state[RK].get("replay") == "pending")

# post-restart next turn: live payload = frozen prefix + synthetic kickoff tail
frozen = sh3._SNAPSTORE.load(RK)["messages"]
live = list(frozen) + [{"role": "user", "content": sh3._freeze.KICK_PREFIX + " resume now"}]
req3 = {"model": "m", "messages": live}
res = replay3(request=req3, task_id=RK, session_id=RK)
check("replay: frame 1 swapped to frozen payload",
      res and res["request"]["messages"] == frozen)
check("replay: frame 1 stamped", sh3._freeze._is_replay(res["request"]))
check("replay: envelope preserved", res["request"].get("model") == "m")

# frame 2: loop appended assistant+tool rows after frame 1
live2 = live + [{"role": "assistant", "content": "done part A", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
req4 = {"model": "m", "messages": live2}
res2 = replay3(request=req4, task_id=RK, session_id=RK)
check("replay: frame N = frozen + appends",
      res2 and res2["request"]["messages"] == frozen + live2[len(live):])

# a final answer closes the episode (observer path)
post_api3(assistant_message={"role": "assistant", "content": "all finished"},
          session_id=RK, task_id=RK)
check("replay: episode closed on final answer", phase_of(sh3._state, RK) in {"released", "gone"})
check("replay: snapshot drained on close", sh3._SNAPSTORE.load(RK) is None)
res3 = replay3(request={"model": "m", "messages": live2}, task_id=RK, session_id=RK)
check("replay: after close, no more swaps (only the synthetic row is stripped)",
      res3 is None or all(not str(m.get("content", "")).startswith(sh._freeze.KICK_PREFIX)
                          for m in res3["request"]["messages"] if isinstance(m, dict)))

# takeover gate: user message during pending episode ends it
TK = "session:replay-2"
hold3(TK)
with sh3._lock:
    sh3._state[TK]["phase"] = "held"
sh3._SNAPSTORE.save(TK, make_request())
sh3._persist_locked()
unhold3(TK)
live_t = list(sh3._SNAPSTORE.load(TK)["messages"]) + \
    [{"role": "user", "content": "actually stop that and do X"}]
res_t = replay3(request={"model": "m", "messages": live_t}, task_id=TK, session_id=TK)
check("replay: frame 1 keeps the real user message (swallowing it would be worse)",
      res_t and res_t["request"]["messages"][-1]["content"].startswith("actually"))
post_api3(assistant_message={"role": "assistant", "content": "ok switching"},
          session_id=TK, task_id=TK)
check("replay: takeover episode closed", phase_of(sh3._state, TK) in {"released", "gone"})

# missing snapshot -> fail-open end (released, never stuck held)
MK = "session:replay-3"
hold3(MK)
with sh3._lock:
    sh3._state[MK].update(phase="held", replay="pending")
res_m = replay3(request={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                task_id=MK, session_id=MK)
check("replay: missing snapshot ends cleanly", phase_of(sh3._state, MK) in {"released", "gone"})

# park re-fire gate: a held+park entry with NO thread and NO pending episode
# re-parks on the next API call (post-restart seamless pause)
RP = "session:repark-1"
hold(RP)
with sh._lock:
    sh._state[RP].update(phase="held", mode="park")
sent.clear()
reqp = make_request()
t = threading.Thread(target=park_mw, kwargs={"request": reqp, "next_call": fake_next,
                                             "task_id": RP, "session_id": RP}, daemon=True)
t.start()
time.sleep(0.8)
check("repark: held+park without a thread re-parks", RP in sh._freeze.PARKS.parked_keys())
sh._freeze.PARKS.release(RP)
t.join(timeout=5)
check("repark: released and request flowed", sent == [reqp])

# --- state file sanity
data = json.loads((home / "session-hold" / "state.json").read_text(encoding="utf-8"))
check("state file json ok", isinstance(data, dict))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
