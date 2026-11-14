"""gateway-dashboard —— 单插件一体（宿主半 + 胶水半）。

胶水半：register(ctx) 订阅 pre_gateway_dispatch / on_session_start（VALID_HOOKS 内），
首个事件触发一次 kick——gateway:startup 不在插件白名单（plugins.py:111 实查），懒起是
唯一插件级合法路径，语义=有人用才养进程。
递归防线：spawn 时注入 HERMES_GATEWAY_DASHBOARD_CHILD=1；子进程再跑 register 见标即
return（web_server_dashboard._plugin_host 会惰性 discover_and_load，实查会跑到这里）。
宿主半：dashboard/plugin_api.py 供 /api/plugins/gateway-dashboard/* 挂载接缝 import。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("gateway-dashboard")

__all__ = ["register"]

_PLUGIN_ID = "gateway-dashboard"
_CHILD_ENV = "HERMES_GATEWAY_DASHBOARD_CHILD"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_DEFAULTS = {"dashboard_port": 9120, "host": "127.0.0.1", "skip_build": True,
             "isolated": True, "open_profile": "", "auto_restart": False}
_HANDSHAKE_TIMEOUT_S = 30.0
_HANDSHAKE_INTERVAL_S = 0.5
_STATE_PATH_HINT = ("cache", "gateway-fluent-gui")

_kicked = threading.Event()  # 进程内 once-flag（懒起只踢一次）


def _s(ctx, key):
    """settings 读取，缺失键按 _DEFAULTS 补（DIY 规范②逐键语义的读侧等价）。"""
    try:
        val = ctx.get_config(key, None)
        return _DEFAULTS[key] if val is None else val
    except Exception:
        return _DEFAULTS[key]


def _already_serving(host: str, port: int) -> bool:
    """目标端口已有真 dashboard（HTML 含 token 名）→ 复用不双起。只判存在性。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2.0) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
        return "__HERMES_SESSION_TOKEN__" in body
    except Exception:
        return False


def _dashboard_root() -> Path | None:
    try:
        import hermes_cli
        return Path(hermes_cli.__file__).resolve().parent.parent
    except Exception:
        return None


def _web_dist_present(root: Path) -> bool:
    return (root / "hermes_cli" / "web_dist" / "index.html").exists()


def _launch_cmd(settings: dict, root: Path) -> list[str] | None:
    host = str(settings["host"])
    try:
        port = int(settings["dashboard_port"])
    except (TypeError, ValueError):  # 面板可把 int 存成字符串脏值——干净拒启，不抛
        logger.warning("gateway-dashboard: port=%r 非整数，拒绝启动", settings["dashboard_port"])
        return None
    if host not in _LOOPBACK:  # §5 硬闸
        logger.warning("gateway-dashboard: host=%r 非 loopback，拒绝启动（§5）", host)
        return None
    if not isinstance(port, int) or port <= 0 or port > 65535:
        logger.warning("gateway-dashboard: port=%r 非法，拒绝启动", port)
        return None
    if settings["skip_build"] and not _web_dist_present(root):
        logger.error("gateway-dashboard: skip_build=true 但 web_dist/index.html 缺失；"
                     "请先手工构建前端，拒绝启动")
        return None
    # 必须用 console 版 python.exe + CREATE_NO_WINDOW（顶层"拥有"隐藏控制台，venv_sync
    # 重生链的 console 孙进程继承它，不闪窗）。pythonw 是反模式：GUI 进程无控制台，
    # 后代每次 spawn 都自配可见窗口——官方 _subprocess_compat.py:163 明说（bug #54220）。
    exe = str(Path(sys.executable))
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[: -len("pythonw.exe")] + "python.exe"
    cmd = [exe, "-m", "hermes_cli.main", "dashboard",
           "--port", str(port), "--host", host, "--no-open"]
    if settings["skip_build"]:
        cmd.append("--skip-build")
    if settings["isolated"]:
        cmd.append("--isolated")
    prof = str(settings.get("open_profile") or "").strip()
    if prof:
        cmd += ["--open-profile", prof]
    return cmd


