# -*- coding: utf-8 -*-
"""test_approval_state.py — 审批弹窗状态机+配置（PROMPT §12.4 断言 5/8，宿主 python）。

R28-5 await 持续档：pending>0 压过活跃阶段与展示层、error 短窗仍优先、红仍最优先、
pending=0 字节级旧行为、150 拍假时钟单调无跳变。config approval 块：白名单/三态/
快照/fill/transport_selected 探针。全部纯函数/FakeCtx，绝不碰真实 config。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin  # noqa: E402

gui = load_plugin()
cfg = gui.cfgmod
SM = gui.StateMachine

R = []
def ck(name, ok):
    R.append((name, bool(ok)))


class FakeCtx:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.writes = []
    def get_config(self, key, default=None):
        return self.store.get(key, default)
    def set_config(self, key, value):
        self.writes.append((key, value))
        self.store[key] = value


# ==================================================== R28-5 derive ====
sm = SM()
sm.pending_approvals = 1
ck("derive: pending>0 -> await", sm.derive(1000.0) == "await")

# error 短窗优先于 await
sm = SM()
sm.pending_approvals = 1
sm.error_ts = [999.0]
sm.error_active_until = 1030.0
ck("derive: error short-window beats await", sm.derive(1000.0) == "error")
ck("derive: await resumes after error window", sm.derive(1031.0) == "await")

# await 压过活跃会话阶段（被审批阻塞的会话不是在工作）
sm = SM()
sm.pending_approvals = 1
sm.set_authority([("agent:ns:desktop:dm:1", type("A", (), {"_last_activity_desc": "executing tool: terminal"})())])
ck("derive: await overrides active acting", sm.derive(1000.0) == "await")
sm.set_authority([("k1", type("A", (), {"_last_activity_desc": "waiting for provider response"})()),
                  ("k2", type("A", (), {"_last_activity_desc": ""})())])
ck("derive: await overrides busy(2)", sm.derive(1000.0) == "await")

# await 压过 happy/listening/idle 展示层
sm = SM()
sm.pending_approvals = 2
sm.happy_until = 2000.0
sm.state_until = 2000.0
ck("derive: await overrides happy+listening", sm.derive(1000.0) == "await")

# pending=0 → 字节级旧行为
sm = SM()
sm.pending_approvals = 0
ck("derive: pending=0 idle", sm.derive(1000.0) == "idle")
sm.happy_until = 2000.0
ck("derive: pending=0 happy preserved", sm.derive(1000.0) == "happy")
sm2 = SM()
sm2.set_authority([("k", type("A", (), {"_last_activity_desc": "executing tool: x"})())])
ck("derive: pending=0 active acting preserved", sm2.derive(1000.0) == "acting")

# 150 拍假时钟单调（pending 恒 1 → 恒 await，无跳变）
sm = SM()
sm.pending_approvals = 1
states = {sm.derive(1000.0 + i * 0.05) for i in range(150)}
ck("stability: 150 ticks monotonic await (no flap)", states == {"await"})
sm.pending_approvals = 0
ck("stability: clears to idle on zero", sm.derive(1080.0) == "idle")

# ================================================ R28-5 tier_and_light ====
sm = SM()
sm.pending_approvals = 1
tier, light = sm.tier_and_light(1000.0, 300)
ck("light: pending -> yellow", light == "yellow")
ck("tier: pending -> 等待人类批准 1 项", tier == "等待人类批准 1 项")
sm.pending_approvals = 3
ck("tier: count reflects pending", sm.tier_and_light(1000.0, 300)[0] == "等待人类批准 3 项")

# 红仍最优先（降级窗内）
sm = SM()
sm.pending_approvals = 1
sm.last_degrade = {"from": "a", "to": "b", "provider": "", "ts": 990.0}
ck("light: red beats yellow (degrade)", sm.tier_and_light(1000.0, 300)[1] == "red")
# 错误连发≥2 红
sm = SM()
sm.pending_approvals = 1
sm.error_ts = [995.0, 996.0]
ck("light: red beats yellow (2 errors)", sm.tier_and_light(1000.0, 300)[1] == "red")

# pending=0 旧灯色保持
sm = SM()
sm.pending_approvals = 0
ck("light: pending=0 green idle", sm.tier_and_light(1000.0, 300)[1] == "green")
sm.set_authority([("k", type("A", (), {"_last_activity_desc": ""})())])
ck("light: pending=0 orange active", sm.tier_and_light(1000.0, 300)[1] == "orange")

# ================================================ config approval ====
ck("config: approval.popup_enabled in REQ_KEYS", "approval.popup_enabled" in cfg.REQ_KEYS)
ck("config: approval.popup_sounds in REQ_KEYS", "approval.popup_sounds" in cfg.REQ_KEYS)
ok, k, v, e = cfg.validate_request({"key": "approval.popup_enabled", "value": True})
ck("validate: popup_enabled bool ok", ok is True and v is True)
ok, k, v, e = cfg.validate_request({"key": "approval.popup_sounds", "value": 0})
ck("validate: popup_sounds coerced bool", ok is True and v is False)
ok, k, v, e = cfg.validate_request({"key": "approval.bogus", "value": True})
ck("validate: unknown approval key rejected", ok is False)

# build_view 缺省（修改类 popup_enabled 缺键 false，不落 config）
ctx = FakeCtx({})
v = cfg.build_view(ctx)
ck("view: approval block present", "approval" in v)
ck("view: popup_enabled default false", v["approval"]["popup_enabled"] is False)
ck("view: popup_sounds default true", v["approval"]["popup_sounds"] is True)
ck("view: explicit.approval empty", v["explicit"]["approval"] == [])
ck("view: build never writes config", len(ctx.writes) == 0)

# 显式值照用
ctx = FakeCtx({"approval": {"popup_enabled": True, "popup_sounds": False}})
v = cfg.build_view(ctx)
ck("view: explicit popup_enabled true", v["approval"]["popup_enabled"] is True)
ck("view: explicit popup_sounds false", v["approval"]["popup_sounds"] is False)
ck("view: explicit.approval lists keys", set(v["explicit"]["approval"]) == {"popup_enabled", "popup_sounds"})

# transport_selected 探针（monkeypatch read_security_transport）
_orig = cfg.read_security_transport
cfg.read_security_transport = lambda: "gateway-ui"
ck("view: transport_selected true when security selects", cfg.build_view(FakeCtx({}))["approval"]["transport_selected"] is True)
cfg.read_security_transport = lambda: "builtin"
ck("view: transport_selected false for builtin", cfg.build_view(FakeCtx({}))["approval"]["transport_selected"] is False)
cfg.read_security_transport = lambda: ""
ck("view: transport_selected false empty", cfg.build_view(FakeCtx({}))["approval"]["transport_selected"] is False)
cfg.read_security_transport = _orig

# fill_approval_display：补 popup_sounds，绝不补 popup_enabled（A1）
ctx = FakeCtx({})
out = cfg.fill_approval_display(ctx)
ck("fill: popup_sounds filled", out.get("popup_sounds") is True)
ck("fill: popup_enabled NOT seeded", "popup_enabled" not in out)
ck("fill: wrote once", len(ctx.writes) == 1)
# 显式 false 不被碾掉
ctx = FakeCtx({"approval": {"popup_enabled": False, "popup_sounds": False}})
out = cfg.fill_approval_display(ctx)
ck("fill: explicit popup_sounds false preserved", out.get("popup_sounds") is False)
ck("fill: no write when complete", len(ctx.writes) == 0)
# 白名单外用户手加键保全（N22 同款）
ctx = FakeCtx({"approval": {"mood": "happy"}})
out = cfg.fill_approval_display(ctx)
ck("fill: unknown user key preserved", out.get("mood") == "happy")

# read_approval_explicit 纯读过滤
ctx = FakeCtx({"approval": {"popup_enabled": True, "mood": "happy"}})
ex = cfg.read_approval_explicit(ctx)
ck("read_explicit: filtered to whitelist", ex == {"popup_enabled": True})
ck("read_explicit: no writes", len(ctx.writes) == 0)

# _apply_setting approval 整块合并（保未知键）
ctx = FakeCtx({"approval": {"mood": "happy"}})
gui._CTX = ctx
gui._apply_setting("approval.popup_enabled", True)
ck("apply: approval leaf merged", ctx.store["approval"].get("popup_enabled") is True)
ck("apply: approval unknown key kept", ctx.store["approval"].get("mood") == "happy")

# ==================================== R29：platform_takeover 配置链 ====
ck("config: approval.platform_takeover in REQ_KEYS", "approval.platform_takeover" in cfg.REQ_KEYS)
ok, k, v, e = cfg.validate_request({"key": "approval.platform_takeover", "value": True})
ck("validate: platform_takeover bool ok", ok is True and v is True)
ck("view: platform_takeover default false", cfg.build_view(FakeCtx({}))["approval"]["platform_takeover"] is False)
v = cfg.build_view(FakeCtx({"approval": {"platform_takeover": True}}))
ck("view: explicit platform_takeover true", v["approval"]["platform_takeover"] is True)
ck("view: explicit lists platform_takeover", "platform_takeover" in v["explicit"]["approval"])
# 修改类：fill 绝不播种 platform_takeover（A1 同款）
out = cfg.fill_approval_display(FakeCtx({}))
ck("fill: platform_takeover NOT seeded", "platform_takeover" not in out)
# fallback_builtin 探针（monkeypatch read_security_fallback）
_origfb = cfg.read_security_fallback
cfg.read_security_fallback = lambda: "builtin"
ck("view: fallback_builtin true", cfg.build_view(FakeCtx({}))["approval"]["fallback_builtin"] is True)
cfg.read_security_fallback = lambda: "deny"
ck("view: fallback_builtin false for deny", cfg.build_view(FakeCtx({}))["approval"]["fallback_builtin"] is False)
cfg.read_security_fallback = lambda: ""
ck("view: fallback_builtin false empty", cfg.build_view(FakeCtx({}))["approval"]["fallback_builtin"] is False)
cfg.read_security_fallback = _origfb
# 整块合并 platform_takeover 保未知键
ctx = FakeCtx({"approval": {"mood": "happy"}})
gui._CTX = ctx
gui._apply_setting("approval.platform_takeover", True)
ck("apply: platform_takeover merged", ctx.store["approval"].get("platform_takeover") is True)
ck("apply: platform_takeover unknown kept", ctx.store["approval"].get("mood") == "happy")

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)
