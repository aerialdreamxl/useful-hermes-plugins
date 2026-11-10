# -*- coding: utf-8 -*-
"""Standalone Advanced Provider E2E — real core discovery chain, synthetic HERMES_HOME.

Proves against the ACTUAL providers/__init__.py machinery:
1. Native `custom` remains untouched (bundled CustomProfile).
2. Standalone `advanced` profile is registered with `advanced:<identifier>` aliases.
3. Requests to `advanced:proxied` resolve directly to AdvancedProviderProfile.
4. Proxy `http_client` is injected for proxied entries in providers-advanced:.
5. Zero core monkey-patching, zero overlay directories needed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_core_env = os.environ.get("HERMES_CORE_ROOT", "")
CORE = Path(_core_env) if _core_env else next(
    (c for c in (Path.home() / ".hermes" / "hermes-agent",
                 Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent")
     if (c / "agent").is_dir()), Path("hermes-agent-not-found"))

TMP = Path(tempfile.mkdtemp(prefix="adv-standalone-e2e-"))
HOME = TMP / "home"
HOME.mkdir(parents=True)

os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_ADVANCED_LIB_DIR"] = str(ROOT / "advlib")
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CFG_YAML = textwrap.dedent("""
    providers-advanced:
      proxied:
        name: "My Proxied Endpoint"
        base_url: https://api.example.com/v1
        advanced:
          proxy: "http://127.0.0.1:7890"
          auth_mode: api
      codex-oauth:
        name: "[OAUTH] ChatGPT Codex"
        base_url: "codex://me@example.com"
        api_mode: advanced_oauth

    providers:
      native-plain:
        base_url: https://plain.example.com/v1
""")
(HOME / "config.yaml").write_text(CFG_YAML, encoding="utf-8")


class StandaloneProviderE2ETest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import advlib.provider_profile as pp
        import advlib.config_scan as cs
        import advlib.transport_patches as tp
        cs.invalidate_cache()
        tp.install()
        # Register the standalone profile
        pp.build_and_register_advanced_profile()

    def test_native_custom_profile_untouched(self):
        """Native custom profile must remain standard CustomProfile, not overridden."""
        from providers import get_provider_profile
        custom_prof = get_provider_profile("custom")
        self.assertIsNotNone(custom_prof)
        self.assertEqual(type(custom_prof).__name__, "CustomProfile")

    def test_advanced_standalone_profile_registered(self):
        """Standalone 'advanced' profile is registered independently."""
        from providers import get_provider_profile
        adv_prof = get_provider_profile("advanced")
        self.assertIsNotNone(adv_prof)
        self.assertEqual(type(adv_prof).__name__, "AdvancedProviderProfile")

    def test_advanced_slug_routes_to_advanced_profile(self):
        """'advanced:proxied' and 'advanced:codex-oauth' resolve to AdvancedProviderProfile."""
        from providers import get_provider_profile
        p1 = get_provider_profile("advanced:proxied")
        self.assertIsNotNone(p1)
        self.assertEqual(type(p1).__name__, "AdvancedProviderProfile")

        p2 = get_provider_profile("advanced:codex-oauth")
        self.assertIsNotNone(p2)
        self.assertEqual(type(p2).__name__, "AdvancedProviderProfile")

    def test_proxy_injection_on_advanced_route(self):
        """AdvancedProviderProfile injects proxy http_client for configured base_url."""
        from providers import get_provider_profile
        prof = get_provider_profile("advanced:proxied")
        extras = prof.build_client_kwargs_extras(base_url="https://api.example.com/v1")
        self.assertIn("http_client", extras)
        self.assertIsNotNone(extras["http_client"])

        # Native unproxied url returns empty
        plain_extras = prof.build_client_kwargs_extras(base_url="https://plain.example.com/v1")
        self.assertEqual(plain_extras, {})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
