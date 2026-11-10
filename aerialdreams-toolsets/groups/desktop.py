"""``desktop-utils`` group: clipboard read/write + screenshot (Windows)."""

from __future__ import annotations

import base64
import ctypes
import os
import struct
import subprocess
import time

from ..common import config
from ..common.util import err, ok

TOOLSET = "desktop-utils"
_PS_SHOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "common", "screenshot.ps1")


def _powershell(script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, timeout=timeout)


def _clipboard(args: dict) -> str:
    action = (args.get("action") or "read").strip().lower()
    if action == "read":
        r = _powershell("$t = Get-Clipboard -Raw; if ($null -eq $t) { $t = '' }; "
                        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t))")
        if r.returncode != 0:
            return err("Get-Clipboard 失败：%s" % (r.stderr or b"").decode("utf-8", "replace")[:300])
        b64 = (r.stdout or b"").decode("ascii", "ignore").strip()
        try:
            text = base64.b64decode(b64).decode("utf-8", "replace") if b64 else ""
        except Exception:
            text = ""
        return ok({"action": "read", "text": text, "chars": len(text)})
    if action == "write":
        text = args.get("text")
        if text is None:
            return err("write 需要 'text'")
        b64 = base64.b64encode(str(text).encode("utf-8")).decode("ascii")
        script = ("$t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')); "
                  "Set-Clipboard -Value $t" % b64)
        r = _powershell(script)
        if r.returncode != 0:
            return err("Set-Clipboard 失败：%s" % (r.stderr or b"").decode("utf-8", "replace")[:300])
        return ok({"action": "write", "chars": len(str(text))})
    return err("action 只能是 read/write")


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _foreground_rect() -> tuple:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed")
    return rect.left, rect.top, rect.right, rect.bottom


def _png_size(path: str) -> list:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", head[16:24])
            return [w, h]
    except Exception:
        pass
    return []


def _screenshot(args: dict) -> str:
    target = (args.get("target") or "full").strip().lower()
    if target not in ("full", "region", "window"):
        return err("target 只能是 full/region/window")
    out = args.get("path") or os.path.join(
        config.scratch_dir("shots"), "shot_%s_%d.png" % (target, int(time.time())))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    x = int(args.get("x") or 0)
    y = int(args.get("y") or 0)
    w = int(args.get("width") or 0)
    h = int(args.get("height") or 0)
    if target == "region" and (w <= 0 or h <= 0):
        return err("region 需要 x/y/width/height")
    ps_err = ""
    # Primary: PowerShell + System.Drawing (stdlib-only, DPI-aware).
    try:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", _PS_SHOT, "-Out", out, "-Mode", target]
        if target == "region":
            cmd += ["-X", str(x), "-Y", str(y), "-Width", str(w), "-Height", str(h)]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        if r.returncode == 0 and os.path.exists(out):
            return ok({"action": "screenshot", "target": target, "path": out,
                       "size": _png_size(out),
                       "kb": round(os.path.getsize(out) / 1024, 1)})
        ps_err = (r.stderr or b"").decode("utf-8", "replace")[-300:]
    except Exception as e:
        ps_err = "%s: %s" % (type(e).__name__, str(e)[:200])
    # Fallback: Pillow, present in the desktop gateway interpreter only.
    try:
        from PIL import ImageGrab
        _dpi_aware()
        bbox = None
        if target == "region":
            bbox = (x, y, x + w, y + h)
        elif target == "window":
            bbox = _foreground_rect()
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        img.save(out, "PNG")
        return ok({"action": "screenshot", "target": target, "path": out,
                   "size": list(img.size),
                   "kb": round(os.path.getsize(out) / 1024, 1)})
    except Exception as e:
        return err("screenshot 失败（PowerShell: %s；Pillow: %s: %s）"
                   % (ps_err or "n/a", type(e).__name__, str(e)[:200]))


_TOOLS = [
    ("clipboard", _clipboard, {
        "action": {"type": "string", "enum": ["read", "write"], "description": "read/write"},
        "text": {"type": "string", "description": "write: 要写入的文本"},
    }, ["action"],
     "读写 Windows 文本剪贴板。仅显式调用时动作。"),
    ("screenshot", _screenshot, {
        "target": {"type": "string", "enum": ["full", "region", "window"],
                   "description": "full=全屏; region=指定区域; window=当前前台窗口"},
        "x": {"type": "integer", "description": "region: 左上 x"},
        "y": {"type": "integer", "description": "region: 左上 y"},
        "width": {"type": "integer", "description": "region: 宽"},
        "height": {"type": "integer", "description": "region: 高"},
        "path": {"type": "string", "description": "输出 PNG 路径（默认 scratch）"},
    }, [],
     "截屏到 PNG（full/region/window），返回路径供 vision_analyze。仅显式调用时动作。"),
]


def register(ctx) -> None:
    for name, handler, props, required, desc in _TOOLS:
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, description=desc, emoji="🖥️")