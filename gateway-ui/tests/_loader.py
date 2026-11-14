# -*- coding: utf-8 -*-
"""宿主测试共用加载器：按官方加载器同款方式以包形式加载 gateway-ui
（spec_from_file_location + submodule_search_locations，plugins_loader.py:834）。"""
import importlib.util
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]


def load_plugin():
    if "gateway_ui" in sys.modules:
        return sys.modules["gateway_ui"]
    spec = importlib.util.spec_from_file_location(
        "gateway_ui", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "gateway_ui"
    mod.__path__ = [str(PKG)]
    sys.modules["gateway_ui"] = mod
    spec.loader.exec_module(mod)
    return mod