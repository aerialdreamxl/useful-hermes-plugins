"""Bilibili credential store (SESSDATA / bili_jct / buvid3 / DedeUserID)."""

from __future__ import annotations

import json
import os
from typing import Optional

from . import config

CRED_FILE = "credential.json"

_COOKIE_KEYS = ("sessdata", "bili_jct", "buvid3", "dedeuserid", "dedeuserid_ckmd5")
# 会打穿 HTTP 协议层的字符：换行/回车/分号（Cookie 分隔符）/空白/控制字符
_BAD_CHARS = ("\r", "\n", ";", " ", "\t")
_LAST = {"warns": []}


def cred_path() -> str:
    return os.path.join(config.plugin_data_dir(), CRED_FILE)


def _dirty(value: str) -> bool:
    """凭证值是否脏：含换行/分号/空白/控制字符（会让 urllib 在协议层抛错，像网络坏了）。"""
    for ch in value:
        if ch in _BAD_CHARS or ord(ch) < 32 or ord(ch) == 127:
            return True
    return False


def _clean(cred: dict) -> tuple:
    """丢掉脏 cookie 值；返回 (干净 dict, 警告列表)。"""
    clean = dict(cred)
    warns = []
    for name in _COOKIE_KEYS:
        raw = clean.get(name)
        if raw is None or str(raw) == "":
            continue
        value = str(raw)
        if _dirty(value):
            clean.pop(name, None)
            warns.append("credential.json 里 %s 字段非法（含换行/分号/空白/控制字符），已丢弃该 cookie" % name)
        else:
            clean[name] = value
    return clean, warns


def cred_warnings() -> list:
    """最近一次 load_cred 丢了哪些脏字段（空列表＝凭证干净）。"""
    return list(_LAST["warns"])


def load_cred() -> Optional[dict]:
    path = cred_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    clean, warns = _clean(data)
    _LAST["warns"] = warns
    if not clean.get("sessdata"):
        return None
    return clean


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
    """拼 Cookie 头：脏值一律跳过，绝不把换行/分号塞进请求头（保持返回 str）。"""
    c = cred or load_cred()
    if not c:
        return ""
    clean, _warns = _clean(c)
    parts = []
    for name, value in (("SESSDATA", clean.get("sessdata")),
                        ("bili_jct", clean.get("bili_jct")),
                        ("buvid3", clean.get("buvid3")),
                        ("DedeUserID", clean.get("dedeuserid")),
                        ("DedeUserID__ckMd5", clean.get("dedeuserid_ckmd5"))):
        if value:
            parts.append("%s=%s" % (name, value))
    return "; ".join(parts)


def csrf(cred: Optional[dict] = None) -> str:
    c = cred or load_cred() or {}
    value = str(c.get("bili_jct", "") or "")
    return "" if _dirty(value) else value


def headers(cred: Optional[dict] = None) -> dict:
    ck = cookie_header(cred)
    return {"Cookie": ck} if ck else {}


def require_cred() -> tuple:
    """Return (cred, error_string)."""
    cred = load_cred()
    if not cred:
        warns = cred_warnings()
        if warns:
            return None, ("凭证文件异常：%s；请重新 bili_login 或手工修 credential.json"
                          % "；".join(warns))
        return None, "未登录：先调用 bili_login 扫码登录（bilibili_unsafe 组）"
    return cred, ""