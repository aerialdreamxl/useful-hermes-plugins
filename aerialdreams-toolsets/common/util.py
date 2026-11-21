"""HTTP / JSON / WBI helpers shared by all groups (stdlib only)."""

from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
TIMEOUT = 25


def safe_output_path(path: str, roots, label: str = "输出路径",
                     allow_existing: bool = False) -> tuple:
    """Constrain a write target to ``roots`` (realpath); refuse existing
    files unless ``allow_existing`` (scratch cache rewrite)."""
    if not path:
        return "", "%s 不能为空" % label
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    allowed = False
    for root in roots if isinstance(roots, (list, tuple, set)) else [roots]:
        base = os.path.realpath(os.path.abspath(str(root)))
        try:
            if os.path.commonpath([candidate, base]) == base and candidate != base:
                allowed = True
                break
        except ValueError:
            pass
    if not allowed:
        return "", "%s 必须位于 scratch 目录内" % label
    if os.path.lexists(candidate) and not allow_existing:
        return "", "%s 已存在，拒绝覆盖" % label
    return candidate, ""


def safe_input_path(path: str, roots, label: str = "source") -> tuple:
    """Return an existing regular file constrained to one of ``roots``."""
    if not path:
        return "", "%s 不能为空" % label
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    allowed = False
    for root in roots if isinstance(roots, (list, tuple, set)) else [roots]:
        base = os.path.realpath(os.path.abspath(str(root)))
        try:
            if os.path.commonpath([candidate, base]) == base and candidate != base:
                allowed = True
                break
        except ValueError:
            pass
    if not allowed:
        return "", "%s 本地路径必须位于 scratch 或 plugin-data 目录内" % label
    if not os.path.isfile(candidate):
        return "", "%s 本地路径不是可读的普通文件" % label
    return candidate, ""


def ok(data: dict) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def err(msg: Any) -> str:
    return json.dumps({"ok": False, "error": str(msg)[:600]}, ensure_ascii=False)


def _merge(headers: Optional[dict]) -> dict:
    return dict(HEADERS, **(headers or {}))


_BILI_DOWNLOAD_HOSTS = ("bilibili.com", "bilivideo.com", "bilivideo.cn", "hdslb.com")


def validate_bili_download_url(url: str) -> str:
    """Return an error for a non-Bilibili HTTPS download URL."""
    try:
        parsed = urllib.parse.urlparse(str(url))
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return "B站下载 URL 无效"
    if parsed.scheme.lower() != "https" or not host:
        return "B站下载 URL 必须使用 HTTPS"
    if port not in (None, 443):
        return "B站下载 URL 端口不受允许"
    if not any(host == suffix or host.endswith("." + suffix)
               for suffix in _BILI_DOWNLOAD_HOSTS):
        return "B站下载 URL host 不在允许名单"
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url: str, headers: Optional[dict] = None, timeout: int = TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers=_merge(headers))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_text(url: str, headers: Optional[dict] = None, timeout: int = TIMEOUT) -> str:
    import zlib
    hdrs = _merge(headers)
    hdrs.setdefault("Accept-Encoding", "gzip, deflate")
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif enc == "deflate" and not raw[:1] == b"<":
        try:
            raw = zlib.decompress(raw, -15)
        except zlib.error:
            raw = zlib.decompress(raw)
    elif enc == "gzip" and not raw[:1] == b"<":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def get_bytes(url: str, headers: Optional[dict] = None, timeout: int = 120) -> bytes:
    url_error = validate_bili_download_url(url)
    if url_error:
        raise ValueError(url_error)
    hdrs = _merge(headers)
    hdrs.setdefault("Referer", "https://www.bilibili.com/video/")
    req = urllib.request.Request(url, headers=hdrs)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            final_error = validate_bili_download_url(r.geturl())
            if final_error:
                raise ValueError("B站下载响应 URL 不在允许名单")
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            raise ValueError("B站下载拒绝跟随重定向")
        raise


def post_form(url: str, data: dict, headers: Optional[dict] = None,
              timeout: int = TIMEOUT) -> Any:
    body = urllib.parse.urlencode(data).encode()
    hdrs = _merge(headers)
    hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def post_json(url: str, payload: dict, headers: Optional[dict] = None,
              timeout: int = TIMEOUT) -> Any:
    body = json.dumps(payload).encode()
    hdrs = _merge(headers)
    hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def clean_title(t: str) -> str:
    return html.unescape(re.sub(r"</?em[^>]*>", "", t or ""))


def dur_text(seconds: Any) -> str:
    m, s = divmod(int(seconds or 0), 60)
    h, m = divmod(m, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


# ---------------------------------------------------------------------------
# WBI signing (needed by space/search/player endpoints)
# ---------------------------------------------------------------------------

MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 26, 13, 50, 29, 7, 5, 49, 21, 47, 19,
             26, 37, 61, 46, 25, 21, 42, 12, 35, 31, 53, 43, 7, 37, 45, 10,
             12, 39, 4, 23, 17, 61, 21, 37, 57, 26, 18, 11, 39, 54, 33, 8,
             46, 11, 40, 56, 30, 28, 19, 3, 35, 47, 37, 33, 41, 12, 55, 31]
_nav_cache = {"key": None, "ts": 0.0}


def wbi_key(headers: Optional[dict] = None) -> str:
    if _nav_cache["key"] and time.time() - _nav_cache["ts"] < 600:
        return _nav_cache["key"]
    d = get_json("https://api.bilibili.com/x/web-interface/nav", headers=headers)
    wi = (d.get("data") or {}).get("wbi_img") or {}
    img = os.path.splitext((wi.get("img_url") or "").split("/")[-1])[0]
    sub = os.path.splitext((wi.get("sub_url") or "").split("/")[-1])[0]
    raw = img + sub
    key = "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]
    if not key:
        raise RuntimeError("wbi key extraction failed")
    _nav_cache.update(key=key, ts=time.time())
    return key


def wbi_sign(params: dict, headers: Optional[dict] = None) -> dict:
    key = wbi_key(headers)
    p = dict(params, wts=str(int(time.time())))
    q = urllib.parse.urlencode({k: "".join(c for c in str(p[k]) if c not in "!'()*")
                                for k in sorted(p)})
    return dict(p, w_rid=hashlib.md5((q + key).encode()).hexdigest())


def resolve_bvid(args: dict, headers: Optional[dict] = None) -> tuple:
    """Accept bvid/aid/cid; return (bvid, aid, title, cid, view-data)."""
    bvid = (args.get("bvid") or "").strip()
    aid = args.get("aid")
    if not bvid and aid:
        d = get_json("https://api.bilibili.com/x/web-interface/view?aid=%s" % aid, headers=headers)
        if d.get("code") != 0:
            raise ValueError("view by aid failed: %s" % d.get("message"))
        data = d["data"]
    elif bvid:
        d = get_json("https://api.bilibili.com/x/web-interface/view?bvid=%s"
                     % urllib.parse.quote(bvid), headers=headers)
        if d.get("code") != 0:
            raise ValueError("view failed: %s" % d.get("message"))
        data = d["data"]
    else:
        raise ValueError("need 'bvid' or 'aid'")
    cid = args.get("cid") or (data["pages"][0]["cid"] if data.get("pages") else None)
    return data["bvid"], data["aid"], clean_title(data.get("title", "")), cid, data


def download_to(url: str, dest: str, headers: Optional[dict] = None) -> int:
    blob = get_bytes(url, headers=headers)
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(blob)
    return len(blob)