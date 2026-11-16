"""P3 unit tests — oauth tokens, importers, fingerprints, facade, runtime.

All external boundaries are faked (no network, no real core auth functions):
- core refreshers patched via sys.modules injection
- httpx requests built directly (no transport)
- RouteTable via synthetic config dicts
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from advlib import config_scan, importers, oauth_client, oauth_tokens  # noqa: E402
from advlib.account_store import AccountStore  # noqa: E402
from advlib.adapters import all_adapters, get  # noqa: E402


def _store(tmpdir: str) -> AccountStore:
    os.environ["HERMES_HOME"] = tmpdir  # store_path() reads it
    return AccountStore(Path(tmpdir) / ".advanced-custom-providers.json")


def _jwt(exp: int) -> str:
    """Unsigned JWT with only exp claim, enough for _jwt_expiry."""
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


class TestExpiryParsing(unittest.TestCase):
    def test_epoch_seconds_and_millis(self):
        self.assertEqual(oauth_tokens.parse_expiry(1780000000), 1780000000.0)
        self.assertEqual(oauth_tokens.parse_expiry(1780000000000), 1780000000.0)
        self.assertEqual(oauth_tokens.parse_expiry("1780000000"), 1780000000.0)

    def test_iso_and_cpa_forms(self):
        self.assertIsNotNone(oauth_tokens.parse_expiry("2027-01-01T00:00:00Z"))
        self.assertIsNotNone(oauth_tokens.parse_expiry("2027-01-01 00:00:00"))
        self.assertIsNone(oauth_tokens.parse_expiry("nonsense"))
        self.assertIsNone(oauth_tokens.parse_expiry(""))
        self.assertIsNone(oauth_tokens.parse_expiry(None))

    def test_needs_refresh_decision(self):
        self.assertTrue(oauth_tokens.needs_refresh({"access_token": ""}))
        self.assertTrue(oauth_tokens.needs_refresh({"access_token": _jwt(1000)}, now=2000))
        self.assertFalse(oauth_tokens.needs_refresh({"access_token": _jwt(10000)}, now=2000))
        # unknown expiry + non-jwt token -> conservative refresh
        self.assertTrue(oauth_tokens.needs_refresh({"access_token": "opaque"}, now=2000))
        # skew zone: expires in 60s, skew 120s -> refresh
        self.assertTrue(oauth_tokens.needs_refresh({"access_token": _jwt(2060)}, now=2000))


class TestRefreshPersist(unittest.TestCase):
    def test_refresh_rotates_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("codex", "me@x.com",
                      {"access_token": _jwt(1000), "refresh_token": "RT-OLD"})
            calls = []

            def fake_refresher(acct, rt):
                calls.append(rt)
                return {"access_token": _jwt(9999), "refresh_token": "RT-NEW"}

            real = oauth_tokens._platform_refresher
            oauth_tokens._platform_refresher = lambda p: fake_refresher
            try:
                out = oauth_tokens.refresh_and_persist("codex", "me@x.com", store)
            finally:
                oauth_tokens._platform_refresher = real
            self.assertEqual(calls, ["RT-OLD"])
            self.assertIsNotNone(out)
            self.assertEqual(out["refresh_token"], "RT-NEW")
            stored = store.get("codex", "me@x.com")
            self.assertEqual(stored["refresh_token"], "RT-NEW", "rotation must persist")

    def test_peer_rotation_adopted(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("codex", "me@x.com",
                      {"access_token": _jwt(1000), "refresh_token": "RT-OURS"})

            class PeerRotator(Exception):
                pass

            def failing_then_peer(acct, rt):
                # peer wrote a NEWER refresh token while we were away
                store.put("codex", "me@x.com",
                          {"access_token": _jwt(8888), "refresh_token": "RT-PEER"})
                raise PeerRotator("invalid_grant")

            real = oauth_tokens._platform_refresher
            oauth_tokens._platform_refresher = lambda p: failing_then_peer
            try:
                out = oauth_tokens.refresh_and_persist("codex", "me@x.com", store)
            finally:
                oauth_tokens._platform_refresher = real
            self.assertIsNotNone(out)
            self.assertEqual(out["refresh_token"], "RT-PEER", "peer pair adopted, not spent replay")


class TestAccountTokenContext(unittest.TestCase):
    def test_token_lazily_refreshes_missing_access(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("codex", "a@b.c", {"refresh_token": "RT"})
            real = oauth_tokens._platform_refresher
            oauth_tokens._platform_refresher = lambda p: (
                lambda acct, rt: {"access_token": _jwt(9999999999), "refresh_token": "RT2"})
            try:
                ctx = oauth_client.OAuthTokenContext("codex", "a@b.c", store)
                tok = ctx.token()
                self.assertTrue(tok.startswith("eyJ") or "." in tok)
                tok2 = ctx.token()  # cached now
                self.assertEqual(tok, tok2)
            finally:
                oauth_tokens._platform_refresher = real

    def test_missing_account_raises_named_error(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            ctx = oauth_client.OAuthTokenContext("codex", "ghost", store)
            with self.assertRaises(oauth_client.AccountNotFound):
                ctx.token()


class TestImporters(unittest.TestCase):
    def test_bare_codex_record(self):
        cands = importers.extract_accounts(
            {"access_token": "eyJabc.def.ghi", "refresh_token": "RT",
             "account_id": "acc1", "email": "me@x.com", "type": "codex"})
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["platform"], "codex")
        self.assertEqual(cands[0]["account"]["account_id"], "acc1")

    def test_claude_record_shape(self):
        cands = importers.extract_accounts(
            {"access_token": "sk-ant-oat01-xyz", "refresh_token": "rt",
             "account_uuid": "u1", "email": "me@y.z"})
        self.assertEqual([c["platform"] for c in cands], ["claude"])

    def test_sub2api_row_nested_credentials(self):
        payload = [
            {"id": 7, "name": "acct-seven", "type": "apikey",
             "credentials": {"access_token": "eyJx.y.z", "refresh_token": "RT7",
                             "chatgpt_account_id": "cafe07"}},
            {"id": 8, "name": "acct-eight",
             "credentials": {"access_token": "sk-ant-oat-nope", "refresh_token": "RT8"}},
        ]
        cands = importers.extract_accounts(payload)
        self.assertEqual(len(cands), 2)
        plats = {c["platform"] for c in cands}
        self.assertEqual(plats, {"codex", "claude"})

    def test_keyed_map(self):
        payload = {"me@x.com": {"access_token": "eyJ1.2.3", "refresh_token": "RT"},
                   "work@y.com": {"access_token": "eyJ9.8.7", "refresh_token": "RT"}}
        cands = importers.extract_accounts(payload)
        self.assertEqual(len(cands), 2)
        names = sorted(c["name"] for c in cands)
        self.assertEqual(names, ["me@x.com", "work@y.com"])

    def test_dedup_same_token(self):
        payload = [{"access_token": "eyJdup.1.2", "refresh_token": "RT"},
                   {"access_token": "eyJdup.1.2", "refresh_token": "RT"}]
        self.assertEqual(len(importers.extract_accounts(payload)), 1)

    def test_garbage_rejected(self):
        self.assertEqual(importers.extract_accounts({"hello": "world"}), [])
        self.assertEqual(importers.extract_accounts([1, 2, 3]), [])

    def test_load_json_file_and_inline(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dump.json"
            p.write_text(json.dumps({"access_token": "eyJf.1.1", "refresh_token": "R"}),
                         encoding="utf-8")
            self.assertIsInstance(importers.load_json(str(p)), dict)
            self.assertIsInstance(importers.load_json('{"a": 1}'), dict)
            with self.assertRaises(ValueError):
                importers.load_json("not json at all")


class TestFingerprints(unittest.TestCase):
    def _req(self):
        import httpx

        return httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses",
                             headers={"User-Agent": "python-httpx/x",
                                      "Authorization": "Bearer tok"})

    def test_hermes_fill_not_rewrite(self):
        import httpx
        from advlib import fingerprints

        req = self._req()
        before = req.headers["User-Agent"]
        fingerprints.apply(req, platform="codex", account={}, phase="inference",
                           name="hermes", access_token="")
        self.assertEqual(req.headers["User-Agent"], before, "fill-only: existing kept")

    def test_sub2api_rewrite(self):
        from advlib import fingerprints

        req = self._req()
        fingerprints.apply(req, platform="codex", account={}, phase="inference",
                           name="sub2api", access_token="")
        self.assertEqual(req.headers["Originator"], "codex_cli_rs")
        from advlib.fingerprints import sub2api

        self.assertIn(f"codex_cli_rs/{sub2api.DEFAULT_VERSION}", req.headers["User-Agent"])
        self.assertEqual(req.headers["Version"], sub2api.DEFAULT_VERSION)

    def test_sub2api_auth_phase_no_version(self):
        from advlib import fingerprints

        req = httpx_request_auth()
        fingerprints.apply(req, platform="codex", account={}, phase="auth",
                           name="sub2api", access_token="")
        self.assertNotIn("Version", req.headers)
        self.assertEqual(req.headers["Originator"], "codex_cli_rs")

    def test_cpa_codex_executor_identity(self):
        """CPA's MAIN executor (codex_executor_request.go:32-33) — not the
        codex_cli_rs alpha-search front door the old stub copied."""
        from advlib import fingerprints

        req = self._req()
        fingerprints.apply(req, platform="codex", account={"account_id": "acc-9"},
                           phase="inference", name="cpa", access_token="")
        self.assertEqual(req.headers["Originator"], "codex-tui")
        self.assertIn("codex-tui/0.154.0", req.headers["User-Agent"])
        self.assertIn("iTerm.app/3.6.11", req.headers["User-Agent"])
        self.assertEqual(req.headers["Connection"], "Keep-Alive")
        self.assertEqual(req.headers["Chatgpt-Account-Id"], "acc-9")
        self.assertNotIn("codex_cli_rs", req.headers["Originator"])

    def test_cpa_codex_version_override(self):
        from advlib import fingerprints

        req = self._req()
        fingerprints.apply(req, platform="codex",
                           account={"fingerprint_version": "0.200.0"},
                           phase="inference", name="cpa", access_token="")
        self.assertIn("codex-tui/0.200.0", req.headers["User-Agent"])
        self.assertIn("(codex-tui; 0.200.0)", req.headers["User-Agent"])

    def test_cpa_claude_device_profile(self):
        import httpx
        from advlib import fingerprints

        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        fingerprints.apply(req, platform="claude", account={}, phase="inference",
                           name="cpa", access_token="")
        self.assertEqual(req.headers["User-Agent"], "claude-cli/2.1.280 (external, cli)")
        self.assertEqual(req.headers["X-App"], "cli")
        self.assertEqual(req.headers["X-Stainless-Package-Version"], "0.112.1")
        self.assertEqual(req.headers["X-Stainless-Runtime-Version"], "v26.3.0")
        self.assertEqual(req.headers["Anthropic-Version"], "2023-06-01")

    def test_cpa_antigravity_hub_ua(self):
        import httpx
        from advlib import fingerprints

        req = httpx.Request("POST", "https://cloudcode-pa.googleapis.com/v1internal:generateContent")
        fingerprints.apply(req, platform="antigravity", account={}, phase="inference",
                           name="cpa", access_token="")
        self.assertEqual(req.headers["User-Agent"],
                         "antigravity/hub/2.24.0 (aidev_client; os_type=darwin; "
                         "arch=arm64; cl=1000335682)")

    def test_cpa_other_platform_is_noop(self):
        import httpx
        from advlib import fingerprints

        req = httpx.Request("POST", "https://chat.qwen.ai/api/v1/chat")
        before = dict(req.headers)
        fingerprints.apply(req, platform="qwen", account={}, phase="inference",
                           name="cpa", access_token="")
        self.assertEqual(dict(req.headers), before)

    def test_unknown_fallback_hermes(self):
        from advlib import fingerprints

        req = self._req()
        fingerprints.apply(req, platform="codex", account={}, phase="inference",
                           name="does-not-exist", access_token="")
        # no crash; hermes profile keeps existing UA (fill-only)
        self.assertEqual(req.headers["User-Agent"], "python-httpx/x")


def httpx_request_auth():
    import httpx

    return httpx.Request("POST", "https://auth.openai.com/oauth/token")


class TestFacadeTranslation(unittest.TestCase):
    def test_chat_kwargs_to_responses_core_converter(self):
        kw = {"model": "gpt-5-codex",
              "messages": [{"role": "system", "content": "be nice"},
                           {"role": "user", "content": "hi"}],
              "max_tokens": 100}
        out = oauth_client._chat_kwargs_to_responses(kw)
        self.assertEqual(out["model"], "gpt-5-codex")
        self.assertEqual(out["instructions"], "be nice")
        self.assertIs(out["store"], False)
        self.assertEqual(out["max_output_tokens"], 100)
        # input is core-converted (list of items or plain messages — both fine)
        self.assertIn("input", out)

    def test_response_to_chat_object(self):
        from types import SimpleNamespace

        resp = SimpleNamespace(
            id="resp1",
            output=[SimpleNamespace(type="message", content=[
                SimpleNamespace(type="output_text", text="hello world")]),
                SimpleNamespace(type="function_call", call_id="call_1", name="do_thing",
                                arguments=json.dumps({"x": 1}))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5))
        obj = oauth_client._response_to_chat_object(resp, model="gpt-5-codex")
        self.assertEqual(obj.choices[0].message.content, "hello world")
        self.assertEqual(obj.choices[0].message.tool_calls[0].function.name, "do_thing")
        self.assertEqual(obj.choices[0].finish_reason, "tool_calls")
        self.assertEqual(obj.usage.total_tokens, 15)

    def test_stream_events_to_chunks(self):
        from types import SimpleNamespace

        evs = [SimpleNamespace(type="response.output_text.delta", delta="a"),
               SimpleNamespace(type="response.output_text.delta", delta="b"),
               SimpleNamespace(type="response.completed")]
        chunks = list(oauth_client._stream_events_to_chunks(iter(evs), model="m"))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0].choices[0].delta.content, "a")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")


class TestOAuthRuntime(unittest.TestCase):
    def test_transport_registration(self):
        from advlib import oauth_runtime

        self.assertTrue(oauth_runtime.ensure_registered())
        self.assertTrue(oauth_runtime.ensure_registered(), "idempotent")
        from agent.transports import registered_api_modes

        self.assertIn("advanced_oauth", registered_api_modes())

    def test_normalize_shape_sniff(self):
        from types import SimpleNamespace

        from advlib.oauth_runtime import AdvancedOAuthTransport
        from agent.transports.types import NormalizedResponse

        t = AdvancedOAuthTransport()
        # responses-shaped object -> core codex normalizer (needs non-empty
        # output items: empty output is a core-side RuntimeError by design)
        raw = SimpleNamespace(id="r", status="completed", model="m", usage=None,
                              error=None, incomplete_details=None,
                              output=[SimpleNamespace(type="message", role="assistant",
                                      content=[SimpleNamespace(
                                          type="output_text", text="hey", annotations=[])])])
        norm = t.normalize_response(raw)
        self.assertIsInstance(norm, NormalizedResponse)
        self.assertEqual((norm.content or "").strip(), "hey")
        # chat-shaped object -> chat normalizer
        raw2 = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="ho", tool_calls=None), finish_reason="stop")], usage=None)
        norm2 = t.normalize_response(raw2)
        self.assertEqual((norm2.content or "").strip(), "ho")

    def test_build_client_for_ref_unknown_scheme_passthrough(self):
        from advlib.oauth_runtime import build_client_for_ref

        self.assertIsNone(build_client_for_ref("https://api.openai.com/v1"))

    def test_disabled_adapter_raises_pointer(self):
        from advlib.oauth_runtime import build_client_for_ref
        from advlib.adapters import AdapterUnavailable

        os.environ["ADVANCED_ADAPTERS"] = "codex"
        try:
            with self.assertRaises(AdapterUnavailable):
                build_client_for_ref("codex://me@x.com")
        finally:
            del os.environ["ADVANCED_ADAPTERS"]


class TestStoreRoundtrip(unittest.TestCase):
    def test_put_get_delete(self):
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            store.put("codex", "me@x.com", {"access_token": "A", "refresh_token": "R"})
            self.assertEqual(store.get("codex", "me@x.com")["access_token"], "A")
            accts = store.list_accounts()
            self.assertIn(("codex", "me@x.com"), [(p, n) for p, n, _ in accts])
            self.assertTrue(store.delete("codex", "me@x.com"))
            self.assertIsNone(store.get("codex", "me@x.com"))


class TestRefreshProxy(unittest.TestCase):
    """advanced.proxy must also cover the TOKEN REFRESH call.

    On a proxied network the Google refresh endpoint is unreachable directly;
    the pre-2026-10-29 code used urllib (no proxy) and the desktop logged
    "refresh failed ... urlopen error timed out" every minute while the
    provider looked dead.
    """

    def test_refresh_and_persist_forwards_proxy_hint(self):
        seen = {}

        def fake_refresher(acct, rt, proxy=""):
            seen["proxy"] = proxy
            return {"access_token": _jwt(2 ** 31 - 1), "refresh_token": rt}

        real = oauth_tokens._platform_refresher
        oauth_tokens._platform_refresher = lambda p: fake_refresher
        try:
            with tempfile.TemporaryDirectory() as td:
                store = _store(td)
                store.put("antigravity", "me@x.com",
                          {"access_token": _jwt(1), "refresh_token": "RT"})
                oauth_tokens.refresh_and_persist("antigravity", "me@x.com", store,
                                                 proxy_hint="http://127.0.0.1:7890")
        finally:
            oauth_tokens._platform_refresher = real
        self.assertEqual(seen["proxy"], "http://127.0.0.1:7890")

    def test_ctx_carries_proxy_into_refresh(self):
        seen = []
        real = oauth_tokens.refresh_and_persist
        oauth_tokens.refresh_and_persist = lambda *a, **kw: seen.append(kw.get("proxy_hint")) or None
        try:
            with tempfile.TemporaryDirectory() as td:
                store = _store(td)
                # expired token -> token() must attempt the refresh
                store.put("antigravity", "me@x.com",
                          {"access_token": _jwt(1), "refresh_token": "RT", "expired": "1"})
                ctx = oauth_client.OAuthTokenContext("antigravity", "me@x.com", store,
                                                     proxy="http://127.0.0.1:7890")
                with self.assertRaises(RuntimeError):
                    ctx.token()
        finally:
            oauth_tokens.refresh_and_persist = real
        self.assertEqual(seen, ["http://127.0.0.1:7890"])

    def test_antigravity_refresh_honours_proxy(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        from advlib.adapters import antigravity as ag

        class _Tok(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                body = json.dumps({"access_token": "NEW-AT", "expires_in": 3600}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), _Tok)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        real = ag.TOKEN_ENDPOINT
        ag.TOKEN_ENDPOINT = f"http://127.0.0.1:{srv.server_address[1]}/token"
        try:
            acct = {"client_id": "cid", "client_secret": "sec"}
            out = ag.refresh(acct, "RT")                       # no proxy: direct
            self.assertEqual(out["access_token"], "NEW-AT")
            # a dead proxy proves the hint is really applied (not ignored)
            with self.assertRaises(Exception):
                ag.refresh(acct, "RT", proxy="http://127.0.0.1:1")
        finally:
            ag.TOKEN_ENDPOINT = real
            srv.shutdown()
            srv.server_close()


class TestTransportCoreSurface(unittest.TestCase):
    """The core calls transport methods DIRECTLY on every turn — the registered
    class must carry the whole surface, not just build/normalize.

    Live bug 2026-10-30: `AttributeError: 'AdvancedOAuthTransport' object has
    no attribute 'validate_response'`, retried 3x per turn, which read as
    "the OAuth provider is broken" in the UI.
    """

    @classmethod
    def setUpClass(cls):
        import os as _os
        root = _os.environ.get("HERMES_CORE_ROOT") or ""
        if not root:
            # locate the core tree from the usual homes; skip (not fail) if absent
            for cand in (Path.home() / ".hermes" / "hermes-agent",
                         Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"):
                if (cand / "agent").is_dir():
                    root = str(cand)
                    break
        if not root:
            raise unittest.SkipTest("set HERMES_CORE_ROOT to a hermes-agent checkout")
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from agent.transports import get_transport  # noqa: F401
        except Exception as exc:  # different machine / core not vendored
            raise unittest.SkipTest(f"hermes core not importable: {exc}")

    def _transport(self):
        from advlib.oauth_runtime import AdvancedOAuthTransport

        return AdvancedOAuthTransport()

    def test_full_call_surface_present(self):
        t = self._transport()
        for name in ("build_kwargs", "normalize_response", "validate_response",
                     "extract_cache_stats", "map_finish_reason",
                     "response_finish_reason", "preflight_kwargs"):
            self.assertTrue(callable(getattr(t, name, None)),
                            f"transport must expose {name}() — the core calls it by name")

    def test_validate_response_chat_shape(self):
        from types import SimpleNamespace

        t = self._transport()
        ok = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))])
        self.assertTrue(t.validate_response(ok))
        self.assertFalse(t.validate_response(None))
        self.assertFalse(t.validate_response(SimpleNamespace(choices=[])))

    def test_validate_response_responses_shape(self):
        from types import SimpleNamespace

        t = self._transport()
        resp = SimpleNamespace(output=[{"type": "message"}], status="completed")
        self.assertTrue(t.validate_response(resp))
        self.assertFalse(t.validate_response(SimpleNamespace(output=[], status="failed")))

    def test_validate_response_fails_open_when_registry_missing(self):
        import types as _types

        t = self._transport()
        real = sys.modules.get("agent.transports")
        stub = _types.ModuleType("agent.transports")
        stub.get_transport = lambda mode: None
        sys.modules["agent.transports"] = stub
        try:
            self.assertTrue(t.validate_response(object()),
                            "delegation unavailable -> never block a turn")
        finally:
            if real is not None:
                sys.modules["agent.transports"] = real

    def test_preflight_is_identity(self):
        t = self._transport()
        kw = {"model": "m", "messages": [], "stream": True}
        self.assertIs(t.preflight_kwargs(kw, allow_stream=False), kw)

    def test_map_finish_reason_passthrough(self):
        t = self._transport()
        self.assertEqual(t.map_finish_reason("tool_calls"), "tool_calls")
        self.assertEqual(t.map_finish_reason("length"), "length")


class TestCodexStreamPolicy(unittest.TestCase):
    """The official ChatGPT codex backend rejects non-stream requests
    ("Stream must be set to true", live 2026-10-30); relays accept them.
    The facade must handle both without a config knob."""

    class _Responses:
        def __init__(self, mode):
            self.mode = mode
            self.calls = []

        def create(self, **kw):
            self.calls.append(dict(kw))
            if kw.get("stream"):
                return iter([
                    {"type": "response.output_item.done",
                     "item": {"type": "message", "role": "assistant", "status": "completed",
                              "content": [{"type": "output_text", "text": "sse ok"}]}},
                    {"type": "response.completed",
                     "response": {"id": "r1", "status": "completed", "output": []}},
                ])
            if self.mode == "stream_only":
                class _Bad(Exception):
                    status_code = 400

                raise _Bad("Error code: 400 - {'detail': 'Stream must be set to true'}")
            from types import SimpleNamespace as NS

            return NS(output=[NS(type="message", role="assistant", status="completed",
                                 content=[NS(type="output_text", text="plain ok")])],
                      usage=None, status="completed")

    class _Client:
        def __init__(self, mode="plain"):
            self.responses = TestCodexStreamPolicy._Responses(mode)

        def close(self):
            pass

        def is_closed(self):
            return False

    def _facade(self, mode="plain", force_stream=False):
        from advlib.oauth_client import _CompletionsFacade

        client = self._Client(mode)
        return client, _CompletionsFacade(client, None, "codex", force_stream=force_stream)

    def _call(self, facade):
        return facade.create(model="gpt-6-luna",
                             messages=[{"role": "user", "content": "hi"}])

    def test_official_base_forces_stream_upfront(self):
        client, facade = self._facade(force_stream=True)
        resp = self._call(facade)
        self.assertEqual(resp.choices[0].message.content, "sse ok")
        self.assertEqual(len(client.responses.calls), 1)
        self.assertTrue(client.responses.calls[0]["stream"],
                        "official backend: never send a non-stream request")

    def test_relay_uses_plain_and_learns_it(self):
        client, facade = self._facade()
        self.assertEqual(self._call(facade).choices[0].message.content, "plain ok")
        self._call(facade)
        self.assertEqual(len(client.responses.calls), 2)
        self.assertFalse(any(c.get("stream") for c in client.responses.calls),
                         "after a plain success the client must not stream")

    def test_stream_only_upstream_adapts_after_400(self):
        client, facade = self._facade(mode="stream_only")
        self.assertEqual(self._call(facade).choices[0].message.content, "sse ok")
        self.assertFalse(client.responses.calls[0].get("stream"))
        self.assertTrue(client.responses.calls[-1].get("stream"))
        # decided now: the next call goes straight to SSE
        self._call(facade)
        self.assertTrue(client.responses.calls[-1].get("stream"))

    def test_requires_stream_error_matcher(self):
        from advlib.oauth_client import _requires_stream_error

        class E(Exception):
            status_code = 400

        self.assertTrue(_requires_stream_error(E("Stream must be set to true")))
        self.assertFalse(_requires_stream_error(E("invalid api key")))


class TestCodexStreamToolCalls(unittest.TestCase):
    """Streaming is the path the agent actually uses, and the codex backend
    streams tool calls as ``output_item.*`` + argument deltas. Before this the
    facade dropped them entirely: the tool turn yielded nothing and
    finish=stop, so tools silently never ran (live 2026-10-30)."""

    def _chunks(self, events, model="gpt-6-luna"):
        from advlib.oauth_client import _stream_events_to_chunks

        return list(_stream_events_to_chunks(iter(events), model=model))

    def test_function_call_is_emitted_with_finish_tool_calls(self):
        events = [
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                      "name": "now", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{}"},
            {"type": "response.output_item.done",
             "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                      "name": "now", "arguments": "{}"}},
            {"type": "response.completed",
             "response": {"id": "r1", "status": "completed", "output": []}},
        ]
        chunks = self._chunks(events)
        tcs = [tc for c in chunks for tc in (getattr(c.choices[0].delta, "tool_calls", None) or [])]
        self.assertEqual(len(tcs), 1, "the tool call must reach the agent")
        self.assertEqual(tcs[0].function.name, "now")
        self.assertEqual(tcs[0].function.arguments, "{}")
        self.assertEqual(tcs[0].id, "call_1")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "tool_calls")

    def test_text_deltas_stream_then_stop(self):
        events = [{"type": "response.output_text.delta", "delta": "O"},
                  {"type": "response.output_text.delta", "delta": "K"},
                  {"type": "response.completed",
                   "response": {"id": "r2", "status": "completed", "output": []}}]
        chunks = self._chunks(events)
        self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "OK")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    def test_terminal_status_mapping(self):
        base = [{"type": "response.output_text.delta", "delta": "x"}]
        failed = self._chunks(base + [{"type": "response.failed", "response": {"status": "failed"}}])
        self.assertEqual(failed[-1].choices[0].finish_reason, "error")
        incomplete = self._chunks(base + [{"type": "response.incomplete",
                                           "response": {"status": "incomplete"}}])
        self.assertEqual(incomplete[-1].choices[0].finish_reason, "length")

    def test_tool_call_survives_without_core_assembler(self):
        """Fallback path: even if agent.codex_runtime moves, the tool call is
        still assembled locally."""
        import types as _types
        import advlib.oauth_client as oc

        real = oc._new_assembler
        oc._new_assembler = lambda model: None
        try:
            chunks = self._chunks([
                {"type": "response.output_item.done",
                 "item": {"type": "function_call", "id": "fc_9", "call_id": "call_9",
                          "name": "calculator", "arguments": '{"expression":"2+2"}'}},
                {"type": "response.completed", "response": {"status": "completed"}},
            ])
        finally:
            oc._new_assembler = real
        tcs = [tc for c in chunks for tc in (getattr(c.choices[0].delta, "tool_calls", None) or [])]
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0].function.name, "calculator")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "tool_calls")


if __name__ == "__main__":
    unittest.main(verbosity=2)
