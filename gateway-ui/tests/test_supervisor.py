# -*- coding: utf-8 -*-
"""A3/A4 根除回归：supervisor 心跳判活 + 永不放弃 + 单实例租约。
全 fake 依赖（不 spawn 真进程、不碰真实总线）。宿主 python 跑。"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_plugin

gui = load_plugin()
sup_mod = gui.supervisor
Supervisor = sup_mod.Supervisor

R = []
def ck(name, ok):
    R.append((name, bool(ok)))


from _env import scratch  # noqa: E402
BUS = scratch("gui22-suptest")
BUS.mkdir(parents=True, exist_ok=True)
LEASE = BUS / "tray_lease.json"


class FakeProc:
    def __init__(self, pid, alive=True):
        self.pid = pid
        self._alive = alive
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.stdin = None

    def poll(self):
        return None if self._alive else (self.returncode or 0)

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = -15

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def write_lease(pid, token, ts):
    # 硬件友好写盘：静态内容无 ts，心跳=文件 mtime（假时钟用 utime 设定）
    LEASE.write_text(json.dumps({"pid": pid, "owner_pid": 999,
                                 "spawn_token": token}), encoding="utf-8")
    os.utime(LEASE, (ts, ts))


ALIVE_PIDS = set()
_orig_pid_alive = sup_mod.pid_alive
sup_mod.pid_alive = lambda pid: pid in ALIVE_PIDS


def make_sup(**over):
    spawned = []

    def fake_spawn(pyexe, meta_path):
        p = FakeProc(424242 + len(spawned))
        spawned.append(p)
        ALIVE_PIDS.add(p.pid)
        return p

    kw = dict(bus_dir=BUS, worker_path=BUS / "fake_worker.py",
              home=BUS, repo=BUS, version_fn=lambda: "0.23.1",
              pet_cfg_fn=lambda: {"enable-pet": False},
              tray_enabled_fn=lambda: True,
              index_url_fn=lambda: "",
              env_python_fn=lambda: Path("fake_python.exe"),
              spawn_fn=fake_spawn, stop_event=threading.Event())
    kw.update(over)
    s = Supervisor(**kw)
    return s, spawned


def cleanup():
    for f in BUS.glob("tray_lease.json*"):
        f.unlink(missing_ok=True)


# ============ 1. 首拍 spawn ============
cleanup()
s, spawned = make_sup()
now = 1000.0
s.step(now)
ck("first tick spawns worker", len(spawned) == 1 and s.worker is spawned[0])
ck("spawn token issued", bool(s.spawn_token))
meta = json.loads((BUS / "tray_meta.json").read_text(encoding="utf-8"))
ck("meta carries token+pid", meta["spawn_token"] == s.spawn_token and meta["gateway_pid"] == os.getpid())

# ============ 2. 心跳健康 → 不重拉、失败清零 ============
s.fails = 3
write_lease(s.worker.pid, s.spawn_token, now + 1)
s.step(now + 1)
ck("healthy heartbeat: no respawn", len(spawned) == 1)
ck("healthy heartbeat clears fails", s.fails == 0)

# ============ 2b. 蹦床回归：lease.pid ≠ Popen.pid 但存活 → 健康（整体重写实测坑）============
s2b, spawned2b = make_sup()
s2b.step(1500.0)
trampoline_pid = spawned2b[0].pid          # 蹦床句柄 pid
real_pid = trampoline_pid + 90000          # 真实解释器 pid（≠句柄）
ALIVE_PIDS.add(real_pid)
write_lease(real_pid, s2b.spawn_token, 1501.0)
s2b.step(1501.0)
ck("trampoline: self-reported pid accepted", len(spawned2b) == 1)
write_lease(real_pid, s2b.spawn_token, 1519.0)  # 租约随 tick 续租（模拟 worker 心跳）
s2b.step(1520.0)  # 远超 WEDGE_S，若误判卡死这里会杀+重拉
ck("trampoline: healthy worker NOT wedged after 19s", len(spawned2b) == 1
   and not spawned2b[0].terminated)
# 真实 pid 死了（蹦床还活着）→ 心跳失败 → 卡死处置
ALIVE_PIDS.discard(real_pid)
write_lease(real_pid, s2b.spawn_token, 1532.5)
s2b.spawned_at = 1500.0
s2b.step(1533.0)
ck("trampoline: real pid dead -> wedged kill+respawn", len(spawned2b) == 2)

# ============ 3. token 不匹配 ≠ 健康（旧 worker 幽灵心跳）============
write_lease(s.worker.pid, "stale-token", now + 2)
s.spawned_at = now - 100  # 超出宽限期
s.step(now + 2)
ck("token mismatch -> wedged kill", spawned[0].terminated)
ck("token mismatch -> respawn", len(spawned) == 2)

# ============ 4. A4：进程活但租约陈旧 >12s = 卡死 → kill+重拉 ============
s2, spawned2 = make_sup()
s2.step(2000.0)
write_lease(s2.worker.pid, s2.spawn_token, 2000.0)
s2.step(2005.0)   # 租约 5s 陈旧，宽限期内 → 等
ck("grace period: no kill yet", len(spawned2) == 1 and not spawned2[0].terminated)
s2.step(2013.0)   # 租约 13s 陈旧 > WEDGE_S → 卡死处置
ck("A4: wedged worker killed", spawned2[0].terminated)
ck("A4: wedged worker respawned", len(spawned2) == 2)

# ============ 5. worker 进程死亡 → 重拉 ============
s3, spawned3 = make_sup()
s3.step(3000.0)
spawned3[0]._alive = False
spawned3[0].returncode = 1
s3.step(3001.0)
ck("dead worker respawned", len(spawned3) == 2)

# ============ 6. A3：新鲜租约+活 pid（无论宿主）→ 不 spawn ============
cleanup()
s4, spawned4 = make_sup()
ALIVE_PIDS.add(777)
write_lease(777, "other-host-token", 4000.0)
s4.step(4000.5)
ck("A3: fresh lease held -> no spawn", len(spawned4) == 0)
# 陈旧租约（持有者已死）→ 接手
write_lease(777, "other-host-token", 3980.0)
s4.step(4000.5)
ck("stale lease -> takeover spawn", len(spawned4) == 1)
# 租约新鲜但 pid 已死 → 接手
cleanup()
s5, spawned5 = make_sup()
write_lease(888, "dead-pid-token", 5000.0)  # 888 不在 ALIVE_PIDS
s5.step(5000.5)
ck("fresh lease but dead pid -> spawn", len(spawned5) == 1)

# ============ 7. tray_enabled=false → 杀 worker 但线程不退（等开回）============
flag = {"on": True}
s6, spawned6 = make_sup(tray_enabled_fn=lambda: flag["on"])
s6.step(6000.0)
ck("spawned while enabled", len(spawned6) == 1)
flag["on"] = False
s6.step(6001.0)
ck("disabled: worker killed", spawned6[0].terminated and s6.worker is None)
s6.step(6002.0)
ck("disabled: still no spawn (loop alive)", len(spawned6) == 1)
flag["on"] = True
s6.step(6003.0)
ck("re-enabled: spawn again (no permanent give-up)", len(spawned6) == 2)

# ============ 8. 失败退避：3→6→12→30 封顶，永不放弃 ============
def boom(pyexe, meta_path):
    raise RuntimeError("env install failed")

s7, _ = make_sup(spawn_fn=boom)
t = 7000.0
s7.step(t)          # 首败 fails=1，退避 6s
ck("first failure counted", s7.fails == 1)
s7.step(t + 3)      # 退避中
ck("backoff: no retry at +3s", s7.fails == 1)
s7.step(t + 7)      # 6s 过 → 再试再败 fails=2，退避 12s
ck("retry after backoff", s7.fails == 2)
s7.step(t + 10)
ck("backoff grows: no retry at +10s", s7.fails == 2)
s7.step(t + 20)     # fails=3 退避 24s
ck("third failure", s7.fails == 3)
s7.step(t + 50)     # fails=4 退避 30s 封顶
ck("fourth failure", s7.fails == 4)
s7.step(t + 70)     # 20s < 30 封顶 → 不试
ck("cap 30s honored", s7.fails == 4)
s7.step(t + 85)     # 35s > 30 → 再试（第 5 败也不放弃！）
ck("A4: never gives up after 5 failures", s7.fails == 5)

# ============ 9. run() 响应 stop_event ============
s8, spawned8 = make_sup()
s8.stop_event.set()
s8.run()
ck("run() exits on stop_event", True)

sup_mod.pid_alive = _orig_pid_alive
fails = [n for n, ok in R if not ok]
print("PASS" if not fails else "FAIL(" + str(len(fails)) + "): " + "; ".join(fails))
for n, ok in R:
    print((" ok " if ok else " !! ") + n)
sys.exit(1 if fails else 0)