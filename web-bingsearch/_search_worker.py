"""Bing HTML child-process entrypoint, run as ``python _search_worker.py``.

One-shot worker for both channels of the plugin (v1.4):

* ``{"op": "search", "queue": str, "settings": {...}}``
  -> ``{"ok": true, "results": [...], "debug": {...}}`` / ``{"ok": false, "error": str}``
* ``{"op": "extract", "url": str, "settings": {...}}``
  -> ``{"ok": true, "raw_content": str}`` / ``{"ok": false, "error": str}``

A missing ``op`` means ``search`` (v1.3 wire format still accepted). Reads its one
JSON request from stdin, writes one envelope to stdout, exits.

Why a child process at all: primp blocks in native code while holding the GIL, so a
slow Bing page or a slow extract target could freeze the whole gateway. The parent
(runHtmlSearchBounded / runExtractBounded) only ever reads stdout and kills this
process on a hard wall-clock deadline (terminate -> kill).

The plugin directory name has a hyphen (web-bingsearch) so it cannot be imported
as a normal package; we load __init__.py by file path via importlib. Settings are
forwarded from the parent through a tiny shim so the child honors the user's
stealth_headers / warm_homepage switches (the child has no live PluginContext).
``primp`` is imported inside the plugin functions, i.e. only ever in here — the
gateway process never imports it on the extract path.

Test hooks (``"test_hook": "sleep"|"gil"|"empty"``) are honored only when
``HERMES_BING_ALLOW_TEST_HOOKS=1`` — used to prove the parent can kill a
GIL-holding child (acceptance: subprocess isolation).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time


def _hold_gil(secs: int) -> None:
    """Block in a foreign call that keeps the GIL — mirrors native primp.
    ctypes.PyDLL (unlike CDLL/WinDLL) does not release the GIL."""
    import ctypes
    if sys.platform == "win32":
        sleep, secs = ctypes.PyDLL("kernel32").Sleep, secs * 1000
    else:
        try:
            sleep = ctypes.PyDLL(None).sleep
        except AttributeError:  # macOS libSystem fallback
            sleep = ctypes.PyDLL("/usr/lib/libSystem.B.dylib").sleep
    sleep.argtypes = [ctypes.c_uint]
    sleep(int(secs))


def _hang(block, name: str) -> dict:
    block(30)
    return {"ok": False, "error": f"{name} hook returned unexpectedly"}


_TEST_HOOKS = {
    "sleep": lambda: _hang(time.sleep, "sleep"),
    "gil": lambda: _hang(_hold_gil, "gil"),
    "empty": lambda: {"ok": True, "results": []},
}


class _SettingsShim:
    """Minimal stand-in for PluginContext.get_config so the child's getSetting()
    reads the switches the parent forwarded (no live ctx in a disposable child)."""

    def __init__(self, settings):
        self._s = settings or {}

    def get_config(self, key, default=None):
        return self._s.get(key, default)


def _load_plugin_module():
    here = os.path.dirname(os.path.abspath(__file__))
    init_path = os.path.join(here, "__init__.py")
    spec = importlib.util.spec_from_file_location("web_bingsearch_plugin", init_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_envelope(envelope: dict) -> None:
    json.dump(envelope, sys.stdout)
    sys.stdout.flush()


def _fail(error: str, code: int) -> int:
    _write_envelope({"ok": False, "error": error})
    return code


def _run_search(request: dict) -> int:
    queue = str(request.get("queue") or "")
    if not queue:
        return _fail("empty queue", 2)
    try:
        mod = _load_plugin_module()
        mod._pluginCtx = _SettingsShim(request.get("settings"))
        results = mod.ladderSearchHtml(queue)
        # F17：把子进程里记的诊断（尝试档数/最终 URL/页面体积）带回父进程
        _write_envelope({"ok": True, "results": results, "debug": getattr(mod, "_lastSearchDiag", None) or {}})
        return 0
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", 1)


def _run_extract(request: dict) -> int:
    """抓一个 URL：primp 在插件函数里 import，父进程只读这里吐出的 JSON。"""
    url = str(request.get("url") or "")
    if not url:
        return _fail("empty url", 2)
    try:
        mod = _load_plugin_module()
        mod._pluginCtx = _SettingsShim(request.get("settings"))
        raw_content = mod.crawlUrlRawHtmlStealth(url)
        _write_envelope({"ok": True, "raw_content": raw_content})
        return 0
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", 1)


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        return _fail(f"invalid request: {exc}", 2)
    if not isinstance(request, dict):
        return _fail("invalid request: not a JSON object", 2)

    hook = request.get("test_hook")
    if hook:
        if os.environ.get("HERMES_BING_ALLOW_TEST_HOOKS") != "1":
            return _fail("test_hook refused (hooks not enabled)", 3)
        fn = _TEST_HOOKS.get(str(hook))
        envelope = fn() if fn else {"ok": False, "error": f"unknown test_hook: {hook!r}"}
        _write_envelope(envelope)
        return 0 if envelope.get("ok") else 1

    op = str(request.get("op") or "search")  # 缺 op = v1.3 旧格式（只有 search）
    if op == "extract":
        return _run_extract(request)
    if op == "search":
        return _run_search(request)
    return _fail(f"unknown op: {op!r}", 2)


if __name__ == "__main__":
    raise SystemExit(main())
