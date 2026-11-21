"""``android-utils`` group: adb + fastboot wrappers (system tools, no deps)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from ..common import config
from ..common.util import err, ok, safe_output_path

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


def _confirmed(args: dict) -> bool:
    """高危动作的确认门槛，与 fastboot flash/oem 同一套写法。"""
    return str(args.get("confirm")).lower() in ("true", "1", "yes")


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
            if not _confirmed(args):
                return err("shell 是高危操作（等于在设备上执行任意命令）：仅在用户明确指令下、确认命令内容后传 confirm=true 再执行（confirm 是模型自设参数，宿主不代审批）")
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
            local, path_error = safe_output_path(local, config.scratch_dir(), "pull local")
            if path_error:
                return err(path_error)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            r = _run(base + ["pull", remote, local], timeout=timeout)
        elif action == "logcat":
            n = int(args.get("lines") or 200)
            r = _run(base + ["logcat", "-d", "-t", str(n)], timeout=timeout)
        elif action == "screenshot":
            # 毫秒时间戳：同一秒连拍不再互相覆盖
            out = args.get("path") or os.path.join(
                config.scratch_dir("android"), "adb_shot_%d.png" % int(time.time() * 1000))
            out, path_error = safe_output_path(out, config.scratch_dir(), "screenshot path")
            if path_error:
                return err(path_error)
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            r = _run(base + ["exec-out", "screencap", "-p"], timeout=timeout)
            blob = r.stdout or b""
            if r.returncode != 0 or not blob.startswith(b"\x89PNG"):
                return err("screencap 失败：%s" % _tail(r, 400))
            try:
                with open(out, "xb") as f:
                    f.write(blob)
            except FileExistsError:
                return err("screenshot path 已存在，拒绝覆盖")
            return ok({"action": "screenshot", "path": out,
                       "kb": round(len(blob) / 1024, 1)})
        elif action == "install":
            apk = args.get("apk") or args.get("local")
            if not apk:
                return err("install 需要 'apk'")
            if not _confirmed(args):
                return err("install 是高危操作（会覆盖安装/替换签名）：仅在用户明确指令下、确认 apk 与设备后传 confirm=true 再执行（confirm 是模型自设参数，宿主不代审批）")
            r = _run(base + ["install", "-r", apk], timeout=max(timeout, 300))
        elif action == "uninstall":
            pkg = args.get("package")
            if not pkg:
                return err("uninstall 需要 'package'")
            if not _confirmed(args):
                return err("uninstall 是高危操作（会连同应用数据一起删除，不可撤销）：仅在用户明确指令下、确认包名与设备后传 confirm=true 再执行（confirm 是模型自设参数，宿主不代审批）")
            r = _run(base + ["uninstall", pkg], timeout=timeout)
        elif action == "props":
            r = _run(base + ["shell", "getprop"], timeout=timeout)
        elif action in ("am", "pm"):
            cmd = args.get("command") or args.get("args") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if not cmd:
                return err("%s 需要 'command'" % action)
            if not _confirmed(args):
                return err("%s 是高危操作（启动/强停/改权限/卸载组件）：仅在用户明确指令下、确认命令后传 confirm=true 再执行（confirm 是模型自设参数，宿主不代审批）" % action)
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
                return err("刷机是高危操作：仅在用户明确指令下、确认目标分区与镜像后传 confirm=true 再执行（confirm 是模型自设参数，宿主不代审批）")
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
                return err("oem 是高危操作：仅在用户明确指令下、确认命令后传 confirm=true（confirm 是模型自设参数，宿主不代审批）")
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
        "local": {"type": "string", "description": "push: 本地文件；pull: scratch 目录内本地路径（越界/已存在拒绝）"},
        "remote": {"type": "string", "description": "push/pull 设备路径"},
        "apk": {"type": "string", "description": "install: apk 路径"},
        "package": {"type": "string", "description": "uninstall: 包名"},
        "lines": {"type": "integer", "description": "logcat: 行数，默认 200"},
        "path": {"type": "string", "description": "screenshot: 输出 PNG 路径（限 scratch 目录内，拒绝覆盖）"},
        "confirm": {"type": "boolean", "description": "shell/am/pm/install/uninstall 必须显式 true 才执行"},
        "timeout": {"type": "integer", "description": "超时秒数（默认 120）"},
    }, ["action"],
     "Android 调试桥：devices/shell/push/pull/logcat/screenshot/install/uninstall/props/am/pm。多设备用 serial。shell/am/pm/install/uninstall 必须显式 confirm=true。"),
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