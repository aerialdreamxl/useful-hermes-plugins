"""Fingerprint resolution: which scheme actually supplies each platform's identity.

User contract (2026-10-30):

- ``oauth_fingerprint: cpa``     → CPA source port
- ``oauth_fingerprint: sub2api`` → Sub2API source port
- ``oauth_fingerprint: hermes``  → Hermes' own scheme when it has one;
  otherwise fall back to CPA, then Sub2API, then report an error (never
  silently leave the request with a default identity).

The interesting cases are the gaps in the host: Hermes core has no antigravity
client at all, so ``hermes`` MUST land on CPA there instead of doing nothing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from advlib import fingerprints  # noqa: E402


def _req(platform: str) -> httpx.Request:
    url = {"codex": "https://chatgpt.com/backend-api/codex/responses",
           "claude": "https://api.anthropic.com/v1/messages",
           "antigravity": "https://cloudcode-pa.googleapis.com/v1internal:generateContent"}[platform]
    return httpx.Request("POST", url, headers={"User-Agent": "placeholder/0", "Authorization": "Bearer tok"})


def _stub_core_claude_version(version: str):
    """Force core's detected Claude Code version (it normally probes PATH)."""
    import contextlib
    import sys
    import types

    @contextlib.contextmanager
    def _ctx():
        saved = sys.modules.get("agent.anthropic_adapter")
        if "agent" not in sys.modules:
            pkg = types.ModuleType("agent")
            pkg.__path__ = []  # type: ignore[attr-defined]
            sys.modules["agent"] = pkg
        mod = types.ModuleType("agent.anthropic_adapter")
        mod._get_claude_code_version = lambda: version  # type: ignore[attr-defined]
        sys.modules["agent.anthropic_adapter"] = mod
        try:
            yield
        finally:
            if saved is None:
                sys.modules.pop("agent.anthropic_adapter", None)
            else:
                sys.modules["agent.anthropic_adapter"] = saved

    return _ctx()


class TestResolutionChain(unittest.TestCase):
    def test_cpa_everywhere(self):
        for platform in ("codex", "claude", "antigravity"):
            mod, effective, skipped = fingerprints.resolve(platform, "cpa")
            self.assertEqual(effective, "cpa", f"{platform}: cpa must win outright")
            self.assertEqual(skipped, [])

    def test_sub2api_everywhere(self):
        for platform in ("codex", "claude", "antigravity"):
            mod, effective, skipped = fingerprints.resolve(platform, "sub2api")
            self.assertEqual(effective, "sub2api", f"{platform}: sub2api has a port")
            self.assertEqual(skipped, [])

    def test_hermes_keeps_codex(self):
        mod, effective, skipped = fingerprints.resolve("codex", "hermes")
        self.assertEqual(effective, "hermes", "host identity exists")
        self.assertEqual(skipped, [])

    def test_hermes_claude_native_when_core_version_is_current(self):
        with _stub_core_claude_version("2.1.280"):
            mod, effective, skipped = fingerprints.resolve("claude", "hermes")
        self.assertEqual(effective, "hermes")
        self.assertEqual(skipped, [])

    def test_hermes_claude_falls_back_when_core_version_is_stale(self):
        """Core's fallback constant (2.1.74) is what a desktop without the CLI
        reports, and Anthropic 400s on such a UA — so CPA must take over."""
        with _stub_core_claude_version("2.1.74"):
            mod, effective, skipped = fingerprints.resolve("claude", "hermes")
            req = _req("claude")
            fingerprints.apply(req, platform="claude", account={}, phase="inference",
                               name="hermes", access_token="")
        self.assertEqual(effective, "cpa")
        self.assertEqual(skipped, ["hermes:no-identity"])
        self.assertEqual(req.headers["User-Agent"], "claude-cli/2.1.280 (external, cli)")

    def test_hermes_antigravity_falls_back_to_cpa(self):
        mod, effective, skipped = fingerprints.resolve("antigravity", "hermes")
        self.assertEqual(effective, "cpa", "core has no antigravity client")
        self.assertEqual(skipped, ["hermes:no-identity"])

    def test_unknown_profile_falls_back(self):
        mod, effective, skipped = fingerprints.resolve("codex", "does-not-exist")
        self.assertEqual(effective, "hermes")
        self.assertIn("does-not-exist:missing", skipped)

    def test_nothing_covers_platform_is_reported(self):
        mod, effective, skipped = fingerprints.resolve("gemini", "hermes")
        self.assertIsNone(mod)
        self.assertEqual(effective, "")
        for name in ("hermes", "cpa", "sub2api"):
            self.assertIn(f"{name}:no-identity", skipped)

    def test_describe_shape(self):
        info = fingerprints.describe("antigravity", "hermes")
        self.assertTrue(info["ok"])
        self.assertEqual(info["effective"], "cpa")
        self.assertEqual(info["requested"], "hermes")


class TestAppliedIdentity(unittest.TestCase):
    def test_hermes_antigravity_gets_cpa_identity(self):
        req = _req("antigravity")
        fingerprints.apply(req, platform="antigravity", account={}, phase="inference",
                           name="hermes", access_token="")
        self.assertIn("antigravity/hub/", req.headers["User-Agent"])

    def test_hermes_claude_uses_core_identity(self):
        req = _req("claude")
        with _stub_core_claude_version("2.1.280"):
            fingerprints.apply(req, platform="claude", account={}, phase="inference",
                               name="hermes", access_token="")
        self.assertEqual(req.headers["User-Agent"], "claude-code/2.1.280 (external, cli)")
        self.assertEqual(req.headers["x-app"], "cli")
        self.assertNotIn("X-Stainless-Package-Version", req.headers,
                         "hermes profile must not borrow CPA's stainless set")

    def test_sub2api_claude_headers(self):
        req = _req("claude")
        fingerprints.apply(req, platform="claude", account={}, phase="inference",
                           name="sub2api", access_token="")
        self.assertEqual(req.headers["User-Agent"], "claude-cli/2.1.258 (external, cli)")
        self.assertEqual(req.headers["X-Stainless-Package-Version"], "0.94.0")
        self.assertEqual(req.headers["X-App"], "cli")

    def test_sub2api_antigravity_ua(self):
        req = _req("antigravity")
        fingerprints.apply(req, platform="antigravity", account={}, phase="inference",
                           name="sub2api", access_token="")
        self.assertEqual(req.headers["User-Agent"], "antigravity/hub/2.23.0 windows/amd64")

    def test_sub2api_version_overrides(self):
        req = _req("antigravity")
        fingerprints.apply(req, platform="antigravity",
                           account={"fingerprint_version_antigravity": "3.4.5"},
                           phase="inference", name="sub2api", access_token="")
        self.assertEqual(req.headers["User-Agent"], "antigravity/hub/3.4.5 windows/amd64")

    def test_uncovered_platform_leaves_request_alone(self):
        req = _req("claude")
        before = dict(req.headers)
        fingerprints.apply(req, platform="gemini", account={}, phase="inference",
                           name="hermes", access_token="")
        self.assertEqual(dict(req.headers), before, "no identity invented out of thin air")


if __name__ == "__main__":
    unittest.main(verbosity=2)
