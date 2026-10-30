"""P3 E2E — the whole advanced_oauth chain against a local fake backend.

Real components: AccountStore (temp HERMES_HOME), RouteTable (synthetic
config), oauth http hook, fingerprints, OpenAI SDK client + responses wire,
chat->Responses facade. Faked: the upstream (http.server) and core refreshers
(sys.modules patch — no network, no real OAuth).

Covers v3 §3.2 runtime contract:
- live bearer injected per request (placeholder key never leaks)
- fingerprint profiles change outbound identity (hermes vs sub2api)
- expired access token -> core-style refresh -> persisted rotation -> retry
  with the NEW token
- missing account slot -> AccountNotFound with pointer (no silent connect)
- wire shape: backend receives Responses-form payload (instructions/input)
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _jwt(exp: int) -> str:
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "https://api.openai.com/auth":
                    {"chatgpt_account_id": "acct-123"}}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


RESPONSES_BODY = {
    "id": "resp_fake_1", "object": "response", "created_at": 1780000000,
    "status": "completed", "model": "gpt-5-codex", "output": [
        {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "pong from fake backend",
                      "annotations": []}]}],
    "usage": {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11,
              "input_tokens_details": {"cached_tokens": 0}},
}


class _CaptureHandler(BaseHTTPRequestHandler):
    captured: "list[dict]" = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).captured.append({
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": raw.decode("utf-8", "replace"),
        })
        payload = json.dumps(RESPONSES_BODY).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # quiet
        pass


class P3E2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _CaptureHandler.captured = []
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        os.environ["HERMES_HOME"] = str(self.home)
        from advlib.account_store import AccountStore

        self.store = AccountStore(self.home / ".advanced-custom-providers.json")

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------------ chain

    def _config(self, fingerprint="hermes", api_mode="advanced_oauth"):
        return {
            "model": {"provider": "custom", "base_url": "https://unused.invalid/v1"},
            "providers": {
                "codex-oauth": {
                    "provider": "custom",
                    "model": "gpt-5-codex",
                    "base_url": "codex://me@x.com",
                    "api_mode": api_mode,
                    "advanced": {"oauth_fingerprint": fingerprint},
                }
            },
        }

    def _patched_refresher(self, future_exp: int, new_rt="RT-ROT"):
        """Install a fake core codex refresher; record calls."""
        from advlib import oauth_tokens

        calls = []

        def fake(acct, rt):
            calls.append(rt)
            return {"access_token": _jwt(future_exp), "refresh_token": new_rt}

        real = oauth_tokens._platform_refresher
        oauth_tokens._platform_refresher = lambda p: fake if p == "codex" else None
        self.addCleanup(setattr, oauth_tokens, "_platform_refresher", real)
        return calls

    def _build(self):
        from advlib import oauth_runtime

        self.assertTrue(oauth_runtime.ensure_registered())
        return oauth_runtime.build_client_for_ref("codex://me@x.com")

    def test_full_chain_live_bearer_and_wire_shape(self):
        base = f"http://127.0.0.1:{self.port}"
        exp_far = 2 ** 31 - 1
        self.store.put("codex", "me@x.com", {
            "access_token": _jwt(exp_far), "refresh_token": "RT-KEEP",
            "base_url": base, "account_id": "acct-123", "type": "oauth"})
        from advlib.config_scan import RouteTable

        # route table re-reads live config via _load_raw_config; in the test we
        # inject the synthetic config through oauth_runtime's entry lookup:
        table = RouteTable.from_config(config=self._config())
        import advlib.config_scan as cs

        real_build = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real_build)

        client = self._build()
        resp = client.chat.completions.create(
            model="gpt-5-codex",
            messages=[{"role": "system", "content": "sys"},
                      {"role": "user", "content": "ping"}])
        self.assertEqual(len(_CaptureHandler.captured), 1)
        rec = _CaptureHandler.captured[0]
        # wire path + bearer from store (placeholder never sent)
        self.assertIn("responses", rec["path"])
        self.assertEqual(rec["headers"]["authorization"], f"Bearer {_jwt(exp_far)}")
        self.assertNotIn("placeholder", rec["headers"]["authorization"])
        # Responses-form payload arrived (chat facade did its job)
        body = json.loads(rec["body"])
        self.assertEqual(body.get("instructions"), "sys")
        self.assertIn("input", body)
        self.assertNotIn("messages", body)
        # chat-shaped result came back
        self.assertEqual(resp.choices[0].message.content, "pong from fake backend")
        self.assertEqual(resp.usage.total_tokens, 11)

    def test_fingerprint_sub2api_overrides_identity(self):
        base = f"http://127.0.0.1:{self.port}"
        exp_far = 2 ** 31 - 1
        self.store.put("codex", "me@x.com", {
            "access_token": _jwt(exp_far), "refresh_token": "RT",
            "base_url": base, "type": "oauth"})
        from advlib.config_scan import RouteTable
        import advlib.config_scan as cs

        table = RouteTable.from_config(config=self._config(fingerprint="sub2api"))
        real_build = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real_build)

        client = self._build()
        client.chat.completions.create(model="gpt-5-codex",
                                       messages=[{"role": "user", "content": "ping"}])
        headers = _CaptureHandler.captured[-1]["headers"]
        self.assertEqual(headers["originator"], "codex_cli_rs")
        from advlib.fingerprints import sub2api

        self.assertIn(f"codex_cli_rs/{sub2api.DEFAULT_VERSION}", headers["user-agent"])
        self.assertEqual(headers["version"], sub2api.DEFAULT_VERSION)
        self.assertEqual(headers["chatgpt-account-id"], "acct-123")

    def test_expired_token_refresh_persist_and_send_new(self):
        base = f"http://127.0.0.1:{self.port}"
        expired = _jwt(1000)  # exp=1000 epoch: long gone
        self.store.put("codex", "me@x.com", {
            "access_token": expired, "refresh_token": "RT-OLD",
            "base_url": base, "type": "oauth"})
        from advlib.config_scan import RouteTable
        import advlib.config_scan as cs

        table = RouteTable.from_config(config=self._config())
        real_build = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real_build)

        calls = self._patched_refresher(future_exp=2 ** 31 - 1, new_rt="RT-NEW")
        client = self._build()
        client.chat.completions.create(model="gpt-5-codex",
                                       messages=[{"role": "user", "content": "ping"}])
        self.assertEqual(calls, ["RT-OLD"], "refresh triggered once")
        headers = _CaptureHandler.captured[-1]["headers"]
        self.assertNotEqual(headers["authorization"], f"Bearer {expired}")
        stored = self.store.get("codex", "me@x.com")
        self.assertEqual(stored["refresh_token"], "RT-NEW", "rotation persisted")

    def test_missing_account_raises_pointer_not_silent_connect(self):
        from advlib.config_scan import RouteTable
        import advlib.config_scan as cs

        table = RouteTable.from_config(config=self._config())
        real_build = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real_build)

        from advlib.oauth_client import AccountNotFound

        with self.assertRaises(AccountNotFound):
            self._build()
        self.assertEqual(_CaptureHandler.captured, [], "nothing hit the wire")

    def test_disabled_adapter_is_a_pointer_not_wire(self):
        from advlib.config_scan import RouteTable
        import advlib.config_scan as cs

        table = RouteTable.from_config(config=self._config())
        real_build = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real_build)

        os.environ["ADVANCED_ADAPTERS"] = "codex"
        self.addCleanup(os.environ.pop, "ADVANCED_ADAPTERS", None)
        from advlib.adapters import AdapterUnavailable

        with self.assertRaises(AdapterUnavailable):
            self._build()
        self.assertEqual(_CaptureHandler.captured, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
