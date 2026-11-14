"""gateway-ui worker 监管 —— A3/A4 根除：心跳判活 + 永不放弃。

旧版三宗罪（交接现状 A3/A4 实锤）：
1. 只查 poll()（进程活着=健康）——卡死在启动期的 worker 永远不被重拉，托盘永久消失。
   现判活 = 进程活 且 tray_lease 新鲜(<8s) 且 pid/token 双匹配；进程活但租约陈旧
   >12s = 卡死 → terminate→kill→重拉。worker 侧另有看门狗自毁兜底（契约§4）。
2. 「连续 5 败永久放弃」+ tray_enabled=false 分支 return 终止线程。
   现循环永不退出：失败指数退避 3→6→12→30s 封顶，健康心跳即清零；
   tray_enabled=false 只杀 worker 继续等开回。
3. 租约自检「同宿主陈旧租约自家接手」放行同宿主双 worker（A3 套娃）。
   现 supervisor spawn 前查租约：新鲜+pid 活=有 worker 持托盘（无论哪个宿主）→不 spawn；
   worker 启动自检同款无条件让位（worker/main.py 实现）。陈旧租约=持有者已死→接手。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

LEASE_FRESH_S = 8.0      # 租约新鲜窗（与 worker 自检一致）
WEDGE_S = 12.0           # 进程活但租约陈旧超过此值=卡死
BACKOFF_BASE_S = 3.0
BACKOFF_CAP_S = 30.0
ENV_NAME = "gateway-ui-qt"
ENV_REQS = ["PySide6-Essentials", "PySide6-Fluent-Widgets", "Pillow"]


def pid_alive(pid: int) -> bool:
    if not pid or pid == os.getpid():
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def lease_held(lease_path: Path, now: float, exclude_pid: int = 0) -> bool:
    """有活 worker 持托盘（新鲜+pid 活）。陈旧租约=持有者已死/卡死，可接手。
    不区分 owner——同宿主双 worker 正是 A3 根因。
    exclude_pid：自家刚 terminate 的 worker——其残留租约在 pid 复用窗口内
    可能仍"新鲜+活着"，不得挡住自家重拉。"""
    try:
        d = json.loads(lease_path.read_text(encoding="utf-8"))
        pid = int(d.get("pid") or 0)
        # 硬件友好写盘：心跳优先 mtime（静态租约无 ts）；无 mtime 回落旧 ts 字段
        hb = lease_path.stat().st_mtime or float(d.get("ts") or 0)
        if not pid or pid == exclude_pid or now - hb > LEASE_FRESH_S:
            return False
        return pid_alive(pid)
    except Exception:
        return False


class Supervisor:
    """worker 生命周期监管。deps 注入便于测试（test_supervisor.py 全 fake）。"""

    def __init__(self, *, bus_dir: Path, worker_path: Path, home: Path, repo: Path,
                 version_fn, pet_cfg_fn, tray_enabled_fn, index_url_fn,
                 env_python_fn=None, spawn_fn=None, logger=None, vlog=None,
                 stop_event=None, tick_s: float = 1.0):
        self.bus_dir = Path(bus_dir)
        self.worker_path = Path(worker_path)
        self.home = Path(home)
        self.repo = Path(repo)
        self.version_fn = version_fn
        self.pet_cfg_fn = pet_cfg_fn
        self.tray_enabled_fn = tray_enabled_fn
        self.index_url_fn = index_url_fn
        self.env_python_fn = env_python_fn or self._default_env_python
        self.spawn_fn = spawn_fn or self._default_spawn
        self.logger = logger
        self.vlog = vlog or (lambda msg: None)
        self.stop_event = stop_event
        self.tick_s = tick_s
        self.worker = None
        self.spawn_token = None
        self.fails = 0
        self.spawned_at = 0.0
        self._last_killed_pid = 0
        self._meta_path = self.bus_dir / "tray_meta.json"
        self._lease_path = self.bus_dir / "tray_lease.json"

    # ------------------------------------------------------------ 日志 ----
    def _log(self, msg: str) -> None:
        with contextlib.suppress(Exception):
            if self.logger is not None:
                self.logger.info("gateway-ui: %s", msg)

    # ------------------------------------------------------------ 环境 ----
    def _default_env_python(self) -> Path:
        from pm import ensure_environment, environment_python
        found = environment_python(ENV_NAME)
        if found and Path(found).is_file():
            return Path(found)
        old = os.environ.get("UV_INDEX_URL")
        url = self.index_url_fn()
        try:
            if url:
                os.environ["UV_INDEX_URL"] = url
            ensure_environment(ENV_NAME, ENV_REQS, explicit=True, timeout=1500)
        finally:
            if old is None:
                os.environ.pop("UV_INDEX_URL", None)
            else:
                os.environ["UV_INDEX_URL"] = old
        found = environment_python(ENV_NAME)
        if not found:
            raise RuntimeError("pm returned no interpreter")
        return Path(found)

    # ------------------------------------------------------------ meta ----
    def _write_meta(self) -> Path:
        import sys
        self.bus_dir.mkdir(parents=True, exist_ok=True)
        keep = ("PATH", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC")
        meta = {
            "gateway_pid": os.getpid(),
            "spawn_token": self.spawn_token,
            "bus_dir": str(self.bus_dir),
            "state_path": str(self.bus_dir / "state.json"),
            "log_dir": str(self.home / "logs"),
            "hermes_home": str(self.home),
            "repo": str(self.repo),
            "version": self.version_fn(),
            "pet": self.pet_cfg_fn(),
            "restart_cmd": [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"],
            "stop_cmd": [sys.executable, "-m", "hermes_cli.main", "gateway", "stop"],
            "cwd": str(self.repo),
            "env": {**{k: v for k, v in os.environ.items() if k in keep},
                    "PYTHONPATH": str(self.repo)},
        }
        tmp = self._meta_path.with_name("tray_meta.json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._meta_path)
        return self._meta_path

    def _default_spawn(self, pyexe: Path, meta_path: Path):
        err_log = self.bus_dir / "worker_stderr.log"
        with contextlib.suppress(Exception):
            if err_log.is_file() and err_log.stat().st_size > 2_000_000:
                keep = err_log.read_bytes()[-500_000:]
                err_log.write_bytes(keep)
        fh = open(err_log, "ab")
        return subprocess.Popen(
            [str(pyexe), "-I", "-X", "utf8=1", str(self.worker_path),
             "--meta", str(meta_path)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=fh)

    # ------------------------------------------------------------ 判活 ----
    def _heartbeat_ok(self, now: float) -> bool:
        """租约新鲜 + token 匹配 + 自报 pid 存活。
        **venv 蹦床坑（整体重写实测）**：side-env python.exe 是 launcher，
        Popen.pid 是蹦床进程，真实解释器是另一个 pid——worker 租约自报的
        os.getpid() 才是权威身份，绝不能拿 Popen.pid 比对（否则心跳永远失配，
        健康 worker 每 12s 被误判卡死杀掉）。蹦床句柄只用于 stdin 命锁与退出探测。"""
        if self.worker is None or self.worker.poll() is not None:
            return False
        try:
            d = json.loads(self._lease_path.read_text(encoding="utf-8"))
            pid = int(d.get("pid") or 0)
            hb = self._lease_path.stat().st_mtime or float(d.get("ts") or 0)
            return (d.get("spawn_token") == self.spawn_token
                    and now - hb <= LEASE_FRESH_S
                    and pid_alive(pid))
        except Exception:
            return False

    def _kill_worker(self) -> None:
        w = self.worker
        self.worker = None
        if w is None:
            return
        # 真实解释器 pid 从租约读（蹦床坑）；先 stdin EOF 命锁优雅自毁，
        # 再 terminate 蹦床句柄兜底（worker 命锁线程收到 EOF 即 os._exit）。
        real_pid = 0
        with contextlib.suppress(Exception):
            d = json.loads(self._lease_path.read_text(encoding="utf-8"))
            real_pid = int(d.get("pid") or 0)
        self._last_killed_pid = real_pid or w.pid
        with contextlib.suppress(Exception):
            if w.stdin:
                w.stdin.close()
        with contextlib.suppress(Exception):
            if w.poll() is None:
                w.terminate()
                try:
                    w.wait(timeout=3)
                except Exception:
                    w.kill()

    # ------------------------------------------------------------ 主循环 ----
    def step(self, now: float) -> None:
        """单拍（测试直接调）。生产 run() 以 tick_s 周期调用。"""
        if not self.tray_enabled_fn():
            if self.worker is not None:
                self._log("tray_enabled=false -> worker stopped (supervisor keeps waiting)")
                self._kill_worker()
            return
        if self.worker is not None:
            if self.worker.poll() is not None:
                self._log(f"worker pid={self.worker.pid} exited rc={self.worker.returncode}")
                self.worker = None
            elif self._heartbeat_ok(now):
                if self.fails:
                    self._log(f"worker healthy after {self.fails} failures, counter reset")
                self.fails = 0
                return
            elif now - self.spawned_at > WEDGE_S:
                self._log(f"worker pid={self.worker.pid} WEDGED (alive, no heartbeat "
                          f">{WEDGE_S:.0f}s) -> kill+respawn")
                self.vlog(f"supervisor: wedged worker {self.worker.pid} killed")
                self._kill_worker()
            else:
                return  # 启动宽限期内，等首次心跳
        if self.worker is None and lease_held(self._lease_path, now,
                                              exclude_pid=self._last_killed_pid):
            return  # 另一宿主的 worker 持托盘，本进程只跑总线
        if self.fails and now - self.spawned_at < min(BACKOFF_CAP_S,
                                                      BACKOFF_BASE_S * (2 ** self.fails)):
            return  # 退避中
        try:
            pyexe = self.env_python_fn()
            self.spawn_token = uuid.uuid4().hex
            meta_path = self._write_meta()
            self.worker = self.spawn_fn(pyexe, meta_path)
            self.spawned_at = now
            self._log(f"worker spawned pid={self.worker.pid} token={self.spawn_token[:8]}")
            self.vlog(f"spawn worker pid={self.worker.pid}")
        except Exception as exc:
            self.fails += 1
            self.spawned_at = now
            self._log(f"spawn failed (fails={self.fails}, backoff "
                      f"{min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** self.fails)):.0f}s): {exc}")

    def run(self) -> None:
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                break
            with contextlib.suppress(Exception):
                self.step(time.time())
            if self.stop_event is not None:
                if self.stop_event.wait(self.tick_s):
                    break
            else:
                time.sleep(self.tick_s)
        self._kill_worker()

    def shutdown(self) -> None:
        self._kill_worker()