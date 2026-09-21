"""Offline simulation of smart-terminal-cwd's pre_llm_call drift note."""
import importlib.util
import pathlib
import sys
import types

plugin_dir = pathlib.Path(r"D:/01-AI/Hermes-Agent/Data/plugins/smart-terminal-cwd")
spec = importlib.util.spec_from_file_location("stc", plugin_dir / "__init__.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("module OK")

NEST = "D:/01-AI/Hermes-Agent/Nest"
HOME = "C:/Users/AerialdreamXL"

m._pinned["sess1"] = NEST
fake_rcwd = types.ModuleType("agent.runtime_cwd")
fake_rcwd.resolve_agent_cwd = lambda: HOME
sys.modules["agent.runtime_cwd"] = fake_rcwd
m._session_key = lambda task_id="": task_id or "default"

hook = m._make_pre_llm_call(None)

r1 = hook(task_id="sess1", conversation_history=[1, 2, 3])
assert r1 and NEST in r1["context"], r1
print("T1 drift -> note emitted")
r2 = hook(task_id="sess1", conversation_history=[1, 2, 3, 4])
assert r2 is None, r2
print("T2 same drift -> suppressed (cadence)")
r3 = hook(task_id="sess1", conversation_history=[1])
assert r3 is not None, "history shrink should re-issue"
print("T3 history shrank -> note re-issued")
m._pinned["sess1"] = "D:/01-AI/Hermes-Agent/Data"
r4 = hook(task_id="sess1", conversation_history=[1, 2])
assert r4 and "/Data" in r4["context"], r4
print("T4 pin changed -> note updated")
fake_rcwd.resolve_agent_cwd = lambda: "D:/01-AI/Hermes-Agent/Data"
r5 = hook(task_id="sess1", conversation_history=[1, 2, 3])
assert r5 is None, r5
print("T5 channels agree -> silent")
assert hook(task_id="other", conversation_history=[]) is None
print("T6 unmanaged session -> silent")

# cadence: fresh state, first turn emits, turns 2-9 silent, turn 10 re-emits
fake_rcwd.resolve_agent_cwd = lambda: HOME
m._cwd_note_sent.pop("sess1", None)
emits = [hook(task_id="sess1", conversation_history=list(range(i))) is not None
         for i in range(1, 12)]
print("T7 emit turns:", [i + 1 for i, e in enumerate(emits) if e])
assert emits[0] and not any(emits[1:10]) and emits[10]

# backslash vs forward-slash equivalence
fake_rcwd.resolve_agent_cwd = lambda: "D:\\01-AI\\Hermes-Agent\\Data"
m._cwd_note_sent.pop("sess1", None)
r = hook(task_id="sess1", conversation_history=[1])
assert r is None, "backslash/forward-slash same dir must count as agreement"
print("T8 path separator equivalence -> silent")

print("ALL PASS")
