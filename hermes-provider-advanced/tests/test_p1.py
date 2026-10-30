# -*- coding: utf-8 -*-
"""P1 test suite — route table, proxy factory, client_extras wiring.

Runs under the Hermes runtime venv (httpx present) AND under any python with
httpx (the few tests needing it skip cleanly without). Core modules are
stubbed via sys.modules injection where the plugin lazy-imports them, so the
suite never touches the live config.yaml or auth.json.

Run:  python tests/test_p1.py
Exit: 0 = all pass.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import httpx  # noqa: F401

    HAVE_HTTPX = True
except Exception:
    HAVE_HTTPX = False


# ---------------------------------------------------------------- stubs

def _install_core_stubs() -> dict:
    """Inject minimal core-module stubs the plugin lazy-imports."""
    stubs: dict[str, types.ModuleType] = {}

    route = types.ModuleType("hermes_cli.route_identity")

    def normalize_route_base_url(base_url):
        raw = str(base_url or "").strip()
        if not raw:
            return ""
        low = raw.lower().rstrip("/")
        return low

    route.normalize_route_base_url = normalize_route_base_url
    stubs["hermes_cli.route_identity"] = route

    bypass = types.ModuleType("agent.proxy_bypass")

    def should_bypass_proxy(target_hosts, *, no_proxy_value=None):
        host = str(target_hosts or "")
        return "lan" in host or "192.168." in host

    bypass.should_bypass_proxy = should_bypass_proxy
    stubs["agent.proxy_bypass"] = bypass

    sslv = types.ModuleType("agent.ssl_verify")

    def resolve_httpx_verify(*, ca_bundle=None, ssl_verify=None, base_url=""):
        if ssl_verify is False:
            return False
        if ca_bundle:
            return str(ca_bundle)
        return True

    sslv.resolve_httpx_verify = resolve_httpx_verify
    stubs["agent.ssl_verify"] = sslv

    utils = types.ModuleType("utils")

    def normalize_proxy_url(value):
        raw = str(value or "").strip()
        if not raw:
            return None
        return raw if "://" in raw else f"http://{raw}"

    utils.normalize_proxy_url = normalize_proxy_url
    stubs["utils"] = utils

    hc = types.ModuleType("hermes_cli")
    hc.__path__ = []  # type: ignore[attr-defined]
    stubs["hermes_cli"] = hc
    hcfg = types.ModuleType("hermes_cli.config")

    def load_config_readonly():
        raise RuntimeError("tests must not read the live config")

    hcfg.load_config_readonly = load_config_readonly
    stubs["hermes_cli.config"] = hcfg
    ag = types.ModuleType("agent")
    ag.__path__ = []  # type: ignore[attr-defined]
    stubs["agent"] = ag

    for name, mod in stubs.items():
        sys.modules.setdefault(name, mod)
    return stubs


_install_core_stubs()

from advlib import config_scan  # noqa: E402
from advlib import proxy_factory  # noqa: E402
import advlib  # noqa: E402

# ---------------------------------------------------------------- fixtures

CFG = {
    "providers": {
        "my-proxy-api": {
            "base_url": "https://api.example.com/v1",
            "advanced": {"proxy": "http://127.0.0.1:7890", "auth_mode": "api"},
        },
        "lan-local": {
            "base_url": "http://vllm.lan:8000/v1",
            "advanced": {"proxy": "http://127.0.0.1:7890"},
        },
        "codex-oauth": {
            # v3 primary: dialect + scheme reference, no advanced.auth_mode
            "base_url": "codex://me@example.com",
            "api_mode": "advanced_oauth",
            "advanced": {
                "proxy": "http://127.0.0.1:7890",
                "oauth_fingerprint": "cpa",
            },
        },
        "codex-oauth-legacy-alias": {
            # v1/v2 alias shape still parsed
            "base_url": "whatever",
            "advanced": {
                "auth_mode": "oauth",
                "oauth_platform": "claude",
                "oauth_account": "claude:old@x.com",
            },
        },
        "oauth-bare-dialect": {
            # scheme reference alone marks oauth (no advanced: block at all)
            "base_url": "claude://b%40x.com",
            "api_mode": "advanced_oauth",
        },
        "no-advanced": {"base_url": "https://plain.example.com/v1"},
        "dupe-route": {
            "base_url": "https://api.example.com/v1/",  # same route as my-proxy-api
            "advanced": {"proxy": "http://other.proxy:1080"},
        },
    },
    "custom_providers": [
        {
            "name": "legacy-tls",
            "base_url": "https://legacy.example.com/v1",
            "ssl_verify": False,
            "advanced": {"proxy": "http://127.0.0.1:7890"},
        },
    ],
    "provider": {  # singular trap: dict under the active-provider key
        "typo-entry": {"base_url": "https://x.example/v1", "advanced": {"proxy": "http://p:1"}},
    },
}


class RouteTableTest(unittest.TestCase):
    def setUp(self):
        config_scan.invalidate_cache()
        self.table = config_scan.RouteTable._build(CFG)

    def test_api_entry_routed(self):
        e = self.table.lookup("https://api.example.com/v1")
        self.assertIsNotNone(e)
        self.assertEqual(e.provider_key, "my-proxy-api")
        self.assertEqual(e.proxy, "http://127.0.0.1:7890")
        self.assertEqual(e.auth_mode, "api")

    def test_trailing_slash_route_equivalence(self):
        self.assertIsNotNone(self.table.lookup("https://api.example.com/v1/"))

    def test_oauth_entries_excluded_from_route_table(self):
        # R4'': scheme base_url must never match api lookups
        self.assertIsNone(self.table.lookup("codex://me@example.com"))
        self.assertEqual(len(self.table.by_oauth_ref), 3)
        e = self.table.lookup_oauth_ref("codex://me@example.com")
        self.assertEqual(e.provider_key, "codex-oauth")
        self.assertEqual(e.fingerprint, "cpa")
        # URL-decoded account: b%40x.com keys the same slot as b@x.com
        self.assertEqual(self.table.by_oauth[("claude", "b@x.com")].provider_key, "oauth-bare-dialect")
        # v1 alias still resolves (platform+account -> reference string)
        self.assertEqual(self.table.by_oauth[("claude", "old@x.com")].provider_key,
                         "codex-oauth-legacy-alias")

    def test_default_auth_mode_api(self):
        e = self.table.lookup("http://vllm.lan:8000/v1")
        self.assertIsNotNone(e)
        self.assertEqual(e.auth_mode, "api")

    def test_unadvanced_entry_absent(self):
        self.assertIsNone(self.table.lookup("https://plain.example.com/v1"))

    def test_duplicate_route_first_wins_with_warning(self):
        e = self.table.lookup("https://api.example.com/v1")
        self.assertEqual(e.proxy, "http://127.0.0.1:7890")  # first declarer
        self.assertTrue(any("duplicate route" in w for w in self.table.warnings))

    def test_legacy_list_shape_consumed(self):
        e = self.table.lookup("https://legacy.example.com/v1")
        self.assertIsNotNone(e)
        self.assertIs(e.ssl_verify, False)

    def test_singular_provider_trap_warned(self):
        self.assertTrue(any("providers:" in w and "singular" in w for w in self.table.warnings))

    def test_from_config_explicit_bypasses_live_config(self):
        t = config_scan.RouteTable.from_config(config={"providers": {}})
        self.assertEqual(len(t.all_entries), 0)


class ProxyDecisionTest(unittest.TestCase):
    def setUp(self):
        config_scan.invalidate_cache()
        self.table = config_scan.RouteTable._build(CFG)

    def test_proxy_used(self):
        e = self.table.lookup("https://api.example.com/v1")
        proxy, reason = proxy_factory.proxy_decision(e, "https://api.example.com/v1")
        self.assertEqual((proxy, reason), ("http://127.0.0.1:7890", "proxy"))

    def test_no_proxy_bypass_r3(self):
        e = self.table.lookup("http://vllm.lan:8000/v1")
        proxy, reason = proxy_factory.proxy_decision(e, "http://vllm.lan:8000/v1")
        self.assertEqual((proxy, reason), ("", "bypass"))

    def test_no_advanced_none(self):
        class Bare:
            proxy = ""
        self.assertEqual(proxy_factory.proxy_decision(Bare(), "https://x/v1"), ("", "none"))


class VerifyTest(unittest.TestCase):
    def setUp(self):
        config_scan.invalidate_cache()
        self.table = config_scan.RouteTable._build(CFG)

    def test_ssl_verify_false_propagates(self):
        e = self.table.lookup("https://legacy.example.com/v1")
        self.assertIs(proxy_factory.resolve_verify(e, "https://legacy.example.com/v1"), False)

    def test_default_verify_true(self):
        e = self.table.lookup("https://api.example.com/v1")
        self.assertIs(proxy_factory.resolve_verify(e, "https://api.example.com/v1"), True)


@unittest.skipUnless(HAVE_HTTPX, "httpx not available")
class ProxyClientFactoryTest(unittest.TestCase):
    def test_client_carries_core_semantics(self):
        c = proxy_factory.build_proxy_client("http://127.0.0.1:7890")
        self.assertIsNotNone(c)
        try:
            import httpx
            self.assertEqual(c._timeout.connect, 15.0)
            self.assertIsNone(c._timeout.read)  # SSE lifeline
            self.assertEqual(c._timeout.write, 15.0)
            self.assertEqual(c._timeout.pool, 10.0)
            # limits ride on the transports (httpx 0.28: no client._limits)
            pool = c._transport._pool
            self.assertEqual(pool._max_keepalive_connections, 20)
            self.assertEqual(pool._max_connections, 100)
            self.assertEqual(pool._keepalive_expiry, 20.0)
            # proxy actually mounted (httpx wraps it in an HTTPProxy transport)
            self.assertIsInstance(c._transport, httpx.HTTPTransport)
        finally:
            c.close()

    def test_empty_proxy_none(self):
        self.assertIsNone(proxy_factory.build_proxy_client(""))

    def test_bad_proxy_url_fail_open_none(self):
        self.assertIsNone(proxy_factory.build_proxy_client("not a url :://::"))

    def test_probe_unreachable_proxy(self):
        # port 1 on loopback: fast deterministic refusal
        ok, detail = proxy_factory.probe_proxy("http://127.0.0.1:1")
        self.assertFalse(ok)
        self.assertIn("127.0.0.1:1", detail)


class ClientExtrasTest(unittest.TestCase):
    def setUp(self):
        config_scan.invalidate_cache()

    def _patched_config(self):
        return mock.patch.object(config_scan, "_load_raw_config", return_value=CFG)

    def test_extras_empty_without_advanced(self):
        with self._patched_config():
            config_scan.invalidate_cache()
            self.assertEqual(advlib.client_extras_for_base_url("https://plain.example.com/v1"), {})

    def test_extras_http_client_for_proxied_api_entry(self):
        with self._patched_config(), mock.patch.object(
                proxy_factory, "build_proxy_client", return_value=mock.sentinel.client):
            config_scan.invalidate_cache()
            got = advlib.client_extras_for_base_url("https://api.example.com/v1")
            self.assertEqual(got, {"http_client": mock.sentinel.client})

    def test_extras_empty_for_oauth_entry_in_p1(self):
        with self._patched_config(), mock.patch.object(
                proxy_factory, "build_proxy_client", return_value=mock.sentinel.client):
            config_scan.invalidate_cache()
            # placeholder must not route; even a direct hit returns {} in P1
            self.assertEqual(advlib.client_extras_for_base_url("codex-oauth"), {})

    def test_extras_empty_on_bypass(self):
        with self._patched_config(), mock.patch.object(
                proxy_factory, "build_proxy_client", return_value=mock.sentinel.client):
            config_scan.invalidate_cache()
            self.assertEqual(advlib.client_extras_for_base_url("http://vllm.lan:8000/v1"), {})

    def test_extras_empty_when_factory_fails(self):
        with self._patched_config(), mock.patch.object(
                proxy_factory, "build_proxy_client", return_value=None):
            config_scan.invalidate_cache()
            self.assertEqual(advlib.client_extras_for_base_url("https://api.example.com/v1"), {})

    def test_never_raises_on_garbage(self):
        self.assertEqual(advlib.client_extras_for_base_url(None), {})
        self.assertEqual(advlib.client_extras_for_base_url(12345), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
