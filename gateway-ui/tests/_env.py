# -*- coding: utf-8 -*-
"""tests/_env.py — 测试环境解析（公开版）：一律从 HERMES_HOME 环境变量取根，
未设置时响亮拒绝（绝不猜路径）。scratch 总线目录 = <home>/cache/scratch/...。
"""
import os
import sys
from pathlib import Path


def hermes_home() -> str:
    v = os.environ.get("HERMES_HOME", "").strip()
    if not v:
        print("FAIL: 请先设置 HERMES_HOME 环境变量（指向 Hermes 数据家目录）再跑测试。",
              flush=True)
        sys.exit(2)
    return v


HOME = hermes_home()


def scratch(name: str) -> Path:
    p = Path(HOME) / "cache" / "scratch" / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def repo() -> Path:
    """Hermes 安装树（官方源码只读取证用）：<home>/hermes-agent。"""
    return Path(HOME) / "hermes-agent"
