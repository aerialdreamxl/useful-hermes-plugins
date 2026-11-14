# -*- coding: utf-8 -*-
"""忙线判定回归：忙线判据 session_id 幂等 + 十五/写者租约权威源。
旧 busy_light_test.py 24+6 断言全量移植（StateMachine 纯函数版）。宿主 python 跑。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()
SM = gui.StateMachine
R = []


def ck(name, ok):
    R.append((name, bool(ok)))


sm = SM()


def reset():
    global sm
    sm = SM()


def ev(kind, ts, **fl):
    sm.apply(kind, ts, fl)


t = time.time()

# --- 场景1：单会话 tool-loop 不虚增 ---
reset()
ev("turn_start", t, session_key="S1", task_key="A")
ev("post_api", t + 1, session_key="S1")
ev("tool", t + 2, session_key="S1", tool="terminal")
ck("multi-req counts as 1", len(sm.active) == 1)
ck("acting after tool", sm.derive(t + 2) == "acting")
sm.authority = {"desk:abc": "acting"}
ck("authority 1 -> phase", sm.derive(t + 2) in ("acting", "thinking"))
sm.authority = {"a": "thinking", "b": "acting"}
ck("authority >=2 -> busy", sm.derive(t + 2) == "busy")
sm.authority = None
ck("light orange", sm.tier_and_light(t + 2, 300)[1] == "orange")
ev("turn_start", t + 3, session_key="S1", task_key="B")
ck("still 1", len(sm.active) == 1)
ev("turn_end", t + 4, session_key="S1", ok=True)
ck("0 active after end", len(sm.active) == 0)
ck("happy transient", sm.derive(t + 4) == "happy")
ck("idle after 2s", sm.derive(t + 7) == "idle")

# --- 场景2：双会话并发恒 busy ---
reset()
ev("turn_start", t, session_key="S1")
ev("tool", t + 0.5, session_key="S1")
ev("turn_start", t + 1, session_key="S2")
ck("2 active", len(sm.active) == 2)
ck("busy semantics", sm.derive(t + 1) == "busy")
ck("light orange 2", sm.tier_and_light(t + 1, 300)[1] == "orange")

# --- 场景3：一键结束回另一键阶段 ---
ev("turn_end", t + 2, session_key="S2", ok=True)
ck("back to single", len(sm.active) == 1)
ck("S1 phase acting kept", sm.derive(t + 2) == "acting")

# --- 场景4：键对不上不瞎销账 ---
reset()
ev("turn_start", t, session_key="S1")
ev("turn_start", t + 1, session_key="S2")
ev("turn_end", t + 2, session_key="", ok=True)
ck("no blind reap with 2 active", len(sm.active) == 2)
ev("turn_end", t + 3, session_key="S2", ok=True)
ev("turn_end", t + 4, session_key="?", ok=True)
ck("unknown key reaps last single", len(sm.active) == 0)

# --- 场景5：错误红灯与活跃解耦 ---
reset()
ev("turn_start", t, session_key="S1")
ev("error", t + 1, session_key="S1")
tier, light = sm.tier_and_light(t + 1, 300)
ck("red on error active", light == "red")
ck("derive busy-class kept", sm.derive(t + 1) in ("thinking", "error"))

# --- 场景6：inbound listening ---
reset()
ev("inbound", t, platform="desktop")
ck("listening", sm.derive(t) == "listening")
ck("listening decays", sm.derive(t + 4) == "idle")

# --- 权威模式独立场景 ---
reset()
sm.authority = {"s1": "thinking", "s2": "acting"}
ck("authority-only 2 -> busy", sm.derive(t) == "busy")
sm.authority = {"s1": "acting"}
ck("authority-only 1 -> phase", sm.derive(t) == "acting")
sm.authority = {"s1": "thinking"}
ck("authority phase flip", sm.derive(t) == "thinking")
sm.authority = {}
ck("authority-empty -> idle-class", sm.derive(t + 999) == "idle")
sm.authority = None

# --- 降级判定（降级判定 base_url 端点）---
reset()
sm.baseline_base_url = "https://main.example/v1"
ev("turn_start", t, session_key="S1", base_url="https://main.example/v1")
ck("same endpoint no degrade", sm.last_degrade is None)
ev("post_api", t + 1, session_key="S1", base_url="https://backup.example/v1")
ck("in-turn switch counts", sm.last_degrade is not None)
ck("degrade red in window", sm.tier_and_light(t + 2, 300)[1] == "red")
ck("degrade red expires", sm.tier_and_light(t + 400, 300)[1] != "red")
reset()
sm.baseline_base_url = "https://main.example/v1"
ev("turn_start", t, session_key="S1", base_url="https://main.example/v1", provider="other-name")
ck("provider rename not degrade", sm.last_degrade is None)

# --- 权威源接入/退避 ---
reset()
class FakeAgent:
    def __init__(self, d):
        self._last_activity_desc = d
sm.set_authority([("k1", FakeAgent("executing tool: terminal")),
                  ("k2", FakeAgent("waiting for provider response"))])
ck("set_authority phases", sm.authority == {"k1": "acting", "k2": "thinking"})
sm.authority_unreachable()
sm.authority_unreachable()
ck("2 misses keep authority", sm.authority is not None)
sm.authority_unreachable()
ck("3 misses -> fallback", sm.authority is None)

# --- 写者租约 _phase_from_activity 6 断言 ---
F = gui.statemachine.phase_from_activity
R2 = [
    ("tool", F("executing tool: terminal") == "acting"),
    ("tool-par", F("executing 3 tools concurrently: a,b,c") == "acting"),
    ("api-wait", F("waiting for provider response (streaming)") == "thinking"),
    ("model-read", F("model reading the prompt") == "thinking"),
    ("stream", F("receiving stream response") == "thinking"),
    ("empty", F("") == "thinking"),
]

# --- 事件反应 notice 派生（完成/失败/等人类输入；平台白名单+幂等）---
SMOD = gui.statemachine
reset()
ck("notice initial None", sm.notice is None)
# completed → done（desktop）
ev("turn_end", t, session_key="S1", turn_key="U1", platform="desktop", completed=True)
ck("completed -> done", sm.notice and sm.notice["kind"] == "done")
ck("notice seq starts 1", sm.notice["seq"] == 1)
ck("notice whitelist fields", set(sm.notice) == {"seq", "kind", "ts", "platform"})
ck("notice platform kept", sm.notice["platform"] == "desktop")
# 同 turn_id 幂等
ev("turn_end", t + 1, session_key="S1", turn_key="U1", platform="desktop", completed=True)
ck("same turn_id deduped", sm.notice["seq"] == 1)
# failed → failed（cli）
ev("turn_end", t + 2, session_key="S1", turn_key="U2", platform="cli", failed=True)
ck("failed -> failed", sm.notice["kind"] == "failed" and sm.notice["seq"] == 2)
# interrupted 不算失败 → stopped（failed 同真也优先 stopped）
ev("turn_end", t + 3, session_key="S1", turn_key="U3", platform="tui",
   interrupted=True, failed=True)
ck("interrupted -> stopped", sm.notice["kind"] == "stopped" and sm.notice["seq"] == 3)
# 平台门控：telegram/discord/qqbot/api_server/cron/subagent/空 一律不产生
for plat in ("telegram", "discord", "qqbot", "api_server", "cron", "subagent", ""):
    ev("turn_end", t + 4, session_key="SX", turn_key="UX-" + plat,
       platform=plat, completed=True)
ck("non-whitelist platforms gated", sm.notice["seq"] == 3)
# 旧 turn_end（post_llm_call/agent_loop_stopped 形状，无结局标志）不产生 notice
ev("turn_end", t + 5, session_key="S1", platform="desktop", ok=True)
ck("legacy turn_end no notice", sm.notice["seq"] == 3)
# 既有记账/灯色不受 notice 影响
ck("active accounting intact", len(sm.active) == 0)
ck("happy transient intact", sm.derive(t + 5) == "happy")

# --- await_input：approval（gateway 键 platform 段归因）---
reset()
ev("await_input", t, source="approval", session_key="agent:main:desktop:dm:c1",
   surface="gateway")
ck("approval gateway-key -> desktop", sm.notice and sm.notice["kind"] == "await_input"
   and sm.notice["platform"] == "desktop")
ck("await seq 1", sm.notice["seq"] == 1)
# coalesced 连发同 (source, session_key) 短窗压一条
ev("await_input", t + 1, source="approval", session_key="agent:main:desktop:dm:c1",
   surface="gateway")
ck("coalesced deduped", sm.notice["seq"] == 1)
# 不同 source（clarify）同会话不压
ev("await_input", t + 2, source="clarify", session_key="agent:main:desktop:dm:c1")
ck("different source not deduped", sm.notice["seq"] == 2)
# 消息平台 gateway 键 → 门控
ev("await_input", t + 3, source="approval", session_key="agent:main:telegram:dm:c9",
   surface="gateway")
ck("telegram approval gated", sm.notice["seq"] == 2)
# surface="cli" 但 session_key 非白名单平台 → 不误报（宁漏报）
ev("await_input", t + 4, source="approval", session_key="agent:main:discord:dm:c9",
   surface="cli")
ck("cli surface discord key gated", sm.notice["seq"] == 2)

# --- await_input：clarify（session→platform 表归因，turn_start 实锤）---
reset()
ev("turn_start", t, session_key="SID1", platform="tui")
ev("await_input", t + 1, source="clarify", session_key="SID1")
ck("clarify attributed via turn_start", sm.notice and sm.notice["kind"] == "await_input"
   and sm.notice["platform"] == "tui")
# 无归因（陌生 session_id，非 gateway 键）→ 不产生
ev("await_input", t + 2, source="clarify", session_key="UNKNOWN-SID")
ck("unattributed clarify gated", sm.notice["seq"] == 1)
# session_id 旁路归因（approval hook 注入的 session_id 对表）
ev("await_input", t + 3, source="approval", session_key="", session_id="SID1",
   surface="smart")
ck("approval session_id fallback attributed", sm.notice["seq"] == 2
   and sm.notice["platform"] == "tui")

# --- notice 纯函数 ---
ck("derive_notice_kind done", SMOD.derive_notice_kind({"completed": True}) == "done")
ck("derive_notice_kind failed", SMOD.derive_notice_kind({"failed": True}) == "failed")
ck("derive_notice_kind stopped priority",
   SMOD.derive_notice_kind({"interrupted": True, "failed": True}) == "stopped")
ck("derive_notice_kind empty", SMOD.derive_notice_kind({}) == "")
ck("platform_from_session_key", SMOD.platform_from_session_key("agent:main:cli:dm:x") == "cli")
ck("platform_from_session_key bad", SMOD.platform_from_session_key("SID1") == "")
ck("notice_platform normalize", SMOD.notice_platform({"platform": " Desktop "}) == "desktop")

# --- notice 去重表 GC ---
reset()
ev("turn_end", t, session_key="S1", turn_key="UG", platform="desktop", completed=True)
ev("await_input", t, source="approval", session_key="agent:main:desktop:dm:cg")
sm.gc(t + SMOD.NOTICE_TURN_TTL + 1)
ck("turn dedup table gc", not sm._notice_turns)
ck("wait dedup table gc", not sm._notice_waits)
ck("sess plat table gc", not sm._sess_plat)
ck("notice survives gc", sm.notice is not None)

fails = [n for n, ok in R if not ok]
print("PASS" if not fails else "FAIL(" + str(len(fails)) + "): " + "; ".join(fails))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
f2 = [n for n, ok in R2 if not ok]
print("ACT-PHASE PASS" if not f2 else "ACT-PHASE FAIL: " + ", ".join(f2))
for n, ok in R2:
    print((" ok " if ok else " !! ") + n)
sys.exit(1 if (fails or f2) else 0)