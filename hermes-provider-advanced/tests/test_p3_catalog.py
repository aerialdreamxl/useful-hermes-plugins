"""P3.6 — OAuth account catalog seams + force refresh (issue #1 & #2).

Covers the two things the user reported:
  #1 model listing for OAuth entries (CPA-style, from the account's own
     upstream — never a public vendor catalog), injected at BOTH core seams,
  #2 `oauth refresh <account>` force-rotating at/rt.

Seams are unit-tested against fake original callables (no heavy core import);
the antigravity lister is tested against a local fake cloudcode-pa over real
httpx. RouteTable is injected with synthetic config.
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

from advlib import oauth_models, transport_patches  # noqa: E402
from advlib.account_store import AccountStore  # noqa: E402
from advlib.adapters import antigravity as ag  # noqa: E402
from advlib.config_scan import RouteTable  # noqa: E402


def _jwt(exp):
    import base64

    h = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{h}.{p}.sig"


RESP = {"allowedTiers": [{"id": "standard-tier", "name": "Gemini Code Assist",
                          "isDefault": True}],
        "ineligibleTiers": [{"reasonCode": "UNSUPPORTED_CLIENT", "tierId": "free-tier"}]}
UPSTREAM_HITS: "list[dict]" = []


class _Anti(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        UPSTREAM_HITS.append({"path": self.path,
                              "auth": self.headers.get("Authorization") or "",
                              "originator": self.headers.get("Originator") or ""})
        p = json.dumps(RESP).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)

    def log_message(self, *a):
        pass


class CatalogTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Anti)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        UPSTREAM_HITS.clear()
        oauth_models._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self.tmp.name
        self.store = AccountStore(Path(self.tmp.name) / ".advanced-custom-providers.json")
        self.store.put("antigravity", "me@x.com",
                       {"access_token": _jwt(2 ** 31 - 1), "refresh_token": "",
                        "base_url": self.url, "type": "antigravity"})
        self._inject_route({"oauth_fingerprint": "hermes"})

    def tearDown(self):
        self.tmp.cleanup()

    def _inject_route(self, advanced, provider_key="ag-oauth",
                      ref="antigravity://me@x.com"):
        cfg = {"providers": {provider_key: {
            "provider": "custom", "model": "gemini-3-pro", "base_url": ref,
            "api_mode": "advanced_oauth", "advanced": advanced}}}
        table = RouteTable.from_config(config=cfg)
        import advlib.config_scan as cs

        real = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real)
        return table


class TestRouteKey(CatalogTestBase):
    def test_by_oauth_key_populated(self):
        table = self._inject_route({"oauth_fingerprint": "hermes"})
        self.assertIn("ag-oauth", table.by_oauth_key)
        self.assertEqual(table.by_oauth_key["ag-oauth"].base_url, "antigravity://me@x.com")

    def test_disabled_oauth_entry_absent_from_key(self):
        # api-mode entries must NOT land in by_oauth_key
        cfg = {"providers": {"plain": {"provider": "custom",
                                       "base_url": "https://x/v1",
                                       "advanced": {"proxy": "http://127.0.0.1:1"}}}}
        table = RouteTable.from_config(config=cfg)
        self.assertNotIn("plain", table.by_oauth_key)


class TestAntigravityLister(CatalogTestBase):
    def test_hits_account_upstream(self):
        models = oauth_models.list_models_for_ref("antigravity://me@x.com")
        self.assertEqual(models, ag.antigravity_catalog(["standard-tier"]))
        self.assertEqual(len(UPSTREAM_HITS), 1)
        self.assertEqual(UPSTREAM_HITS[0]["path"], "/v1internal:loadCodeAssist")
        self.assertTrue(UPSTREAM_HITS[0]["auth"].startswith("Bearer eyJ"))

    def test_cache_then_force_refresh(self):
        oauth_models.list_models_for_ref("antigravity://me@x.com")
        oauth_models.list_models_for_ref("antigravity://me@x.com")
        self.assertEqual(len(UPSTREAM_HITS), 1, "second call served from cache")
        oauth_models.list_models_for_ref("antigravity://me@x.com", force_refresh=True)
        self.assertEqual(len(UPSTREAM_HITS), 2, "force_refresh bypassed cache")

    def test_fingerprint_other_platform_is_noop(self):
        # sub2api identity is codex-specific (recon: its canonical headers are
        # the codex CLI pair); on antigravity it must no-op, not crash or
        # mis-label the listing call.
        self._inject_route({"oauth_fingerprint": "sub2api"})
        models = oauth_models.list_models_for_ref("antigravity://me@x.com", force_refresh=True)
        self.assertEqual(models, ag.antigravity_catalog(["standard-tier"]))
        self.assertEqual(UPSTREAM_HITS[-1]["originator"], "")

    def test_no_access_material_returns_none(self):
        self.store.put("antigravity", "ghost", {"refresh_token": ""})
        self._inject_route({"oauth_fingerprint": "hermes"}, ref="antigravity://ghost")
        self.assertIsNone(oauth_models.list_models_for_ref("antigravity://ghost"))


class TestProfileSeam(CatalogTestBase):
    def test_wraps_named_oauth_entry(self):
        captured = []
        fake_orig = lambda norm: captured.append(norm) or ["CURATED"]
        wrapped = transport_patches._wrap_profile_live_catalog(fake_orig)
        out = wrapped("ag-oauth")
        self.assertEqual(out, ag.antigravity_catalog(["standard-tier"]))
        self.assertEqual(captured, [], "original must not run for our entry")

    def test_passthrough_non_oauth(self):
        sent = []
        fake_orig = lambda norm: sent.append(norm) or ["CURATED"]
        wrapped = transport_patches._wrap_profile_live_catalog(fake_orig)
        out = wrapped("some-other-provider")
        self.assertEqual(out, ["CURATED"])
        self.assertEqual(sent, ["some-other-provider"])

    def test_degrades_when_upstream_empty(self):
        # a platform with no listing (or failed probe) -> original curated path
        self.store.delete("antigravity", "me@x.com")
        fake_orig = lambda norm: ["CURATED"]
        wrapped = transport_patches._wrap_profile_live_catalog(fake_orig)
        out = wrapped("ag-oauth")
        self.assertEqual(out, ["CURATED"], "empty account list falls back, never []")


class TestDiscoverSeam(CatalogTestBase):
    def test_scheme_ref_intercepted(self):
        called = []
        fake_orig = lambda api_key, api_url, *a, **kw: called.append(api_url) or (["UR"], False)
        wrapped = transport_patches._wrap_discover_endpoint_models(fake_orig)
        models, native_empty = wrapped("", "antigravity://me@x.com", "custom", False,
                                       headers=None, api_mode="advanced_oauth",
                                       probe_live=True, discovery_allowed=True,
                                       fast_custom_probe=False)
        self.assertEqual(models, ag.antigravity_catalog(["standard-tier"]))
        self.assertFalse(native_empty)
        self.assertEqual(called, [], "urllib fetch must not run for scheme refs")

    def test_http_url_passthrough(self):
        called = []
        fake_orig = lambda api_key, api_url, *a, **kw: called.append(api_url) or (["OK"], False)
        wrapped = transport_patches._wrap_discover_endpoint_models(fake_orig)
        models, _ = wrapped("sk-1", "https://real/v1", "custom", False,
                            headers=None, api_mode=None, probe_live=True,
                            discovery_allowed=True, fast_custom_probe=False)
        self.assertEqual(models, ["OK"])
        self.assertEqual(called, ["https://real/v1"])


class TestForceRefresh(unittest.TestCase):
    """Issue #2: oauth refresh rotates at/rt NOW regardless of expiry."""

    def test_force_refresh_bypasses_expiry_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["HERMES_HOME"] = td
            store = AccountStore(Path(td) / ".advanced-custom-providers.json")
            # a token valid for YEARS (needs_refresh would say False)
            store.put("codex", "me@x.com",
                      {"access_token": _jwt(2 ** 31 - 1), "refresh_token": "RT-OLD"})
            calls = []

            def fake(acct, rt):
                calls.append(rt)
                return {"access_token": _jwt(10 ** 10), "refresh_token": "RT-NEW"}

            from advlib import oauth_client, oauth_tokens

            real = oauth_tokens._platform_refresher
            oauth_tokens._platform_refresher = lambda p: fake if p == "codex" else None
            try:
                ctx = oauth_client.OAuthTokenContext("codex", "me@x.com", store)
                fresh = ctx.force_refresh()
            finally:
                oauth_tokens._platform_refresher = real
            self.assertEqual(calls, ["RT-OLD"], "refresh ran despite valid token")
            self.assertEqual(fresh["refresh_token"], "RT-NEW")
            self.assertEqual(store.get("codex", "me@x.com")["refresh_token"], "RT-NEW")

    def test_force_refresh_missing_account_raises(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["HERMES_HOME"] = td
            store = AccountStore(Path(td) / ".advanced-custom-providers.json")
            from advlib import oauth_client

            ctx = oauth_client.OAuthTokenContext("codex", "ghost", store)
            with self.assertRaises(oauth_client.AccountNotFound):
                ctx.force_refresh()


class _CodexCatalog(BaseHTTPRequestHandler):
    """Fake ChatGPT-backend catalog: records the exact path+query it was hit
    with and serves a fixed payload (visibility mix included)."""

    PAYLOAD: dict = {}

    def do_GET(self):
        HITS.append({"path": self.path,
                     "ua": self.headers.get("User-Agent") or "",
                     "originator": self.headers.get("Originator") or ""})
        p = json.dumps(self.PAYLOAD).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)

    def log_message(self, *a):
        pass


