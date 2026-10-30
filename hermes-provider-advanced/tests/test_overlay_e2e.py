# -*- coding: utf-8 -*-
"""P1 overlay E2E — real core discovery chain, synthetic HERMES_HOME.

Proves, against the ACTUAL providers/__init__.py scan machinery:
1. bundled discovery registers the stock CustomProfile as ``custom``
2. our user-layer overlay dir is scanned and REPLACES it (user wins)
3. ``custom:<name>`` route also resolves to the overlay (lookup fallback)
4. the overlay's build_client_kwargs_extras returns an ``http_client`` for a
   proxied api entry and {} otherwise — through the real hook contract
5. the core's ``create_openai_client`` setdefaults our http_client ahead of
   its own keepalive gate (ordering probe on real source)

No production config/auth touched: HERMES_HOME points at a temp dir whose
config.yaml is synthesized; advlib is loaded via HERMES_ADVANCED_LIB_DIR.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent          # hermes-provider-advanced/
import os as _os

_core_env = _os.environ.get("HERMES_CORE_ROOT", "")
CORE = Path(_core_env) if _core_env else next(
    (c for c in (Path.home() / ".hermes" / "hermes-agent",
                 Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent")
     if (c / "agent").is_dir()), Path("hermes-agent-not-found"))  # source tree (read-only use)

TMP = Path(tempfile.mkdtemp(prefix="adv-e2e-"))
HOME = TMP / "home"
(HOME / "plugins" / "model-providers").mkdir(parents=True)
(HOME / "plugins" / "model-providers" / "hermes-advanced-custom").mkdir(parents=True)

# deploy the overlay + point advlib at the dev tree
for f in (ROOT / "model-providers" / "hermes-advanced-custom").iterdir():
    if not f.is_file():
        continue  # skip __pycache__ and any other directory
    shutil.copy2(f, HOME / "plugins" / "model-providers" / "hermes-advanced-custom" / f.name)

os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_ADVANCED_LIB_DIR"] = str(ROOT / "advlib")
sys.path.insert(0, str(CORE))

CFG_YAML = textwrap.dedent("""
    providers:
      proxied:
        base_url: https://api.example.com/v1
        advanced:
          proxy: "http://127.0.0.1:7890"
          auth_mode: api
      lan:
        base_url: http://vllm.lan:8000/v1
        advanced:
          proxy: "http://127.0.0.1:7890"
      codex-oauth:
        base_url: codex-oauth
        advanced:
          auth_mode: oauth
          oauth_platform: codex
          oauth_account: "codex:me@example.com"
""")
(HOME / "config.yaml").write_text(CFG_YAML, encoding="utf-8")

# ---------------------------------------------------------------- discovery

import providers  # noqa: E402  (core, from CORE on sys.path)

providers._discover_providers()

# force the user-layer scan by resolving a profile (triggers _home_layer)
from providers import get_provider_profile  # noqa: E402

custom = get_provider_profile("custom")


class OverlayE2ETest(unittest.TestCase):
    def test_overlay_replaced_bundled(self):
        self.assertIsNotNone(custom)
        self.assertEqual(type(custom).__name__, "AdvancedCustomProfile")

    def test_bundled_behaviour_preserved(self):
        # reasoning-effort vocabulary + default from the bundled profile survive
        efforts = custom.supported_reasoning_efforts("any-model")
        self.assertTrue(efforts)
        self.assertIsInstance(custom.default_reasoning_config(), dict)

    def test_custom_route_alias_resolves_overlay(self):
        p = get_provider_profile("custom:proxied")
        self.assertEqual(type(p).__name__, "AdvancedCustomProfile")

    def test_hook_returns_http_client_for_proxied_entry(self):
        try:
            import httpx
        except Exception:
            self.skipTest("httpx missing")
        extras = custom.build_client_kwargs_extras(base_url="https://api.example.com/v1")
        self.assertIn("http_client", extras)
        client = extras["http_client"]
        self.assertIsInstance(client, httpx.Client)
        self.assertIsNone(client._timeout.read)  # SSE lifeline through the real chain
        client.close()

    def test_hook_empty_for_unproxied_and_oauth(self):
        self.assertEqual(custom.build_client_kwargs_extras(base_url="https://plain.example.com/v1"), {})
        self.assertEqual(custom.build_client_kwargs_extras(base_url="codex-oauth"), {})

    def test_gate_ordering_in_real_source(self):
        import inspect

        import agent.agent_runtime_helpers as arh

        src = inspect.getsource(arh.create_openai_client)
        hook_at = src.find("build_client_kwargs_extras")
        gate_at = src.find('if "http_client" not in client_kwargs')
        self.assertGreater(hook_at, -1)
        self.assertGreater(gate_at, -1)
        self.assertLess(hook_at, gate_at)

    def test_hook_failure_never_kills_extras(self):
        advlib_mod = sys.modules.get("hermes_advlib")
        self.assertIsNotNone(advlib_mod)
        with mock.patch.object(advlib_mod, "client_extras_for_base_url", side_effect=RuntimeError("boom")):
            # fail-open: hook must still return the bundled extras, no raise
            got = custom.build_client_kwargs_extras(base_url="https://api.example.com/v1")
            self.assertNotIn("http_client", got)


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False).result
    os.environ.pop("HERMES_HOME", None)
    os.environ.pop("HERMES_ADVANCED_LIB_DIR", None)
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
