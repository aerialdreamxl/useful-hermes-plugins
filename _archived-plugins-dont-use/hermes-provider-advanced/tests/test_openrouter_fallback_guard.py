# -*- coding: utf-8 -*-
"""block-custom-openrouter-fallback 闸门测试。

覆盖四组契约：
1. should_block 判定（裸 custom/auto 落 OpenRouter → 拦；显式 openrouter / 非 OR 端点 /
   config 显式镜像 → 放行）
2. setting_enabled 设置读取（官方 ctx 通道优先；裸名兜底；缺省 false；字符串真值）
3. _wrap 行为（开关开 → AuthError(code=openrouter_fallback_blocked)；开关关 → 原样透传）
4. install 状态机（wrapped → already；无模块 → missing）

Run:  <Hermes venv python> tests/test_openrouter_fallback_guard.py
Exit: 0 = all pass.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from advlib import fallback_guard as fg  # noqa: E402


class _FakeCtx:
    def __init__(self, values=None, plugin_id="custom-plugins/hermes-provider-advanced"):
        self._values = dict(values or {})
        self.plugin_id = plugin_id

    def get_config(self, key, default=None):
        return self._values.get(key, default)


class ShouldBlockTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = fg._configured_base_url
        fg._configured_base_url = lambda: ""

    def tearDown(self):
        fg._configured_base_url = self._orig_cfg

    def test_bare_custom_to_openrouter_blocks(self):
        blocked, base = fg.should_block({"base_url": "https://openrouter.ai/api/v1"}, "custom")
        self.assertTrue(blocked)
        self.assertEqual(base, "https://openrouter.ai/api/v1")

    def test_auto_to_openrouter_blocks(self):
        self.assertTrue(fg.should_block({"base_url": "https://openrouter.ai/api/v1/"}, "auto")[0])

    def test_explicit_openrouter_request_passes(self):
        self.assertFalse(fg.should_block({"base_url": "https://openrouter.ai/api/v1"}, "openrouter")[0])

    def test_non_openrouter_endpoint_passes(self):
        self.assertFalse(fg.should_block(
            {"base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic"}, "custom")[0])

    def test_config_mirror_passes(self):
        fg._configured_base_url = lambda: "https://openrouter.ai/api/v1"
        self.assertFalse(fg.should_block({"base_url": "https://openrouter.ai/api/v1"}, "custom")[0])

    def test_empty_base_url_passes(self):
        self.assertFalse(fg.should_block({}, "custom")[0])


class SettingTests(unittest.TestCase):
    def setUp(self):
        fg._CTX = None

    def tearDown(self):
        fg._CTX = None

    def test_official_channel_true(self):
        fg._CTX = _FakeCtx({fg.SETTING_KEY: True})
        self.assertTrue(fg.setting_enabled())

    def test_official_channel_false_wins(self):
        fg._CTX = _FakeCtx({fg.SETTING_KEY: False})
        self.assertFalse(fg.setting_enabled())

    def test_bare_name_fallback_on_nested_entry(self):
        cfg = {"plugins": {"entries": {
            "hermes-provider-advanced": {"settings": {fg.SETTING_KEY: "true"}}}}}
        fake_cfg_mod = types.ModuleType("hermes_cli.config")
        setattr(fake_cfg_mod, "load_config_readonly", lambda: cfg)
        fg._CTX = _FakeCtx({}, plugin_id="custom-plugins/hermes-provider-advanced")
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}), \
                mock.patch("hermes_cli.config.load_config_readonly", lambda: cfg, create=True):
            self.assertTrue(fg.setting_enabled())

    def test_missing_means_false(self):
        fake_cfg_mod = types.ModuleType("hermes_cli.config")
        setattr(fake_cfg_mod, "load_config_readonly", lambda: {"plugins": {"entries": {}}})
        fg._CTX = _FakeCtx({})
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}):
            self.assertFalse(fg.setting_enabled())

    def test_string_truthiness(self):
        for value in ("true", "1", "YES", "on"):
            self.assertTrue(fg._truthy(value), value)
        for value in ("false", "0", "", None, 0):
            self.assertFalse(fg._truthy(value), value)


class WrapTests(unittest.TestCase):
    def setUp(self):
        self.orig_original = lambda *, requested_provider, explicit_api_key=None, explicit_base_url=None: {
            "provider": "custom",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-test",
            "requested_provider": requested_provider,
        }
        self._orig_cfg = fg._configured_base_url
        fg._configured_base_url = lambda: ""

    def tearDown(self):
        fg._configured_base_url = self._orig_cfg
        fg._BLOCKED_ONCE.clear()

    def _auth_error(self):
        mod = types.ModuleType("hermes_cli.auth")

        class AuthError(RuntimeError):
            def __init__(self, message, provider="", code=None, **kw):
                super().__init__(message)
                self.provider = provider
                self.code = code

        setattr(mod, "AuthError", AuthError)
        return mod

    def test_blocked_when_enabled(self):
        wrapped = fg._wrap(self.orig_original)
        auth_mod = self._auth_error()
        with mock.patch.object(fg, "setting_enabled", lambda: True), \
                mock.patch.dict(sys.modules, {"hermes_cli.auth": auth_mod}):
            with self.assertRaises(auth_mod.AuthError) as ctx:
                wrapped(requested_provider="custom")
        self.assertEqual(ctx.exception.code, "openrouter_fallback_blocked")
        self.assertIn("block-custom-openrouter-fallback", str(ctx.exception))

    def test_passthrough_when_disabled(self):
        wrapped = fg._wrap(self.orig_original)
        with mock.patch.object(fg, "setting_enabled", lambda: False):
            rt = wrapped(requested_provider="custom")
        self.assertEqual(rt["base_url"], "https://openrouter.ai/api/v1")

    def test_explicit_openrouter_passthrough(self):
        wrapped = fg._wrap(self.orig_original)
        with mock.patch.object(fg, "setting_enabled", lambda: True):
            rt = wrapped(requested_provider="openrouter")
        self.assertEqual(rt["provider"], "custom")

    def test_named_custom_not_touched(self):
        def named(*, requested_provider, explicit_api_key=None, explicit_base_url=None):
            return {"provider": "custom", "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic"}

        wrapped = fg._wrap(named)
        with mock.patch.object(fg, "setting_enabled", lambda: True):
            rt = wrapped(requested_provider="custom:dashscope-token-plan")
        self.assertIn("token-plan", rt["base_url"])


class InstallTests(unittest.TestCase):
    def test_wrapped_then_already(self):
        fake_mod = types.ModuleType("hermes_cli.runtime_provider")

        def _resolve_openrouter_runtime(*, requested_provider, explicit_api_key=None, explicit_base_url=None):
            return {"provider": "custom", "base_url": "https://openrouter.ai/api/v1"}

        setattr(fake_mod, "_resolve_openrouter_runtime", _resolve_openrouter_runtime)
        with mock.patch.dict(sys.modules, {"hermes_cli.runtime_provider": fake_mod}):
            self.assertEqual(fg.install(), "wrapped")
            self.assertEqual(fg.install(), "already")

    def test_missing_module(self):
        fake_holder = types.ModuleType("hermes_cli.runtime_provider")
        with mock.patch.dict(sys.modules, {"hermes_cli.runtime_provider": fake_holder}):
            self.assertEqual(fg.install(), "missing")

    def test_shape_changed(self):
        fake_mod = types.ModuleType("hermes_cli.runtime_provider")

        def stranger(a, b):
            return {}

        setattr(fake_mod, "_resolve_openrouter_runtime", stranger)
        with mock.patch.dict(sys.modules, {"hermes_cli.runtime_provider": fake_mod}):
            self.assertEqual(fg.install(), "shape-changed")


if __name__ == "__main__":
    unittest.main(verbosity=2)