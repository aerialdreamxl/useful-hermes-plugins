"""P3.5 — interactive login flows (claude / antigravity) against fake upstreams.

No browser and no real credentials: the callback collector is stubbed to hand
back an authorization code, and every upstream URL is pointed at a local HTTP
server that records the exact request shape. What this proves is the part that
breaks in production — the wire contract:

- claude: JSON exchange whose KEY ORDER matches native Claude Code, the
  axios-shaped headers, the scope/params on the authorize URL, profile
  identity winning over the token body, one 32-byte device id.
- antigravity: form exchange with the Google public client, loadCodeAssist
  carrying ONLY ideType, project + default tier captured, onboard fallback.
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from advlib import oauth_login  # noqa: E402

HITS: "list[dict]" = []


class _Auth(BaseHTTPRequestHandler):
    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        HITS.append({"method": self.command, "path": self.path, "body": raw,
                     "headers": dict(self.headers)})
        return raw

    def do_POST(self):
        raw = self._record()
        p = self.path
        if p.startswith("/claude/token"):
            self._send({"access_token": "sk-ant-oat01-CLAUDE-AT",
                        "refresh_token": "CLAUDE-RT", "expires_in": 3600,
                        "account": {"uuid": "uuid-from-token",
                                    "email_address": "me@x.com"}})
        elif p.startswith("/ag/token"):
            self._send({"access_token": "ya29.AG-AT", "refresh_token": "1//AG-RT",
                        "expires_in": 3600, "token_type": "Bearer"})
        elif p.startswith("/v1internal:loadCodeAssist"):
            self._send({"allowedTiers": [{"id": "free-tier"},
                                         {"id": "standard-tier", "isDefault": True}],
                        "cloudaicompanionProject": "proj-known"})
        elif p.startswith("/v1internal:onboardUser"):
            self._send({"done": True,
                        "response": {"cloudaicompanionProject": {"id": "proj-onboarded"}}})
        else:
            self._send({"error": "unexpected"}, 404)

    def do_GET(self):
        self._record()
        p = self.path
        if p.startswith("/claude/profile"):
            self._send({"account": {"uuid": "uuid-from-profile", "email": "prof@x.com"},
                        "organization": {"uuid": "org-1", "name": "Org One"}})
        elif p.startswith("/claude/roles"):
            self._send({"roles": ["user"]})
        elif p.startswith("/ag/userinfo"):
            self._send({"email": "ag@x.com"})
        else:
            self._send({"error": "unexpected"}, 404)

    def log_message(self, *a):
        pass


class _StubCollector:
    """Stands in for the loopback server; returns a fixed code."""

    code = "AUTH-CODE"
    frag = ""
    seen: "list[tuple]" = []

    def __init__(self, port, paths, state):
        _StubCollector.seen.append((port, paths, state))

    def collect(self, timeout=0):
        return _StubCollector.code, _StubCollector.frag


class LoginTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Auth)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        HITS.clear()
        _StubCollector.seen = []
        self._saved = (oauth_login._Collector, oauth_login.CLAUDE_TOKEN_URL,
                       oauth_login.CLAUDE_PROFILE_URL, oauth_login.CLAUDE_ROLES_URL,
                       oauth_login.AG_TOKEN_URL, oauth_login.AG_USERINFO_URL,
                       oauth_login.AG_DAILY)
        oauth_login._Collector = _StubCollector
        oauth_login.CLAUDE_TOKEN_URL = self.base + "/claude/token"
        oauth_login.CLAUDE_PROFILE_URL = self.base + "/claude/profile"
        oauth_login.CLAUDE_ROLES_URL = self.base + "/claude/roles"
        oauth_login.AG_TOKEN_URL = self.base + "/ag/token"
        oauth_login.AG_USERINFO_URL = self.base + "/ag/userinfo"
        oauth_login.AG_DAILY = self.base + "/v1internal"

    def tearDown(self):
        (oauth_login._Collector, oauth_login.CLAUDE_TOKEN_URL,
         oauth_login.CLAUDE_PROFILE_URL, oauth_login.CLAUDE_ROLES_URL,
         oauth_login.AG_TOKEN_URL, oauth_login.AG_USERINFO_URL,
         oauth_login.AG_DAILY) = self._saved

    def hits(self, needle):
        return [h for h in HITS if needle in h["path"]]


class TestPrimitives(unittest.TestCase):
    def test_pkce_pair_matches_cpa_lengths(self):
        import base64
        import hashlib

        verifier, challenge = oauth_login.pkce_pair()
        self.assertEqual(len(verifier), 128, "96 random bytes -> 128 base64 chars")
        self.assertNotIn("=", verifier)
        want = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, want)

    def test_device_id_is_one_32_byte_hex(self):
        d = oauth_login.device_id()
        self.assertEqual(len(d), 64)
        int(d, 16)

    def test_slot_name_never_keeps_placeholder(self):
        self.assertEqual(oauth_login.slot_name("__login__", "a@b.c", "claude"), "a@b.c")
        self.assertEqual(oauth_login.slot_name("", "a@b.c", "claude"), "a@b.c")
        self.assertEqual(oauth_login.slot_name("work", "a@b.c", "claude"), "work")
        self.assertEqual(oauth_login.slot_name("__login__", "", "claude"), "claude-account")

    def test_antigravity_ua_matches_cpa(self):
        self.assertEqual(oauth_login.ag_user_agent(),
                         "antigravity/hub/2.24.0 (aidev_client; os_type=darwin; "
                         "arch=arm64; cl=1000335682)")


class TestClaudeLogin(LoginTestBase):
    def test_authorize_url_and_exchange(self):
        acct = oauth_login.claude_login(proxy="")
        # one loopback collector on the fixed Claude port
        self.assertEqual(_StubCollector.seen[0][0], 54545)
        self.assertEqual(_StubCollector.seen[0][1], ("/callback",))

        tok = self.hits("/claude/token")[0]
        # Key ORDER is the native-Claude-Code wire contract.
        keys = list(json.loads(tok["body"]).keys())
        self.assertEqual(keys, ["grant_type", "code", "redirect_uri", "client_id",
                                "code_verifier", "state"])
        body = json.loads(tok["body"])
        self.assertEqual(body["code"], "AUTH-CODE")
        self.assertEqual(body["client_id"], "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
        self.assertEqual(body["redirect_uri"], "http://localhost:54545/callback")
        self.assertGreaterEqual(len(body["code_verifier"]), 43)
        # axios-shaped identity of the exchange call
        self.assertEqual(tok["headers"].get("User-Agent"), "axios/1.15.2")
        self.assertEqual(tok["headers"].get("Connection"), "close")

        # profile identity WINS over the token body
        self.assertEqual(acct["email"], "prof@x.com")
        self.assertEqual(acct["account_uuid"], "uuid-from-profile")
        self.assertEqual(acct["organization_uuid"], "org-1")
        self.assertEqual(acct["organization_name"], "Org One")
        self.assertEqual(acct["access_token"], "sk-ant-oat01-CLAUDE-AT")
        self.assertEqual(acct["refresh_token"], "CLAUDE-RT")
        self.assertEqual(len(acct["device_ids"]), 1)
        self.assertEqual(len(acct["device_ids"][0]), 64)
        self.assertGreater(int(acct["expired"]), 0)

    def test_fragment_state_overrides(self):
        _StubCollector.frag = "STATE-FROM-FRAGMENT"
        try:
            oauth_login.claude_login(proxy="")
        finally:
            _StubCollector.frag = ""
        body = json.loads(self.hits("/claude/token")[0]["body"])
        self.assertEqual(body["state"], "STATE-FROM-FRAGMENT")

    def test_token_error_is_user_actionable(self):
        saved = oauth_login.CLAUDE_TOKEN_URL
        oauth_login.CLAUDE_TOKEN_URL = self.base + "/nope"
        try:
            with self.assertRaises(oauth_login.LoginError) as ctx:
                oauth_login.claude_login(proxy="")
        finally:
            oauth_login.CLAUDE_TOKEN_URL = saved
        self.assertIn("404", str(ctx.exception))


class TestAntigravityLogin(LoginTestBase):
    def test_form_exchange_and_project(self):
        acct = oauth_login.antigravity_login(proxy="")
        self.assertEqual(_StubCollector.seen[0][0], 51121)
        self.assertEqual(_StubCollector.seen[0][1], ("/oauth-callback",))

        tok = self.hits("/ag/token")[0]
        self.assertIn("grant_type=authorization_code", tok["body"])
        self.assertIn(f"client_secret={oauth_login.AG_CLIENT_SECRET}", tok["body"])
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A51121%2Foauth-callback",
                      tok["body"])
        self.assertEqual(tok["headers"].get("Content-Type"),
                         "application/x-www-form-urlencoded")

        load = self.hits("loadCodeAssist")[0]
        self.assertEqual(json.loads(load["body"]), {"metadata": {"ideType": "ANTIGRAVITY"}},
                         "loadCodeAssist body carries ONLY ideType (pluginType = 400)")

        self.assertEqual(acct["email"], "ag@x.com")
        self.assertEqual(acct["project_id"], "proj-known")
        self.assertEqual(acct["tier_id"], "standard-tier", "isDefault tier wins")
        self.assertEqual(acct["client_id"], oauth_login.AG_CLIENT_ID)
        self.assertTrue(acct["refresh_token"])
        self.assertEqual(self.hits("onboardUser"), [], "project known -> no onboarding")

    def test_onboard_when_project_missing(self):
        class _NoProject(_Auth):
            def do_POST(self):
                if self.path.startswith("/v1internal:loadCodeAssist"):
                    self._record()
                    self._send({"allowedTiers": [{"id": "free-tier", "isDefault": True}]})
                    return
                if self.path.startswith("/v1internal:onboardUser"):
                    self._record()
                    self._send({"done": True,
                                "response": {"cloudaicompanionProject": {"id": "proj-new"}}})
                    return
                _Auth.do_POST(self)  # body still unread here -> safe to delegate

        srv = HTTPServer(("127.0.0.1", 0), _NoProject)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old_token, old_info, old_daily = (oauth_login.AG_TOKEN_URL,
                                          oauth_login.AG_USERINFO_URL, oauth_login.AG_DAILY)
        b = f"http://127.0.0.1:{srv.server_address[1]}"
        oauth_login.AG_TOKEN_URL, oauth_login.AG_USERINFO_URL = b + "/ag/token", b + "/ag/userinfo"
        oauth_login.AG_DAILY = b + "/v1internal"
        try:
            acct = oauth_login.antigravity_login(proxy="")
        finally:
            (oauth_login.AG_TOKEN_URL, oauth_login.AG_USERINFO_URL,
             oauth_login.AG_DAILY) = old_token, old_info, old_daily
            srv.shutdown()
            srv.server_close()
        self.assertEqual(acct["project_id"], "proj-new")
        ob = [h for h in HITS if "onboardUser" in h["path"]]
        self.assertEqual(len(ob), 1)
        payload = json.loads(ob[0]["body"])
        self.assertEqual(payload["tier_id"], "free-tier")
        self.assertEqual(payload["metadata"]["ide_name"], "antigravity")
        self.assertIn("google-api-nodejs-client/10.3.0", ob[0]["headers"].get("User-Agent"))
        self.assertEqual(ob[0]["headers"].get("X-Goog-Api-Client"), "gl-node/22.21.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
