"""Tests for independent provider profile registration and hermes model picker discovery."""

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import advlib
from advlib import config_scan as cs
from advlib.provider_profile import (
    build_and_register_advanced_profile,
    ensure_model_provider_bridge,
    AdvancedProviderProfile,
)


class ModelDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adv-model-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)
        self.orig_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        cs.invalidate_cache()

    def tearDown(self):
        if self.orig_home is not None:
            os.environ["HERMES_HOME"] = self.orig_home
        else:
            os.environ.pop("HERMES_HOME", None)
        cs.invalidate_cache()

    def test_multi_provider_batch_registration(self):
        cfg_yaml = textwrap.dedent("""
            providers-advanced:
              pxy-deepseek:
                name: "DeepSeek Proxy"
                base_url: "https://api.deepseek.com/v1"
                advanced:
                  proxy: "http://127.0.0.1:7890"
              oauth-codex:
                name: "[OAUTH] ChatGPT Codex"
                base_url: "codex://me@example.com"
                api_mode: "advanced_oauth"
                models:
                  gpt-6-luna: {}
                  gpt-5.6-terra: {}
                advanced:
                  oauth_fingerprint: "cpa"
        """)
        (self.home / "config.yaml").write_text(cfg_yaml, encoding="utf-8")
        cs.invalidate_cache()

        registered = build_and_register_advanced_profile()
        reg_names = [p.name for p in registered]

        # 包含每个条目的独立 slug 和全局 advanced
        self.assertIn("advanced:pxy-deepseek", reg_names)
        self.assertIn("advanced:oauth-codex", reg_names)
        self.assertIn("advanced", reg_names)

        # 验证每个 profile 的 display_name
        p_ds = next(p for p in registered if p.name == "advanced:pxy-deepseek")
        self.assertEqual(p_ds.display_name, "[ADVANCED] DeepSeek Proxy")
        self.assertEqual(p_ds.description, "[ADVANCED] DeepSeek Proxy")

        p_oauth = next(p for p in registered if p.name == "advanced:oauth-codex")
        self.assertEqual(p_oauth.display_name, "[ADVANCED] [OAUTH] ChatGPT Codex")
        # 验证 models 优先取配置中的列表
        models = p_oauth.fetch_models()
        self.assertEqual(models, ["gpt-6-luna", "gpt-5.6-terra"])

    def test_ensure_model_provider_bridge(self):
        self.assertTrue(ensure_model_provider_bridge())
        bridge_dir = self.home / "plugins" / "model-providers" / "hermes-provider-advanced"
        self.assertTrue((bridge_dir / "plugin.yaml").is_file())
        self.assertTrue((bridge_dir / "__init__.py").is_file())

        # 再次调用应幂等成功
        self.assertTrue(ensure_model_provider_bridge())

    def test_hermes_model_picker_rows_visibility(self):
        cfg_yaml = textwrap.dedent("""
            providers-advanced:
              pxy-1:
                name: "Alpha Proxy"
                base_url: "https://api.alpha.com/v1"
                advanced:
                  proxy: "http://127.0.0.1:7890"
        """)
        (self.home / "config.yaml").write_text(cfg_yaml, encoding="utf-8")
        cs.invalidate_cache()

        build_and_register_advanced_profile()

        from hermes_cli.config import load_config
        from hermes_cli.main import _named_custom_provider_map, _build_provider_picker_rows
        from hermes_cli.models import _PROVIDER_LABELS

        cfg = load_config()
        custom_map = _named_custom_provider_map(cfg)
        labels = dict(_PROVIDER_LABELS)
        rows, default_idx = _build_provider_picker_rows(cfg, "", labels, custom_map)

        # 检查 rows 中是否有 advanced:pxy-1
        row_slugs = [r[0] for r in rows]
        row_labels = [r[1] for r in rows]

        self.assertIn("advanced:pxy-1", row_slugs)
        matching_label = next(l for s, l, _ in rows if s == "advanced:pxy-1")
        self.assertIn("[ADVANCED] Alpha Proxy", matching_label)


if __name__ == "__main__":
    unittest.main()
