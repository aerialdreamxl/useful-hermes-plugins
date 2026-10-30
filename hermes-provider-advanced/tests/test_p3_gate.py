"""Offline-audit fixes: kill switch, in-process refresh single-flight, native
proxy-aware claude/codex refreshers with the CPA wire shape.

Fakes at the boundary only (httpx patched, no network, no real tokens).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from advlib import oauth_gate  # noqa: E402
from advlib.account_store import AccountStore  # noqa: E402
from advlib import oauth_tokens  # noqa: E402


def _jwt(exp: int) -> str:
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _store(tmpdir: str) -> AccountStore:
    os.environ["HERMES_HOME"] = tmpdir
    return AccountStore(Path(tmpdir) / ".advanced-custom-providers.json")


class _Env(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("ADVANCED_DISABLE_OAUTH")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ADVANCED_DISABLE_OAUTH", None)
        else:
            os.environ["ADVANCED_DISABLE_OAUTH"] = self._saved


class TestKillSwitch(_Env):
    def test_truthy_values(self):
        for v in ("1", "true", "YES", "on", " 1 "):
            os.environ["ADVANCED_DISABLE_OAUTH"] = v
            self.assertTrue(oauth_gate.oauth_disabled(), v)
        for v in ("", "0", "false", "off"):
            os.environ["ADVANCED_DISABLE_OAUTH"] = v
            self.assertFalse(oauth_gate.oauth_disabled(), v)
        os.environ.pop("ADVANCED_DISABLE_OAUTH", None)
        self.assertFalse(oauth_gate.oauth_disabled())

    def test_transport_not_registered_when_disabled(self):
        os.environ["ADVANCED_DISABLE_OAUTH"] = "1"
        import advlib.oauth_runtime as rt

        rt._registered = False
        self.addCleanup(setattr, rt, "_registered", rt._registered)
        self.assertFalse(rt.ensure_registered())

    def test_client_materialization_refuses_our_refs(self):
        os.environ["ADVANCED_DISABLE_OAUTH"] = "1"
        import advlib as pkg

        with self.assertRaises(RuntimeError) as ctx:
            pkg.oauth_client_for_base_url("codex://me@x.com")
        self.assertIn("ADVANCED_DISABLE_OAUTH", str(ctx.exception))
        # a non-ref URL is not ours: passes through untouched (None)
        self.assertIsNone(pkg.oauth_client_for_base_url("https://api.openai.com/v1"))

    def test_model_listing_degrades_when_disabled(self):
        os.environ["ADVANCED_DISABLE_OAUTH"] = "1"
        import advlib as pkg

        self.assertIsNone(pkg.oauth_models_for_ref("codex://me@x.com"))

    def test_refresh_returns_none_when_disabled(self):
        os.environ["ADVANCED_DISABLE_OAUTH"] = "1"
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("codex", "me@x.com", {"refresh_token": "RT"})
            self.assertIsNone(oauth_tokens.refresh_and_persist("codex", "me@x.com", store))


class TestSingleFlight(_Env):
    def test_concurrent_gets_share_one_call(self):
        os.environ.pop("ADVANCED_DISABLE_OAUTH", None)
        calls = []
        gate = threading.Event()
        release = threading.Event()

        def slow_fn():
            calls.append(1)
            gate.set()        # first caller is in flight
            release.wait(2.0)  # hold the network window open
            return "TOKEN"

        results = []
        t1 = threading.Thread(target=lambda: results.append(
            oauth_gate.single_flight(("codex", "a@b.c"), slow_fn)))
        t1.start()
        gate.wait(2.0)
        t2 = threading.Thread(target=lambda: results.append(
            oauth_gate.single_flight(("codex", "a@b.c"), lambda: calls.append(2) or "SECOND")))
        t2.start()
        time.sleep(0.15)
        release.set()
        for t in (t1, t2):
            t.join(3.0)
        self.assertEqual(len(calls), 1, "the second caller must NOT hit the token endpoint twice")
        self.assertEqual(results, ["TOKEN", "TOKEN"])

    def test_failure_is_shared_then_slot_freed(self):
        def boom():
            raise ValueError("invalid_grant")

        with self.assertRaises(ValueError):
            oauth_gate.single_flight(("claude", "x"), boom)
        with self.assertRaises(ValueError):
            oauth_gate.single_flight(("claude", "x"), boom)  # key reusable after failure
        # a wedged leader times out the waiters instead of hanging the session
        started = threading.Event()
        ev = threading.Event()

        def wedge():
            started.set()
            ev.wait(30)
            return "late"

        t = threading.Thread(target=lambda: oauth_gate.single_flight(("codex", "wedged"), wedge))
        t.start()
        started.wait(2.0)  # leader is inside fn: the slot is definitely registered
        with self.assertRaises(TimeoutError):
            oauth_gate.single_flight(("codex", "wedged"), lambda: "never",
                                     wait_timeout=0.2)
        ev.set()
        t.join(3.0)

    def test_refresh_and_persist_serializes_same_account(self):
        """Two threads refreshing one spent-soon account => exactly one
        upstream refresher call (the replay guard, in-process)."""
        os.environ.pop("ADVANCED_DISABLE_OAUTH", None)
        real = oauth_tokens._platform_refresher
        calls = []

        def fake(platform):
            def _ref(acct, rt, proxy=""):
                calls.append(rt)
                time.sleep(0.2)  # widen the race window
                return {"access_token": _jwt(9999999999), "refresh_token": "RT-NEW"}
            return _ref

        oauth_tokens._platform_refresher = fake
        try:
            with tempfile.TemporaryDirectory() as td:
                store = _store(td)
                store.put("codex", "me@x.com", {"refresh_token": "RT-SINGLE"})
                outs = []
                ts = [threading.Thread(target=lambda: outs.append(
                    oauth_tokens.refresh_and_persist("codex", "me@x.com", store)))
                    for _ in range(3)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(5.0)
                stored = store.get("codex", "me@x.com")
        finally:
            oauth_tokens._platform_refresher = real
        self.assertEqual(len(calls), 1, f"replay risk: refresher ran {len(calls)}x")
        self.assertTrue(all(o and o.get("refresh_token") == "RT-NEW" for o in outs))
        self.assertIsNotNone(stored, "account readable inside the store's lifetime")
        self.assertEqual(stored["refresh_token"], "RT-NEW")


class TestNativeRefreshers(_Env):
    """Audit P0 pair: adapter-owned refreshers with proxy + CPA wire shape."""

    def _capture_client(self, holder, response_body, status=200):
        import httpx

        class _Resp:
            status_code = status

            def __init__(self, body):
                self._b = body

            def json(self):
                return self._b

            @property
            def text(self):
                return json.dumps(self._b)

        class _Client:
            def __init__(self, **kw):
                holder["client_kw"] = kw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, **kw):
                holder["post"] = {"url": url, **kw}
                return _Resp(response_body)

        return _Client

    def test_claude_refresh_is_json_to_platform_claude_com(self):
        from advlib.adapters import claude as cl

        holder: dict = {}
        real = httpx.Client
        httpx.Client = self._capture_client(
            holder, {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})
        try:
            upd = cl.refresh({}, "RT1", proxy="http://127.0.0.1:7890")
        finally:
            httpx.Client = real
        from advlib.oauth_login import CLAUDE_TOKEN_URL

        self.assertEqual(holder["post"]["url"], CLAUDE_TOKEN_URL)
        self.assertIn("platform.claude.com", CLAUDE_TOKEN_URL)
        body = json.loads(holder["post"]["content"])
        self.assertEqual(body["grant_type"], "refresh_token")
        self.assertEqual(body["client_id"], "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
        self.assertNotIn("data", holder["post"], "JSON body, not a form post")
        self.assertEqual(holder["post"]["headers"]["Content-Type"], "application/json")
        self.assertEqual(holder["post"]["headers"]["User-Agent"], "axios/1.15.2")
        self.assertEqual(holder["client_kw"].get("proxy"), "http://127.0.0.1:7890")
        self.assertEqual(upd["access_token"], "AT2")
        self.assertEqual(upd["refresh_token"], "RT2")
        self.assertTrue(int(upd["expired"]) > time.time())

    def test_codex_refresh_is_form_with_proxy_and_jwt_expiry(self):
        from advlib.adapters import codex as cx

        holder: dict = {}
        real = httpx.Client
        httpx.Client = self._capture_client(
            holder, {"access_token": _jwt(9999999999), "refresh_token": "RT2"})
        try:
            upd = cx.refresh({}, "RT1", proxy="http://127.0.0.1:7890")
        finally:
            httpx.Client = real
        self.assertEqual(holder["post"]["url"], "https://auth.openai.com/oauth/token")
        self.assertEqual(holder["post"]["data"]["grant_type"], "refresh_token")
        self.assertEqual(holder["post"]["data"]["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertEqual(holder["client_kw"].get("proxy"), "http://127.0.0.1:7890")
        self.assertEqual(upd["expired"], "9999999999")

    def test_dispatch_prefers_adapter_over_core(self):
        """Audit §2.3: codex/claude/antigravity all resolve to the ADAPTER's
        refresh(); the core pure refreshers remain only as a fallback."""
        for platform in ("codex", "claude", "antigravity"):
            fn = oauth_tokens._platform_refresher(platform)
            self.assertIsNotNone(fn, platform)
            import inspect

            self.assertIn("proxy", inspect.signature(fn).parameters,
                          f"{platform}: refresher must take proxy")

    def test_refresh_failure_raises_not_silently_empty(self):
        from advlib.adapters import claude as cl

        real = httpx.Client
        httpx.Client = self._capture_client({}, {"error": "invalid_grant"}, status=400)
        try:
            with self.assertRaises(ValueError):
                cl.refresh({}, "RT1")
        finally:
            httpx.Client = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