def _spawn(cmd: list[str], root: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env[_CHILD_ENV] = "1"  # 递归标记：子进程里再进 register 时见标即退
    kwargs: dict = dict(cwd=str(root), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    if os.name == "nt":
        try:
            from hermes_cli._subprocess_compat import windows_detach_flags
            kwargs["creationflags"] = windows_detach_flags()
        except Exception:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(cmd, **kwargs)


def _write_dashboard_url(host: str, port: int) -> None:
    """就绪后回写总线 state.json.dashboard_url（合并写；gateway-fluent-gui 未装则静默跳过）。"""
    try:
        from hermes_cli.config import get_hermes_home
        state = Path(get_hermes_home()).joinpath(*_STATE_PATH_HINT) / "state.json"
        if not state.parent.exists():
            return
        data = {}
        if state.exists():
            data = json.loads(state.read_text(encoding="utf-8") or "{}")
        data["dashboard_url"] = f"http://{host}:{port}/"
        data["updated_ts"] = time.time()
        tmp = state.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, state)
    except Exception as exc:
        logger.debug("gateway-dashboard: dashboard_url 回写跳过: %s", exc)


def _wait_ready(host: str, port: int) -> bool:
    deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
    while time.monotonic() < deadline:
        if _already_serving(host, port):
            return True
        time.sleep(_HANDSHAKE_INTERVAL_S)
    return False


def _monitor(settings: dict, cmd: list[str], root: Path) -> None:
    """auto_restart=true：60s 后探测，两轮不可用补踢一次（一次即止防振荡，§4.2）。"""
    host, port = str(settings["host"]), int(settings["dashboard_port"])
    time.sleep(60.0)
    for _ in range(2):
        if _already_serving(host, port):
            return
        time.sleep(10.0)
    logger.info("gateway-dashboard: auto_restart 补踢一次")
    try:
        _spawn(cmd, root)
    except Exception:
        return
    for _ in range(3):
        time.sleep(10.0)
        if _already_serving(host, port):
            return


def _run(ctx) -> None:
    try:
        settings = {k: _s(ctx, k) for k in _DEFAULTS}
        try:
            settings["dashboard_port"] = int(settings["dashboard_port"])
        except (TypeError, ValueError):
            logger.warning("gateway-dashboard: port=%r 非整数，本次放弃", settings["dashboard_port"])
            return
        host, port = str(settings["host"]), int(settings["dashboard_port"])
        if _already_serving(host, port):
            logger.info("gateway-dashboard: %s:%s 已有实例，探测复用不双起", host, port)
            _write_dashboard_url(host, port)
            return
        root = _dashboard_root()
        if root is None:
            logger.warning("gateway-dashboard: 无法定位项目根，放弃")
            return
        cmd = _launch_cmd(settings, root)
        if cmd is None:
            return
        proc = _spawn(cmd, root)
        logger.info("gateway-dashboard: 踢起 dashboard 子进程 pid=%s cmd=%s", proc.pid, " ".join(cmd[1:]))
        if _wait_ready(host, port):
            logger.info("gateway-dashboard: 握手成功 http://%s:%s", host, port)
            _write_dashboard_url(host, port)
            if settings["auto_restart"]:
                threading.Thread(target=_monitor, args=(settings, cmd, root),
                                 daemon=True, name="gd-monitor").start()
        else:
            logger.warning("gateway-dashboard: %.0fs 握手超时（清 flag，下个事件可再踢）",
                           _HANDSHAKE_TIMEOUT_S)
            _kicked.clear()
    except Exception as exc:
        logger.warning("gateway-dashboard kick 异常（不影响 Gateway）: %s", exc, exc_info=True)


def _kick_once(ctx) -> None:
    if os.environ.get(_CHILD_ENV):
        return  # 递归防线：我已是 dashboard 子进程，不装胶水
    if _kicked.is_set():
        return
    _kicked.set()
    threading.Thread(target=_run, args=(ctx,), daemon=True,
                     name="gateway-dashboard-spawn").start()


def register(ctx) -> None:
    if os.environ.get(_CHILD_ENV):
        logger.debug("gateway-dashboard: dashboard 子进程内，拒绝加载（递归防线）")
        return
    ctx.register_hook("pre_gateway_dispatch", lambda **kw: _kick_once(ctx))
    ctx.register_hook("on_session_start", lambda **kw: _kick_once(ctx))
    logger.debug("gateway-dashboard: 胶水已就位（懒起：首个 gateway 事件触发）")
