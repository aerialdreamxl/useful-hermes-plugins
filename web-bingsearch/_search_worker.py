"""Bing HTML search child-process entrypoint, run as ``python _search_worker.py``.

Reads one JSON request ``{"queue": str, "settings": {...}}`` from stdin, writes one
envelope ``{"ok": true, "results": [...]}`` / ``{"ok": false, "error": str}`` to
stdout, exits. The parent (runHtmlSearchBounded) kills this process on a hard
wall-clock deadline because primp can block in native code while holding the GIL.

The plugin directory name has a hyphen (web-bingsearch) so it cannot be imported
as a normal package; we load __init__.py by file path via importlib. Settings are
forwarded from the parent through a tiny shim so the child honors the user's
stealth_headers / warm_homepage switches (the child has no live PluginContext).

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


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        return _fail(f"invalid request: {exc}", 2)

    hook = request.get("test_hook")
    if hook:
        if os.environ.get("HERMES_BING_ALLOW_TEST_HOOKS") != "1":
            return _fail("test_hook refused (hooks not enabled)", 3)
        fn = _TEST_HOOKS.get(str(hook))
        envelope = fn() if fn else {"ok": False, "error": f"unknown test_hook: {hook!r}"}
        _write_envelope(envelope)
        return 0 if envelope.get("ok") else 1

    queue = str(request.get("queue") or "")
    if not queue:
        return _fail("empty queue", 2)
    try:
        mod = _load_plugin_module()
        mod._pluginCtx = _SettingsShim(request.get("settings"))
        results = mod.ladderSearchHtml(queue)
        _write_envelope({"ok": True, "results": results})
        return 0
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}", 1)


if __name__ == "__main__":
    raise SystemExit(main())
