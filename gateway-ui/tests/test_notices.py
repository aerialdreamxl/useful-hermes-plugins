# -*- coding: utf-8 -*-
"""test_notices.py — 事件反应 NoticeTracker 纯逻辑回归（worker/notices.py，无 Qt）。
宿主 python 或 side-env python 均可跑（纯 stdlib）。
- seq 去重 / 启动首轮不回放 / 过期忽略 / seq 回退不回放 / 未知 kind 不反应。
- reaction 映射 = PetWindow.play_reaction kind 约定。
"""
import sys
import time
from pathlib import Path

WORKER = Path(__file__).resolve().parent.parent / "worker"
sys.path.insert(0, str(WORKER))

from notices import NoticeTracker, REACTION_KINDS, DEFAULT_MAX_AGE_S

R = []
def ck(name, ok):
    R.append((name, bool(ok)))

NOW = [1000.0]
def now():
    return NOW[0]

def state(seq, kind="done", ts=None, platform="desktop"):
    return {"notice": {"seq": seq, "kind": kind,
                       "ts": NOW[0] if ts is None else ts, "platform": platform}}

# ============ 启动首轮不回放 ============
tr = NoticeTracker(now_fn=now)
ck("first feed with stale notice -> None (baseline only)",
   tr.feed(state(42, ts=NOW[0] - 500)) is None)
ck("baseline recorded", tr._last_seq == 42)
# 同 seq 再喂（state 每拍重写）→ 不重复反应
ck("same seq deduped", tr.feed(state(42)) is None)

# ============ 新 seq → 反应 ============
n = tr.feed(state(43, "done"))
ck("new seq -> notice", n is not None and n["seq"] == 43 and n["kind"] == "done")
ck("notice whitelist fields", set(n) == {"seq", "kind", "ts", "platform"})
ck("platform carried", n["platform"] == "desktop")
ck("last_notice kept", tr.last_notice == n)
ck("repeat same seq -> None", tr.feed(state(43, "done")) is None)

# ============ reaction 映射（play_reaction kind 约定）============
for kind in ("done", "failed", "await_input", "stopped"):
    tr2 = NoticeTracker(now_fn=now)
    tr2.feed(state(1))  # 基线
    n2 = tr2.feed(state(2, kind))
    ck(f"reaction {kind} mapped", n2 is not None and tr2.reaction_kind(n2) == kind)
ck("REACTION_KINDS covers 4 kinds", set(REACTION_KINDS) == {"done", "failed", "await_input", "stopped"})

# ============ 过期忽略 ============
tr3 = NoticeTracker(max_age_s=120.0, now_fn=now)
tr3.feed(state(1))
ck("fresh notice accepted", tr3.feed(state(2, ts=NOW[0] - 10)) is not None)
ck("expired notice ignored", tr3.feed(state(3, ts=NOW[0] - 121)) is None)
ck("expired still advances baseline", tr3._last_seq == 3)
ck("after expired, newer fresh accepted", tr3.feed(state(4, ts=NOW[0])) is not None)
ck("default max age 120s", DEFAULT_MAX_AGE_S == 120.0)

# ============ seq 回退（宿主重启归零）不回放 ============
tr4 = NoticeTracker(now_fn=now)
tr4.feed(state(50))
ck("seq regression -> None", tr4.feed(state(1)) is None)
ck("regression rebaselines", tr4._last_seq == 1)
ck("increment after regression accepted", tr4.feed(state(2)) is not None)

# ============ 畸形输入收敛 ============
tr5 = NoticeTracker(now_fn=now)
ck("non-dict state -> None", tr5.feed(None) is None)
ck("state without notice -> None", tr5.feed({"pet_state": "idle"}) is None)
ck("notice null -> None", tr5.feed({"notice": None}) is None)
ck("notice non-dict -> None", tr5.feed({"notice": "x"}) is None)
tr5.feed(state(1))
ck("bad seq -> None", tr5.feed({"notice": {"seq": "abc", "kind": "done", "ts": NOW[0]}}) is None)
ck("seq 0 -> None", tr5.feed({"notice": {"seq": 0, "kind": "done", "ts": NOW[0]}}) is None)
ck("unknown kind advances baseline no reaction",
   tr5.feed(state(2, "mystery")) is None and tr5._last_seq == 2)
ck("bad ts -> None", tr5.feed({"notice": {"seq": 3, "kind": "done", "ts": "x"}}) is None)
ck("reaction_kind(None) -> None", tr5.reaction_kind(None) is None)
ck("reaction_kind bad kind -> None", tr5.reaction_kind({"kind": "mystery"}) is None)

# ============ 连续事件流（模拟 tick 每拍喂 state）============
tr6 = NoticeTracker(now_fn=now)
tr6.feed(state(10))                      # 启动基线
got = []
for seq, kind in ((11, "done"), (11, "done"), (12, "await_input"),
                  (12, "await_input"), (13, "failed")):
    n6 = tr6.feed(state(seq, kind))
    if n6:
        got.append((n6["seq"], n6["kind"]))
ck("event stream deduped to 3 reactions",
   got == [(11, "done"), (12, "await_input"), (13, "failed")])

fails = [n for n, ok in R if not ok]
print(f"\nSUMMARY: {len(R)-len(fails)}/{len(R)} PASS" + ("" if not fails else f"  FAIL({len(fails)})"))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
print("PASS" if not fails else "FAIL")
sys.exit(1 if fails else 0)