"""Bilibili credential store (SESSDATA / bili_jct / buvid3 / DedeUserID)."""

from __future__ import annotations

import json
import os
from typing import Optional

from . import config

CRED_FILE = "credential.json"


def cred_path() -> str:
    return os.path.join(config.plugin_data_dir(), CRED_FILE)


def load_cred() -> Optional[dict]:
    path = cred_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("sessdata"):
        return None
    return data


def save_cred(data: dict) -> str:
    path = cred_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def cookie_header(cred: Optional[dict] = None) -> str:
    c = cred or load_cred()
    if not c:
        return ""
    parts = []
    for name, value in (("SESSDATA", c.get("sessdata")),
                        ("bili_jct", c.get("bili_jct")),
                        ("buvid3", c.get("buvid3")),
                        ("DedeUserID", c.get("dedeuserid")),
                        ("DedeUserID__ckMd5", c.get("dedeuserid_ckmd5"))):
        if value:
            parts.append("%s=%s" % (name, value))
    return "; ".join(parts)


def csrf(cred: Optional[dict] = None) -> str:
    c = cred or load_cred() or {}
    return c.get("bili_jct", "") or ""


def headers(cred: Optional[dict] = None) -> dict:
    ck = cookie_header(cred)
    return {"Cookie": ck} if ck else {}


def require_cred() -> tuple:
    """Return (cred, error_string)."""
    cred = load_cred()
    if not cred:
        return None, "未登录：先调用 bili_login 扫码登录（bilibili_unsafe 组）"
    return cred, ""