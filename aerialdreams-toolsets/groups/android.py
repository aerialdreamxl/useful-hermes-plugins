"""``android-utils`` group: adb + fastboot wrappers (system tools, no deps)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from ..common import config
from ..common.util import err, ok

TOOLSET = "android-utils"


def _adb_bin() -> str:
    return os.environ.get("AERIALDREAMS_ADB") or shutil.which("adb") or "adb"


def _fb_bin() -> str:
    return os.environ.get("AERIALDREAMS_FASTBOOT") or shutil.which("fastboot") or "fastboot"


def _run(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _tail(r: subprocess.CompletedProcess, n: int = 2000) -> str:
    out = (r.stdout or b"").decode("utf-8", "replace")
    errb = (r.stderr or b"").decode("utf-8", "replace")
    text = (out + ("\n" + errb if errb.strip() else "")).strip()
    return text[-n:]


def _adb(args: dict) -> str:
    action = (args.get("action") or "devices").strip().lower()
    adb = _adb_bin()
    if not os.path.exists(adb) and not shutil.which(adb):
        return err("未找到 adb（PATH，或用 AERIALDREAMS_ADB 指向可执行文件）")
    serial = (args.get("serial") or "").strip()
    base = [adb] + (["-s", serial] if serial else [])
    timeout = min(max(int(args.get("timeout") or 120), 5), 600)
    try:
        if action == "devices":
            r = _run([adb, "devices", "-l"], timeout=30)
            rows = []
            for line in (r.stdout or b"").decode("utf-8", "replace").splitlines()[1:]:
                line = line.strip()
                if line:
                    parts = line.split()
                    rows.append({"serial": parts[0],
                                 "state": parts[1] if len(parts) > 1 else "",
                                 "detail": " ".join(parts[2:])})
            return ok({"action": "devices", "count": len(rows), "devices": rows})
        if action == "shell":
            cmd = args.get("command") or args.get("args") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if not cmd:
                return err("shell 需要 'command'")
            r = _run(base + ["shell", cmd], timeout=timeout)
        elif action == "push":
            local, remote = args.get("local"), args.get("remote")
            if not local or not remote:
                return err("push 需要 'local' 与 'remote'")
            r = _run(base + ["push", local, remote], timeout=timeout)
        elif action == "pull":
            remote, local = args.get("remote"), args.get("local")
            if not remote or not local:
                return err("pull 需要 'remote' 与 'local'")
            r = _run(base + ["pull", remote, local], timeout=timeout)
        elif action == "logcat":
            n = int(args.get("lines") or 200)
            r = _run(base + ["logcat", "-d", "-t", str(n)], timeout=timeout)
        elif action == "screenshot":
            out = args.get("path") or os.path.join(
                config.scratch_dir("android"), "adb_shot_%d.png" % int(time.time()))
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            r = _run(base + ["exec-out", "screencap", "-p"], timeout=timeout)
            blob = r.stdout or b""
            if r.returncode != 0 or not blob.startswith(b"\x89PNG"):
                return err("screencap 失败：%s" % _tail(r, 400))
            with open(out, "wb") as f:
                f.write(blob)
            return ok({"action": "screenshot", "path": out,
                       "kb": round(len(blob) / 1024, 1)})
        elif action == "install":
            apk = args.get("apk") or args.get("local")
            if not apk:
                return err("install 需要 'apk'")
            r = _run(base + ["install", "-r", apk], timeout=max(timeout, 300))
        elif action == "uninstall":
            pkg = args.get("package")
            if not pkg:
                return err("uninstall 需要 'package'")
            r = _run(base + ["uninstall", pkg], timeout=timeout)
        elif action == "props":
            r = _run(base + ["shell", "getprop"], timeout=timeout)
        elif action in ("am", "pm"):
            cmd = args.get("command") or args.get("args") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if not cmd:
                return err("%s 需要 'command'" % action)
            r = _run(base + ["shell", action, cmd], timeout=timeout)
        else:
            return err("action 不支持：" + action)
        return ok({"action": action, "exit_code": r.returncode,
                   "output": _tail(r)})
    except subprocess.TimeoutExpired:
        return err("%s 超时（%ss）" % (action, timeout))
    except Exception as e:
        return err("adb %s: %s: %s" % (action, type(e).__name__, str(e)[:300]))


def _fastboot(args: dict) -> str:
    action = (args.get("action") or "devices").strip().lower()
    fb = _fb_bin()
    if not os.path.exists(fb) and not shutil.which(fb):
        return err("未找到 fastboot（PATH，或用 AERIALDREAMS_FASTBOOT 指向可执行文件）")
    serial = (args.get("serial") or "").strip()
    base = [fb] + (["-s", serial] if serial else [])
    timeout = min(max(int(args.get("timeout") or 120), 5), 900)
    try:
        if action == "devices":
            r = _run([fb, "devices"], timeout=30)
            rows = []
            for line in (r.stdout or b"").decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line:
                    parts = line.split()
                    rows.append({"serial": parts[0],
                                 "state": parts[1] if len(parts) > 1 else ""})
            return ok({"action": "devices", "count": len(rows), "devices": rows})
        if action == "flash":
            partition = (args.get("partition") or "").strip()
            image = (args.get("image") or "").strip()
            if not partition or not image:
                return err("flash 需要 'partition' 与 'image'")
            if str(args.get("confirm")).lower() not in ("true", "1", "yes"):
                return err("刷机是高危操作：确认目标分区与镜像后传 confirm=true 再执行")
            if not os.path.exists(image):
                return err("镜像不存在：%s" % image)
            r = _run(base + ["flash", partition, image], timeout=max(timeout, 600))
        elif action == "reboot":
            target = (args.get("target") or "").strip()
            r = _run(base + (["reboot", target] if target else ["reboot"]), timeout=60)
        elif action == "oem":
            cmd = args.get("command") or ""
            if not cmd:
                return err("oem 需要 'command'（如 unlock / device-info）")
            if str(args.get("confirm")).lower() not in ("true", "1", "yes"):
                return err("oem 是高危操作：确认命令后传 confirm=true")
            r = _run(base + ["oem", cmd], timeout=timeout)
        else:
            return err("action 不支持：" + action)
        return ok({"action": action, "exit_code": r.returncode, "output": _tail(r)})
    except subprocess.TimeoutExpired:
        return err("fastboot %s 超时（%ss）" % (action, timeout))
    except Exception as e:
        return err("fastboot %s: %s: %s" % (action, type(e).__name__, str(e)[:300]))


_TOOLS = [
    ("androidadb", _adb, {
        "action": {"type": "string",
                   "enum": ["devices", "shell", "push", "pull", "logcat",
                            "screenshot", "install", "uninstall", "props", "am", "pm"],
                   "description": "设备/命令/文件/日志/截图/安装"},
        "serial": {"type": "string", "description": "多设备时指定序列号"},
        "command": {"type": "string", "description": "shell/am/pm 的命令"},
        "local": {"type": "string", "description": "push/pull 本地路径"},
        "remote": {"type": "string", "description": "push/pull 设备路径"},
        "apk": {"type": "string", "description": "install: apk 路径"},
        "package": {"type": "string", "description": "uninstall: 包名"},
        "lines": {"type": "integer", "description": "logcat: 行数，默认 200"},
        "path": {"type": "string", "description": "screenshot: 输出 PNG 路径"},
        "timeout": {"type": "integer", "description": "超时秒数（默认 120）"},
    }, ["action"],
     "Android 调试桥：devices/shell/push/pull/logcat/screenshot/install/uninstall/props/am/pm。多设备用 serial。"),
    ("androidfb", _fastboot, {
        "action": {"type": "string", "enum": ["devices", "flash", "reboot", "oem"],
                   "description": "fastboot 操作"},
        "serial": {"type": "string", "description": "多设备时指定序列号"},
        "partition": {"type": "string", "description": "flash: 分区名，如 boot/system/recovery"},
        "image": {"type": "string", "description": "flash: 镜像绝对路径"},
        "target": {"type": "string", "description": "reboot: bootloader/recovery 等"},
        "command": {"type": "string", "description": "oem: 子命令"},
        "confirm": {"type": "boolean", "description": "flash/oem 必须显式 true 才执行"},
        "timeout": {"type": "integer", "description": "超时秒数（默认 120）"},
    }, ["action"],
     "FastBoot：devices/flash/reboot/oem。flash 必须显式 partition+image+confirm=true，无默认目标。"),
]


def register(ctx) -> None:
    for name, handler, props, required, desc in _TOOLS:
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, description=desc, emoji="📱")