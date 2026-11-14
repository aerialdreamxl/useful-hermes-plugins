# -*- coding: utf-8 -*-
"""真集成冒烟：宿主 Supervisor 真实 spawn side-env worker（复刻生产命令行），
验证 venv 蹦床场景下心跳判活、租约 token、优雅停止、无孤儿。
只用 scratch 总线；不碰真实总线/旧插件/config；worker 是本测试自己的孩子。
宿主 python 跑（约 30s）。"""
import glob
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
    print((" ok " if ok else " !! ") + name, flush=True)

from _env import HOME, scratch  # noqa: E402
BUS = scratch("gui22-integration")
BUS.mkdir(parents=True, exist_ok=True)
for f in ("worker_trace.log", "tray_lease.json", "worker_stderr.log", "tray_meta.json"):
    (BUS / f).unlink(missing_ok=True)

envs = glob.glob(str(Path(HOME) / "environments" / "gateway-*-qt" / "gen-*" / "venv" / "Scripts" / "python.exe"))
assert envs, "side-env not found"
ENV_PY = Path(envs[0])
print("side-env:", ENV_PY, flush=True)

sup = gui.Supervisor(
    bus_dir=BUS, worker_path=gui.WORKER_PATH,
    home=Path(HOME),
    repo=Path(HOME) / "hermes-agent",
    version_fn=lambda: "0.23.1",
    pet_cfg_fn=lambda: {"enable-pet": False, "slug": "", "scale": 0.7,
                        "roam": True, "topmost": True},
    tray_enabled_fn=lambda: True, index_url_fn=lambda: "",
    env_python_fn=lambda: ENV_PY)

# ---- spawn ----
t0 = time.time()
sup.step(t0)
ck("worker spawned", sup.worker is not None)
popen_pid = sup.worker.pid if sup.worker else -1
print("Popen.pid (trampoline):", popen_pid, flush=True)

# ---- 等首次心跳（worker 启动 + Qt 初始化约 3-8s）----
lease = None
for i in range(30):
    time.sleep(1.0)
    now = time.time()
    sup.step(now)
    try:
        lease = json.loads((BUS / "tray_lease.json").read_text(encoding="utf-8"))
    except Exception:
        lease = None
    if lease and lease.get("spawn_token") == sup.spawn_token:
        break
ck("lease appeared with our token", bool(lease) and lease.get("spawn_token") == sup.spawn_token)
real_pid = int(lease.get("pid") or 0) if lease else 0
print("worker self-reported pid:", real_pid, flush=True)
ck("trampoline confirmed (Popen.pid != real pid)", real_pid != popen_pid and real_pid > 0)

# ---- 心跳判活：持续 15s，健康 worker 不得被杀/重拉 ----
worker_handle = sup.worker
wedged = False
for i in range(15):
    time.sleep(1.0)
    sup.step(time.time())
    if sup.worker is not worker_handle:
        wedged = True
        break
ck("15s heartbeat: healthy worker kept (no false wedge)", not wedged)
ck("fails counter stays 0", sup.fails == 0)

# ---- trace 全阶段留证 ----
trace = (BUS / "worker_trace.log").read_text(encoding="utf-8") if (BUS / "worker_trace.log").exists() else ""
for stage in ("boot pid=", "app created", "tray shown", "tick start"):
    ck(f"trace stage: {stage}", stage in trace)

# ---- 优雅停止：stdin EOF 命锁 → 真实解释器自毁，无孤儿 ----
sup.shutdown()
time.sleep(3.0)
alive_real = gui.supervisor.pid_alive(real_pid)
ck("real interpreter exited after shutdown (no orphan)", not alive_real)
ck("trampoline handle reaped", sup.worker is None)

fails = [n for n, ok in R if not ok]
print("PASS" if not fails else "FAIL(" + str(len(fails)) + "): " + "; ".join(fails))
sys.exit(1 if fails else 0)