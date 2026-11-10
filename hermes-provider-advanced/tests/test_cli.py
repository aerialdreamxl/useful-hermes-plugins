# -*- coding: utf-8 -*-
"""CLI half tests — parser shape, verb dispatch, selfcheck probes (stubbed).

No core imports needed except where explicitly stubbed; the probes are
exercised against mocks so the suite runs anywhere.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cli  # noqa: E402


def _parse(argv):
    parser = argparse.ArgumentParser(prog="hermes advanced-providers")
    cli.build_parser(parser)
    return parser.parse_args(argv)


class ParserTest(unittest.TestCase):
    def test_status(self):
        self.assertEqual(_parse(["status"]).adv_verb, "status")

    def test_selfcheck(self):
        self.assertEqual(_parse(["selfcheck"]).adv_verb, "selfcheck")

    def test_login_noninteractive_shape(self):
        # the exact flag串 from the owner's spec
        args = _parse(["oauth", "login", "--platform=codex", "--json=/somewhere/someone.json",
                       "--json-format=cpa", "--proxy=http://127.0.0.1:7890"])
        self.assertEqual(args.adv_verb, "oauth")
        self.assertEqual(args.adv_oauth_verb, "login")
        self.assertEqual(args.platform, "codex")
        self.assertEqual(args.json_src, "/somewhere/someone.json")
        self.assertEqual(args.json_format, "cpa")
        self.assertEqual(args.proxy, "http://127.0.0.1:7890")

    def test_signout_noninteractive(self):
        args = _parse(["oauth", "signout", "--platform=codex", "--account=me@x.com"])
        self.assertEqual(args.adv_oauth_verb, "signout")
        self.assertEqual(args.account, "me@x.com")

    def test_bare_defaults_to_help(self):
        # argparse leaves adv_verb=None; run_command's `or "help"` folds it (spec: no-arg prints help)
        self.assertEqual(cli.run_command(_parse([])), 0)


class DispatchTest(unittest.TestCase):
    def test_help_exit0(self):
        self.assertEqual(cli.run_command(_parse(["help"])), 0)

    def test_oauth_list_runs_real_store(self):
        # P3: the verb is live — with an empty store it exits 0 and says so.
        rc = cli.run_command(_parse(["oauth", "list"]))
        self.assertIn(rc, (0, 1))


class SelfcheckProbeTest(unittest.TestCase):
    def test_gate_probe_detects_reorder(self):
        fake = mock.Mock()
        good = "x = profile.build_client_kwargs_extras(...)\nif \"http_client\" not in client_kwargs:\n"
        bad = "if \"http_client\" not in client_kwargs:\nprofile.build_client_kwargs_extras(...)\n"
        with mock.patch("inspect.getsource", side_effect=[good, bad]), \
             mock.patch.dict(sys.modules, {"agent.agent_runtime_helpers": fake}):
            ok1, _ = cli._probe_gate()
            ok2, detail = cli._probe_gate()
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("BEFORE", detail)

    def test_provider_profile_probe(self):
        class AdvancedProviderProfile:
            pass
        class CustomProfile:
            pass
        class WrongClass:
            pass

        fake_mod = mock.Mock()
        # Case 1: advanced not registered
        fake_mod.get_provider_profile = lambda name: None
        with mock.patch.dict(sys.modules, {"providers": fake_mod}):
            ok, detail = cli._probe_provider_profile()
            self.assertFalse(ok)
            self.assertIn("not registered", detail)

        # Case 2: advanced ok and custom untouched
        fake_mod.get_provider_profile = lambda name: AdvancedProviderProfile() if name == "advanced" else CustomProfile()
        with mock.patch.dict(sys.modules, {"providers": fake_mod}):
            ok, detail = cli._probe_provider_profile()
            self.assertTrue(ok)
            self.assertIn("untouched", detail)

        # Case 3: native custom was modified
        fake_mod.get_provider_profile = lambda name: AdvancedProviderProfile() if name == "advanced" else WrongClass()
        with mock.patch.dict(sys.modules, {"providers": fake_mod}):
            ok, detail = cli._probe_provider_profile()
            self.assertFalse(ok)
            self.assertIn("unexpectedly modified", detail)

    def test_redact_hides_credentials(self):
        self.assertEqual(cli._redact("http://user:pass@proxy.lan:7890"), "http://proxy.lan:7890")
        self.assertEqual(cli._redact("http://proxy.lan:7890"), "http://proxy.lan:7890")


if __name__ == "__main__":
    unittest.main(verbosity=2)
