"""gateway-ui 文件总线 —— §5 姊妹契约（gateway-dashboard 纯消费）。

state.json 原子写（内容无变不重写，心跳 utime）+ events.jsonl 滚动 2000 条 +
writer_lease.json 静态 {pid}+mtime 心跳（硬件友好写盘）。
字段白名单：绝无消息正文/prompt/密钥。总线目录只许 <HERMES_HOME>/cache/gateway-ui/。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

EVENTS_KEEP = 2000


def write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class Bus:
    def __init__(self, bus_dir: Path, logger=None):
        self.dir = Path(bus_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self._lease_pid = None
        self.lease_path = self.dir / "writer_lease.json"
        self.pet_cfg_path = self.dir / "pet_cfg.json"
        self._lock = threading.Lock()
        self._logger = logger

    def _log(self, msg: str) -> None:
        try:
            if self._logger is not None:
                self._logger.info("gateway-ui: %s", msg)
        except Exception:
            pass

    def renew_writer_lease(self, pid: int) -> None:
        """写者租约（硬件友好写盘）：内容静态 {pid} 只在 pid 变化时写一次；
        心跳=os.utime mtime（读侧写者租约起本就按 mtime TTL 判）。"""
        try:
            if self._lease_pid != pid or not self.lease_path.is_file():
                write_json(self.lease_path, {"pid": pid})
                self._lease_pid = pid
            now = time.time()
            os.utime(self.lease_path, (now, now))
        except Exception:
            pass

    def touch_state(self) -> None:
        """state 心跳（硬件友好写盘）：内容无变化时不重写 JSON，只 utime mtime
        维持读侧 STATE_TTL（worker read_state 按 mtime 判失联）。"""
        try:
            now = time.time()
            os.utime(self.state_path, (now, now))
        except Exception:
            pass

    def append_event(self, row: dict) -> None:
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            if row.get("seq", 0) % 200 == 0:
                keep = self.events_path.read_text(encoding="utf-8").splitlines()[-EVENTS_KEEP:]
                tmp = self.events_path.with_name(self.events_path.name + ".tmp")
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                os.replace(tmp, self.events_path)

    def write_state(self, st: dict) -> None:
        try:
            write_json(self.state_path, st)
        except Exception as exc:
            self._log(f"state write failed: {exc}")

    def vlog(self, msg: str, enabled: bool, prefix: str = "GW") -> None:
        """verbose_log=true 才落盘（DIY 规范①）。400KB 滚动 2000 行。"""
        if not enabled:
            return
        try:
            f = self.dir / "debug.log"
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {prefix} {msg}\n")
            if f.stat().st_size > 400_000:
                keep = f.read_text(encoding="utf-8").splitlines()[-2000:]
                tmp = f.with_name("debug.log.tmp")
                tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                os.replace(tmp, f)
        except Exception:
            pass