HITS: "list[dict]" = []


class TestCodexCatalog(unittest.TestCase):
    """Issue #1 (codex half): the ChatGPT backend catalog needs
    ?client_version= (400 without it) and must hide visibility!=list models."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _CodexCatalog)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        HITS.clear()
        oauth_models._CACHE.clear()
        _CodexCatalog.PAYLOAD = {"models": [
            {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1},
            {"slug": "gpt-reserve", "visibility": "hide", "priority": 4},
            {"slug": "gpt-6-luna", "visibility": "list", "priority": 5},
            {"slug": "codex-auto-review", "visibility": "hide", "priority": 43},
        ]}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self.tmp.name
        self.store = AccountStore(Path(self.tmp.name) / ".advanced-custom-providers.json")
        self.store.put("codex", "me@x.com",
                       {"access_token": _jwt(2 ** 31 - 1), "refresh_token": "RT",
                        "account_id": "acc1", "base_url": self.url, "type": "codex"})
        cfg = {"providers": {"cx-oauth": {
            "provider": "custom", "model": "gpt-6.1-sol",
            "base_url": "codex://me@x.com", "api_mode": "advanced_oauth",
            "advanced": {"oauth_fingerprint": "hermes"}}}}
        table = RouteTable.from_config(config=cfg)
        import advlib.config_scan as cs

        real = cs.RouteTable.from_config
        cs.RouteTable.from_config = classmethod(lambda cls, **kw: table)
        self.addCleanup(setattr, cs.RouteTable, "from_config", real)

    def tearDown(self):
        self.tmp.cleanup()

    def test_official_branch_sends_client_version_and_filters_hidden(self):
        from advlib.adapters import codex as cx

        real = cx.is_official_codex_base
        cx.is_official_codex_base = lambda base: True
        self.addCleanup(setattr, cx, "is_official_codex_base", real)

        models = oauth_models.list_models_for_ref("codex://me@x.com")
        self.assertEqual(models, ["gpt-6.1-sol", "gpt-6-luna"],
                         "visibility=hide entries must never surface")
        self.assertEqual(len(HITS), 1)
        self.assertIn("client_version=", HITS[0]["path"])
        self.assertIn(cx.CODEX_CLIENT_VERSION, HITS[0]["path"])

    def test_official_branch_falls_back_to_curated_when_upstream_dead(self):
        from advlib.adapters import codex as cx

        real = cx.is_official_codex_base
        cx.is_official_codex_base = lambda base: True
        self.addCleanup(setattr, cx, "is_official_codex_base", real)
        self.store.put("codex", "me@x.com",
                       {"access_token": _jwt(2 ** 31 - 1), "refresh_token": "RT",
                        "base_url": "http://127.0.0.1:1", "type": "codex"})

        models = oauth_models.list_models_for_ref("codex://me@x.com")
        self.assertEqual(models, list(cx.CURATED_MODELS),
                         "dead upstream degrades to the curated table, never []")
        self.assertNotIn("gpt-reserve", models)

    def test_relay_branch_stays_plain(self):
        # stored base_url that is NOT chatgpt.com = relay: no query, no filter
        models = oauth_models.list_models_for_ref("codex://me@x.com")
        self.assertEqual(models, ["gpt-6.1-sol", "gpt-reserve",
                                  "gpt-6-luna", "codex-auto-review"])
        self.assertEqual(HITS[0]["path"], "/models",
                         "relay listing must stay byte-identical to before")

    def test_is_official_codex_base(self):
        from advlib.adapters import codex as cx

        self.assertTrue(cx.is_official_codex_base("https://chatgpt.com/backend-api/codex"))
        self.assertTrue(cx.is_official_codex_base("https://CHATGPT.com/backend-api/codex/"))
        self.assertFalse(cx.is_official_codex_base("https://my-relay.example/v1"))
        self.assertFalse(cx.is_official_codex_base("https://chatgpt.com/v1"))
        self.assertFalse(cx.is_official_codex_base(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
