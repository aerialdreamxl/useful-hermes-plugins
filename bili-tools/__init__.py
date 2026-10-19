"""bili-tools: Bilibili agent tools.

Read path (default, anonymous, zero-dependency): bili_search / bili_video_info
/ bili_comments / bili_danmaku / bili_subtitle / bili_download — all support
implementation="curl" (default) | "bilibili_api" (higher res / AI subs / fuller
danmaku pools; needs the bilibili_api package installed, saved login helps).

Danger path (gated): bili_login / bili_login_check / bili_comment_post /
bili_danmaku_post are registered ONLY when HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1
is set at session start. Write ops act as the user's account and carry ToS
ban risk — the gate is deliberate.

Endpoints verified green 2026-10-19 (v0.1 curl path); dual-impl + danger in v0.2.
"""

from __future__ import annotations

import asyncio
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
import xml.etree.ElementTree as ET
from typing import Any, Optional

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_HEADERS = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
_TIMEOUT = 25

# overridden at register() from ctx/hermes_home
_DL_DIR = r"D:\01-AI\Hermes-Agent\Data\cache\scratch\bili"
_DATA_DIR = r"D:\01-AI\Hermes-Agent\Data\plugins-data\bili-tools"

DANGER_ENV = "HERMES_BILIBILI_ENABLE_DANGER_FEATURE"
CRED_FILE = "credential.json"


# ---------------------------------------------------------------------------
# low-level helpers (curl implementation)
# ---------------------------------------------------------------------------

def _get(url: str) -> Any:
    """GET and parse JSON (B站 API envelope)."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_text(url: str) -> str:
    """GET, transparently decompress (danmaku XML: Content-Encoding gzip|deflate)."""
    import zlib
    req = urllib.request.Request(url, headers=dict(_HEADERS, **{"Accept-Encoding": "gzip, deflate"}))
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif enc == "deflate" and not raw[:1] == b"<":
        try:
            raw = zlib.decompress(raw, -15)  # raw stream, no zlib header
        except zlib.error:
            raw = zlib.decompress(raw)
    elif enc == "gzip" and not raw[:1] == b"<":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def _dl_bytes(url: str) -> bytes:
    """GET raw bytes for stream download (CDN validates Referer — must send it)."""
    req = urllib.request.Request(url, headers=dict(_HEADERS, **{"Referer": "https://www.bilibili.com/video/"}))
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def _clean_title(t: str) -> str:
    return html.unescape(re.sub(r"</?em[^>]*>", "", t or ""))


def _dur(seconds: Any) -> str:
    m, s = divmod(int(seconds or 0), 60)
    h, m = divmod(m, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _err(msg: Any) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _resolve_bvid(args: dict) -> tuple:
    """Accept bvid/aid/cid; return (bvid, aid, title, cid, meta-dict). Raises ValueError."""
    bvid = (args.get("bvid") or "").strip()
    aid = args.get("aid")
    if not bvid and aid:
        d = _get("https://api.bilibili.com/x/web-interface/view?aid=%s" % aid)
        if d.get("code") != 0:
            raise ValueError("view by aid failed: %s" % d.get("message"))
        data = d["data"]
    elif bvid:
        d = _get("https://api.bilibili.com/x/web-interface/view?bvid=%s"
                 % urllib.parse.quote(bvid))
        if d.get("code") != 0:
            raise ValueError("view failed: %s" % d.get("message"))
        data = d["data"]
    else:
        raise ValueError("need 'bvid' or 'aid'")
    cid = args.get("cid") or (data["pages"][0]["cid"] if data.get("pages") else None)
    return data["bvid"], data["aid"], _clean_title(data.get("title", "")), cid, data


# ---------------------------------------------------------------------------
# WBI signing (best-effort; the proven unsigned path stays as fallback)
# ---------------------------------------------------------------------------

_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 26, 13, 50, 29, 7, 5, 49, 21, 47, 19,
              26, 37, 61, 46, 25, 21, 42, 12, 35, 31, 53, 43, 7, 37, 45, 10,
              12, 39, 4, 23, 17, 61, 21, 37, 57, 26, 18, 11, 39, 54, 33, 8,
              46, 11, 40, 56, 30, 28, 19, 3, 35, 47, 37, 33, 41, 12, 55, 31]
_nav_cache = {"key": None, "ts": 0.0}


def _wbi_key() -> str:
    if _nav_cache["key"] and time.time() - _nav_cache["ts"] < 600:
        return _nav_cache["key"]
    d = _get("https://api.bilibili.com/x/web-interface/nav")
    wi = (d.get("data") or {}).get("wbi_img") or {}
    img = os.path.splitext((wi.get("img_url") or "").split("/")[-1])[0]
    sub = os.path.splitext((wi.get("sub_url") or "").split("/")[-1])[0]
    raw = img + sub
    key = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
    if not key:
        raise RuntimeError("wbi key extraction failed")
    _nav_cache.update(key=key, ts=time.time())
    return key


def _wbi_sign(params: dict) -> dict:
    key = _wbi_key()
    p = dict(params, wts=str(int(time.time())))
    q = urllib.parse.urlencode({k: "".join(c for c in str(p[k]) if c not in "!'()*")
                                for k in sorted(p)})
    return dict(p, w_rid=hashlib.md5((q + key).encode()).hexdigest())


# ---------------------------------------------------------------------------
# bilibili_api implementation layer (lazy optional dependency)
# ---------------------------------------------------------------------------

_PY_HINT = ("bilibili_api not installed. Hermes runs on a standalone Python "
            "toolchain whose site-packages holds only pip — install with "
            '"<Data>\\tools\\python-3.14.7+*-win32-x64\\python.exe" -m pip '
            "install bilibili_api (ask the user first), then retry.")


def _import_ba():
    try:
        import bilibili_api
        return bilibili_api
    except ImportError as e:
        raise RuntimeError("%s (ImportError: %s)" % (_PY_HINT, e))


def _load_cred_dict() -> Optional[dict]:
    """Raw saved credential dict (sessdata/bili_jct/buvid3/dedeuserid) or None."""
    path = os.path.join(_DATA_DIR, CRED_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("sessdata") else None
    except (OSError, ValueError):
        return None


def _load_credential():
    """Saved credential as bilibili_api.Credential, or None."""
    data = _load_cred_dict()
    if not data:
        return None
    Credential = _import_ba().Credential
    return Credential(sessdata=data.get("sessdata", ""),
                      bili_jct=data.get("bili_jct", ""),
                      buvid3=data.get("buvid3") or "",
                      dedeuserid=data.get("dedeuserid", ""))


def _run_async(coro_factory):
    """Drive an async bilibili_api call from a sync handler: fresh thread,
    fresh event loop (never blocks or reuses the gateway loop)."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_factory())).result(timeout=120)


def _impl_choice(args: dict) -> str:
    v = (args.get("implementation") or "curl").strip().lower()
    return "bilibili_api" if v in ("bilibili_api", "bilibili-api", "api", "lib") else "curl"


def _ba_error(tool: str, e: Exception) -> str:
    return _err("bilibili_api impl failed (%s): %s: %s"
                % (tool, type(e).__name__, str(e)[:300]))


def _via(fn, tool: str, args: dict, curl_fn) -> str:
    """Dispatch by implementation choice, translating lib errors to guidance."""
    if _impl_choice(args) != "bilibili_api":
        try:
            return curl_fn(args)
        except Exception as e:
            return _err("%s: %s" % (type(e).__name__, str(e)[:200]))
    try:
        return fn(args)
    except Exception as e:
        return _ba_error(tool, e)


# ---------------------------------------------------------------------------
# handlers — curl side
# ---------------------------------------------------------------------------

def _curl_search(args: dict) -> str:
    kw = (args.get("keyword") or "").strip()
    if not kw:
        return _err("need 'keyword'")
    page = int(args.get("page") or 1)
    op = "&order=%s" % args["order"] if (args.get("order") or "").strip() else ""
    base = "https://api.bilibili.com/x/web-interface/wbi/search/type"
    unsigned = "%s?search_type=video&keyword=%s&page=%d%s" % (base, urllib.parse.quote(kw), page, op)
    try:  # try signed first (anti-crawl), fall back to plain (v0.1 proven path)
        sig = _wbi_sign({"search_type": "video", "keyword": kw,
                         "page": str(page), "page_size": "20"})
        signed = base + "?" + urllib.parse.urlencode(sig) + op
        d = _get(signed)
        if d.get("code") != 0:
            d = _get(unsigned)
    except Exception:
        d = _get(unsigned)
    if d.get("code") != 0:
        return _err("search code %s: %s" % (d.get("code"), d.get("message")))
    rows = [{"title": _clean_title(v.get("title")), "bvid": v.get("bvid"),
             "aid": v.get("aid"), "author": v.get("author"), "play": v.get("play"),
             "duration": v.get("duration"), "pubdate_text": v.get("pubdate_text")}
            for v in (d.get("data") or {}).get("result") or []]
    return _ok({"keyword": kw, "page": page, "count": len(rows), "results": rows})


def _curl_video_info(args: dict) -> str:
    bvid, aid, title, cid, data = _resolve_bvid(args)
    stat = data.get("stat") or {}
    pages = [{"page": p.get("page"), "part": p.get("part"), "cid": p.get("cid"),
              "duration": p.get("duration")} for p in (data.get("pages") or [])]
    return _ok({
        "bvid": bvid, "aid": aid, "title": title, "first_cid": cid,
        "author": (data.get("owner") or {}).get("name"),
        "desc": (data.get("desc") or "")[:500],
        "pubdate": data.get("pubdate"), "duration": data.get("duration"),
        "duration_text": _dur(data.get("duration", 0)),
        "stat": {k: stat.get(k) for k in
                 ("view", "danmaku", "reply", "favorite", "coin", "share", "like")},
        "pages": pages,
    })


def _curl_comments(args: dict) -> str:
    bvid, aid, title, _cid, _ = _resolve_bvid(args)
    sort = int(args.get("sort") or 2)  # 2=like, 0=time
    pn = int(args.get("page") or 1)
    ps = min(int(args.get("size") or 20), 30)
    d = _get("https://api.bilibili.com/x/v2/reply?type=1&oid=%s&sort=%d&pn=%d&ps=%d"
             % (aid, sort, pn, ps))
    if d.get("code") != 0:
        return _err("reply code %s: %s" % (d.get("code"), d.get("message")))
    rows = [{"rpid": r.get("rpid"),
             "user": (r.get("member") or {}).get("uname"),
             "text": (r.get("content") or {}).get("message", "")[:300],
             "like": r.get("like"), "reply_count": r.get("rcount", 0)}
            for r in (d.get("data") or {}).get("replies") or []]
    page = (d.get("data") or {}).get("page") or {}
    total = page.get("count") or page.get("total") or 0
    return _ok({"bvid": bvid, "title": title, "total": total,
                "page": pn, "count": len(rows), "comments": rows})


def _curl_danmaku(args: dict) -> str:
    bvid, _aid, title, cid, _ = _resolve_bvid(args)
    if not cid:
        return _err("could not resolve cid")
    txt = _get_text("https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid)
    root = ET.fromstring(txt)
    items = []
    limit = int(args.get("limit") or 50)
    for d in root.iter("d"):
        attrs = (d.get("p") or "").split(",")
        items.append({"time_sec": round(float(attrs[0]), 1) if attrs and attrs[0] else None,
                      "text": (d.text or "").strip()[:120]})
        if len(items) >= limit:
            break
    return _ok({"bvid": bvid, "title": title, "cid": cid,
                "count": len(items), "danmaku": items})


def _parse_sub_body(url: str) -> list:
    body = _get(url)
    if isinstance(body, dict):
        body = body.get("body") or []
    return [{"t": round(float(line.get("from", 0)), 1),
             "text": (line.get("content") or "").strip()} for line in (body or [])[:400]]


def _curl_subtitle(args: dict) -> str:
    bvid, _aid, title, cid, _ = _resolve_bvid(args)
    if not cid:
        return _err("could not resolve cid")
    d = _get("https://api.bilibili.com/x/player/wbi/v2?bvid=%s&cid=%s"
             % (urllib.parse.quote(bvid), cid))
    if d.get("code") != 0:
        return _err("player code %s: %s" % (d.get("code"), d.get("message")))
    subs = ((d.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not subs:
        return _ok({"bvid": bvid, "title": title, "available": False,
                    "note": "no CC subtitles anonymously (AI subs usually need "
                            "login: bili_login then implementation=bilibili_api)"})
    out = []
    for s in subs:
        url = s.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        row = {"lan": s.get("lan"), "name": s.get("lan_doc"), "lines": []}
        try:
            row["lines"] = _parse_sub_body(url)
        except Exception as e:
            row["error"] = str(e)[:120]
        out.append(row)
    return _ok({"bvid": bvid, "title": title, "available": True, "subtitles": out})


_QN_LABEL = {127: "8K", 126: "Dolby", 125: "HDR", 120: "4K", 116: "1080P60",
             112: "1080P+", 80: "1080P", 64: "720P", 32: "480P", 16: "360P"}


def _dest_path(args: dict, bvid: str, tag: str) -> str:
    fn = args.get("filename")
    if fn and os.path.isabs(fn):
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        return fn
    os.makedirs(_DL_DIR, exist_ok=True)
    return os.path.join(_DL_DIR, "%s_%s.flv" % (bvid, tag))


def _curl_download(args: dict) -> str:
    bvid, _aid, title, cid, _ = _resolve_bvid(args)
    if not cid:
        return _err("could not resolve cid")
    qn = int(args.get("quality") or 16)
    d = _get("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=%d&fnval=1"
             % (urllib.parse.quote(bvid), cid, qn))
    if d.get("code") != 0:
        return _err("playurl code %s: %s" % (d.get("code"), d.get("message")))
    dd = d.get("data") or {}
    durls = dd.get("durl") or []
    if not durls:
        return _err("no direct stream (durl empty; login may unlock it — "
                    "bili_login then implementation=bilibili_api; or dash-only)")
    actual_q = dd.get("quality")
    part_no = min(int(args.get("page_no") or 1), len(durls))
    u = durls[part_no - 1]
    dest = _dest_path(args, bvid, "q%s_p%d" % (actual_q, part_no))
    if os.path.exists(dest) and os.path.getsize(dest) > 0 and not args.get("force"):
        return _ok({"bvid": bvid, "title": title, "path": dest,
                    "size_mb": round(os.path.getsize(dest) / 1048576, 1),
                    "quality": actual_q, "cached": True})
    blob = _dl_bytes(u["url"])
    with open(dest, "wb") as f:
        f.write(blob)
    return _ok({"bvid": bvid, "title": title, "path": dest,
                "size_mb": round(len(blob) / 1048576, 1),
                "quality": actual_q,
                "quality_label": _QN_LABEL.get(actual_q, str(actual_q)),
                "accept_quality": dd.get("accept_quality") or [],
                "duration_sec": round(int(u.get("length", 0)) / 1000)})


# ---------------------------------------------------------------------------
# handlers — bilibili_api side
# ---------------------------------------------------------------------------

def _ba_cred():
    try:
        return _load_credential()
    except Exception:
        return None


def _api_search(args: dict) -> str:
    ba = _import_ba()
    search = ba.search
    kw = (args.get("keyword") or "").strip()
    if not kw:
        return _err("need 'keyword'")
    order_map = {"": search.OrderVideo.TOTALRANK,
                 "totalrank": search.OrderVideo.TOTALRANK,
                 "click": search.OrderVideo.CLICK,
                 "pubdate": search.OrderVideo.PUBDATE,
                 "dm": search.OrderVideo.DM,
                 "stow": search.OrderVideo.STOW}
    enum = order_map.get((args.get("order") or "").strip(), search.OrderVideo.TOTALRANK)
    cred = _ba_cred()
    if cred:
        raw = _run_async(lambda: search.search_by_type(
            keyword=kw, search_type=search.SearchObjectType.VIDEO,
            page=int(args.get("page") or 1), order_type=enum, credential=cred))
    else:
        raw = _run_async(lambda: search.search_by_type(
            keyword=kw, search_type=search.SearchObjectType.VIDEO,
            page=int(args.get("page") or 1), order_type=enum))
    rows = [{"title": _clean_title(v.get("title")), "bvid": v.get("bvid"),
             "aid": v.get("aid"), "author": v.get("author"), "play": v.get("play"),
             "duration": v.get("duration"),
             "desc": (v.get("description") or v.get("desc") or "")[:120]}
            for v in raw.get("result") or []][:20]
    return _ok({"keyword": kw, "count": len(rows), "results": rows})


def _api_video_info(args: dict) -> str:
    ba = _import_ba()
    cred = _ba_cred()
    bvid = (args.get("bvid") or "").strip()
    aid = args.get("aid")

    async def go():
        v = (ba.video.Video(bvid=bvid, credential=cred) if bvid
             else ba.video.Video(aid=int(aid), credential=cred))
        return await v.get_info()
    info = _run_async(go)
    st = info.get("stat") or {}
    return _ok({
        "bvid": info.get("bvid"), "aid": info.get("aid"),
        "title": _clean_title(info.get("title") or ""),
        "first_cid": (info.get("pages") or [{}])[0].get("cid") or info.get("cid"),
        "author": (info.get("owner") or {}).get("name"),
        "desc": (info.get("desc") or "")[:500],
        "pubdate": info.get("pubdate"), "duration": info.get("duration"),
        "duration_text": _dur(info.get("duration", 0)),
        "stat": {k: st.get(k) for k in
                 ("view", "danmaku", "reply", "favorite", "coin", "share", "like")},
        "pages": [{"page": p.get("page"), "part": p.get("part"),
                   "cid": p.get("cid")} for p in (info.get("pages") or [])],
    })


def _api_comments(args: dict) -> str:
    ba = _import_ba()
    comment = ba.comment
    cred = _ba_cred()
    bvid = (args.get("bvid") or "").strip()

    async def go():
        aid = args.get("aid")
        if not aid:
            v = ba.video.Video(bvid=bvid, credential=cred)
            aid = (await v.get_info())["aid"]
        return aid, await comment.get_comments(
            oid=int(aid), type_=comment.CommentResourceType.VIDEO,
            page_index=int(args.get("page") or 1),
            order=comment.OrderType.LIKE if int(args.get("sort") or 2) == 2
            else comment.OrderType.TIME,
            credential=cred)
    aid, resp = _run_async(go)
    data = resp.get("data") or {}
    rows = [{"rpid": r.get("rpid"), "user": (r.get("member") or {}).get("uname"),
             "text": (r.get("content") or {}).get("message", "")[:300],
             "like": r.get("like"), "reply_count": r.get("rcount", 0)}
            for r in data.get("replies") or []][:min(int(args.get("size") or 20), 30)]
    return _ok({"bvid": bvid, "aid": aid,
                "total": (data.get("page") or {}).get("total") or 0,
                "count": len(rows), "comments": rows})


def _api_danmaku(args: dict) -> str:
    ba = _import_ba()
    cred = _ba_cred()
    bvid = (args.get("bvid") or "").strip()
    limit = int(args.get("limit") or 50)

    async def go():
        v = ba.video.Video(bvid=bvid, credential=cred)
        return await v.get_danmakus(page_index=0)
    dm = _run_async(go)
    items = [{"time_sec": getattr(d, "dm_time", None),
              "text": (getattr(d, "text", "") or "").strip()[:120]} for d in dm[:limit]]
    return _ok({"bvid": bvid, "count": len(items), "danmaku": items})


def _api_subtitle(args: dict) -> str:
    ba = _import_ba()
    cred = _ba_cred()
    bvid = (args.get("bvid") or "").strip()

    async def go():
        v = ba.video.Video(bvid=bvid, credential=cred)
        info = await v.get_info()
        cid = info.get("cid") or (info.get("pages") or [{}])[0].get("cid", 0)
        return info, await v.get_subtitle(cid=cid)
    info, slist = _run_async(go)
    subs = slist.get("subtitles") or []
    if not subs:
        return _ok({"bvid": bvid, "title": info.get("title"), "available": False})
    out = []
    for s in subs:
        url = s.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        row = {"lan": s.get("lan"), "name": s.get("lan_doc"), "lines": []}
        try:
            row["lines"] = _parse_sub_body(url)
        except Exception as e:
            row["error"] = str(e)[:120]
        out.append(row)
    return _ok({"bvid": bvid, "title": info.get("title"),
                "available": True, "subtitles": out})


_QN_ATTR = {16: "Q_360", 32: "Q_480", 64: "Q_720", 80: "Q_1080",
            112: "Q_1080P_PLUS", 116: "Q_1080P60", 120: "Q_4K"}


def _api_download(args: dict) -> str:
    ba = _import_ba()
    cred = _ba_cred()
    if not cred:
        return _err("bilibili_api high-quality download wants a saved login — "
                    "run bili_login first (danger env), or use implementation=curl.")
    bvid = (args.get("bvid") or "").strip()
    qn = int(args.get("quality") or 80)

    async def go():
        v = ba.video.Video(bvid=bvid, credential=cred)
        info = await v.get_info()
        quality = getattr(ba.video.VideoQuality, _QN_ATTR.get(qn, "Q_1080"))
        return info, await v.get_download_url(page_index=0, quality=quality)
    info, dl = _run_async(go)
    dl = dl.get("data") if isinstance(dl.get("data"), dict) else dl
    blob = None
    got_q = qn
    if dl.get("durl"):  # flv single file
        got_q = dl["durl"][0].get("quality") or qn
        blob = _dl_bytes(dl["durl"][0]["url"])
    elif (dl.get("dash") or {}).get("video"):  # dash — pick best <= qn
        vids = [s for s in dl["dash"]["video"] if s.get("id", 0) <= qn]
        chosen = max(vids, key=lambda s: s.get("id", 0)) if vids else \
            max(dl["dash"]["video"], key=lambda s: s.get("id", 0))
        got_q = chosen.get("id")
        blob = _dl_bytes(chosen.get("base_url") or (chosen.get("backup_url") or [""])[0])
    else:
        return _err("no playable stream from get_download_url (keys: %s)" % list(dl)[:6])
    dest = _dest_path(args, bvid, "api_q%s" % got_q)
    if os.path.exists(dest) and os.path.getsize(dest) > 0 and not args.get("force"):
        return _ok({"bvid": bvid, "cached": True, "path": dest,
                    "size_mb": round(os.path.getsize(dest) / 1048576, 1)})
    with open(dest, "wb") as f:
        f.write(blob)
    return _ok({"bvid": bvid, "title": info.get("title"), "path": dest,
                "size_mb": round(len(blob) / 1048576, 1), "quality": got_q,
                "quality_label": _QN_LABEL.get(got_q, str(got_q)),
                "dash_note": "dash 视频轨不含音轨时，需 ffmpeg 合并；需要时告诉我"})


# ---------------------------------------------------------------------------
# unified tool handlers
# ---------------------------------------------------------------------------

def _h_search(args: dict, **kw) -> str:
    return _via(_api_search, "bili_search", args, _curl_search)

def _h_video_info(args: dict, **kw) -> str:
    return _via(_api_video_info, "bili_video_info", args, _curl_video_info)

def _h_comments(args: dict, **kw) -> str:
    return _via(_api_comments, "bili_comments", args, _curl_comments)

def _h_danmaku(args: dict, **kw) -> str:
    return _via(_api_danmaku, "bili_danmaku", args, _curl_danmaku)

def _h_subtitle(args: dict, **kw) -> str:
    return _via(_api_subtitle, "bili_subtitle", args, _curl_subtitle)

def _h_download(args: dict, **kw) -> str:
    return _via(_api_download, "bili_download", args, _curl_download)


# ---------------------------------------------------------------------------
# danger tools (registered only when HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1)
# ---------------------------------------------------------------------------

_login_session_box: dict = {}


def _danger_enabled() -> bool:
    return os.environ.get(DANGER_ENV, "").strip() == "1"


def _danger_guard(tool: str) -> Optional[str]:
    if _danger_enabled():
        return None
    return _err("%s is a WRITE/LOGIN tool, disabled by default. Set %s=1 before "
                "starting the session to enable." % (tool, DANGER_ENV))


def _h_login(args: dict, **kw) -> str:
    g = _danger_guard("bili_login")
    if g:
        return g
    try:
        _import_ba()
        from bilibili_api.login_v2 import QrCodeLogin
        if _load_cred_dict():
            return _ok({"status": "already_logged_in",
                        "credential_file": os.path.join(_DATA_DIR, CRED_FILE)})
        sess = QrCodeLogin()
        _run_async(sess.generate_qrcode)
        pic = sess.get_qrcode_picture()
        os.makedirs(_DATA_DIR, exist_ok=True)
        qr_path = os.path.join(_DATA_DIR, "qrcode_login.png")
        with open(qr_path, "wb") as f:
            f.write(pic.content)
        _login_session_box["session"] = sess
        return _ok({"status": "qrcode_ready",
                    "qrcode_path": qr_path,
                    "qrcode_url": getattr(sess, "_QrCodeLogin__qr_link", ""),
                    "ttl_note": "二维码约180秒有效；用户扫码后调用 bili_login_check 轮询",
                    "media_hint": "attach qrcode_path via MEDIA so the user can scan it"})
    except Exception as e:
        return _ba_error("bili_login", e)


def _h_login_check(args: dict, **kw) -> str:
    g = _danger_guard("bili_login_check")
    if g:
        return g
    try:
        from bilibili_api.login_v2 import QrCodeLoginEvents
        sess = _login_session_box.get("session")
        if not sess:
            if _load_cred_dict():
                return _ok({"status": "already_logged_in"})
            return _err("no active login session; call bili_login first")
        state = _run_async(sess.check_state)
        if state == QrCodeLoginEvents.DONE:
            cred = sess.get_credential()
            data = {"sessdata": cred.sessdata, "bili_jct": cred.bili_jct,
                    "buvid3": getattr(cred, "buvid3", "") or "",
                    "dedeuserid": cred.dedeuserid}
            os.makedirs(_DATA_DIR, exist_ok=True)
            tmp = os.path.join(_DATA_DIR, CRED_FILE + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:  # atomic: a torn
                json.dump(data, f)                      # write must never
            os.replace(tmp, os.path.join(_DATA_DIR, CRED_FILE))  # kill a good cred
            try:
                os.chmod(os.path.join(_DATA_DIR, CRED_FILE), 0o600)
            except OSError:
                pass
            _login_session_box.pop("session", None)
            return _ok({"status": "done", "dedeuserid": data["dedeuserid"],
                        "credential_saved": os.path.join(_DATA_DIR, CRED_FILE)})
        m = {QrCodeLoginEvents.SCAN: ("scanning", "已扫码待确认，3秒后再查"),
             QrCodeLoginEvents.CONF: ("confirming", "已确认处理中，2秒后再查"),
             QrCodeLoginEvents.TIMEOUT: ("timeout", "二维码过期，重新调用 bili_login")}
        if state == QrCodeLoginEvents.TIMEOUT:
            _login_session_box.pop("session", None)
        st, msg = m.get(state, ("unknown", str(state)))
        return _ok({"status": st, "message": msg})
    except Exception as e:
        return _ba_error("bili_login_check", e)


def _h_comment_post(args: dict, **kw) -> str:
    g = _danger_guard("bili_comment_post")
    if g:
        return g
    try:
        ba = _import_ba()
        comment = ba.comment
        cred = _ba_cred()
        if not cred:
            return _err("no saved login — run bili_login (scan QR) first")
        text = (args.get("text") or "").strip()
        if not text:
            return _err("need 'text'")
        bvid = (args.get("bvid") or "").strip()
        rpid = int(args.get("reply_to_rpid") or 0)

        async def go():
            v = ba.video.Video(bvid=bvid, credential=cred)
            aid = (await v.get_info())["aid"]
            if rpid == 0:
                return aid, await comment.send_comment(
                    text=text, oid=aid, type_=comment.CommentResourceType.VIDEO,
                    credential=cred)
            root = int(args.get("root_rpid") or 0) or rpid
            return aid, await comment.send_comment(
                text=text, oid=aid, type_=comment.CommentResourceType.VIDEO,
                root=root, parent=rpid, credential=cred)
        aid, res = _run_async(go)
        if res.get("code") != 0:
            return _err("send code %s: %s" % (res.get("code"), res.get("message")))
        return _ok({"bvid": bvid, "aid": aid,
                    "rpid": (res.get("data") or {}).get("rpid"),
                    "message": res.get("message") or "ok"})
    except Exception as e:
        return _ba_error("bili_comment_post", e)


def _h_danmaku_post(args: dict, **kw) -> str:
    """EXPERIMENTAL: raw web danmaku submit with saved credential cookies
    (no stable bilibili_api helper — deliberate curl to avoid guessing lib names)."""
    g = _danger_guard("bili_danmaku_post")
    if g:
        return g
    try:
        data = _load_cred_dict()
        if not data:
            return _err("no saved login — run bili_login (scan QR) first")
        text = (args.get("text") or "").strip()
        if not text:
            return _err("need 'text'")
        bvid, _aid, _title, cid, _ = _resolve_bvid(args)
        if not cid:
            return _err("could not resolve cid")
        offset = float(args.get("time_offset_sec") or 0)
        seg = int(offset // 600) + 1
        form = urllib.parse.urlencode({
            "oid": cid, "msg": text,
            "c": "%d,1,25,1,16777215,%d,0" % (int(offset * 1000), int(time.time())),
            "csrf": data.get("bili_jct", "")}).encode()
        req = urllib.request.Request("https://api.bilibili.com/x/v2/dm/web/submit",
                                     data=form, headers=dict(_HEADERS, **{
                                         "Cookie": "SESSDATA=%s; bili_jct=%s; buvid3=%s"
                                         % (data.get("sessdata", ""), data.get("bili_jct", ""),
                                            data.get("buvid3", "")),
                                         "Content-Type": "application/x-www-form-urlencoded",
                                         "Origin": "https://www.bilibili.com",
                                         "Referer": "https://www.bilibili.com/video/%s" % bvid}))
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
        if res.get("code") != 0:
            return _err("danmaku code %s: %s" % (res.get("code"), res.get("message")))
        return _ok({"bvid": bvid, "cid": cid, "segment": seg,
                    "dm_id": (res.get("data") or {}).get("id"),
                    "message": res.get("message") or "ok"})
    except Exception as e:
        return _err("bili_danmaku_post: %s: %s" % (type(e).__name__, str(e)[:200]))


# ---------------------------------------------------------------------------
# schemas & registration
# ---------------------------------------------------------------------------

_IMPL_PROP = {
    "type": "string",
    "enum": ["curl", "bilibili_api"],
    "description": ("curl = anonymous stdlib path (default, zero deps). "
                    "bilibili_api = Python lib path: needs `pip install bilibili_api` "
                    "in the Hermes toolchain; with saved login unlocks higher download "
                    "quality, AI subtitles, fuller danmaku. Choose curl unless lib "
                    "capability is needed."),
}

_BVID_ARG = {
    "bvid": {"type": "string", "description": "BV id, e.g. BV1T5nL6rEPb (or use aid)"},
    "aid": {"type": "integer", "description": "AV-number id (alternative to bvid)"},
    "cid": {"type": "integer", "description": "page cid (default: first page)"},
}

TOOL_DEFS = [
    ("bili_search", "bili-tools", _h_search, {
        "keyword": {"type": "string", "description": "search words"},
        "page": {"type": "integer", "description": "result page, default 1"},
        "order": {"type": "string", "description": "sort: '', totalrank, click, pubdate, dm, stow"},
        "implementation": _IMPL_PROP,
    }, ["keyword"],
    "Search Bilibili videos by keyword (anonymous public API). Returns title, bvid, aid, author, play, duration."),
    ("bili_video_info", "bili-tools", _h_video_info, {
        **_BVID_ARG, "implementation": _IMPL_PROP,
    }, [],
    "Get Bilibili video metadata: title, author, desc, stat (view/danmaku/reply/like...), pages with cid per part. Prerequisite for danmaku/subtitle/download on multi-part videos."),
    ("bili_comments", "bili-tools", _h_comments, {
        **_BVID_ARG,
        "sort": {"type": "integer", "description": "2=by like (default), 0=by time"},
        "page": {"type": "integer", "description": "comment page, default 1"},
        "size": {"type": "integer", "description": "per page, max 30"},
        "implementation": _IMPL_PROP,
    }, [],
    "Read top-level comments of a Bilibili video, with rpid (for replies) and like counts."),
    ("bili_danmaku", "bili-tools", _h_danmaku, {
        **_BVID_ARG,
        "limit": {"type": "integer", "description": "max items, default 50"},
        "implementation": _IMPL_PROP,
    }, [],
    "Read danmaku (bullet comments) of a video part: time_sec + text per item."),
    ("bili_subtitle", "bili-tools", _h_subtitle, {
        **_BVID_ARG, "implementation": _IMPL_PROP,
    }, [],
    "Read CC/AI subtitles (timestamped lines). Many videos have none anonymously — empty is normal. AI subs usually need login + implementation=bilibili_api."),
    ("bili_download", "bili-tools", _h_download, {
        **_BVID_ARG,
        "quality": {"type": "integer", "description": "qn: 16=360p 32=480p 64=720p 80=1080p 112=1080P+ 116=1080P60 120=4K; default 16 curl / 80 lib"},
        "page_no": {"type": "integer", "description": "which stream part, default 1"},
        "force": {"type": "boolean", "description": "re-download even if cached"},
        "filename": {"type": "string", "description": "absolute path override"},
        "implementation": _IMPL_PROP,
    }, [],
    "Download one video part to a local file, returns path (scratch bili dir). Anonymous quality capped per-video (see accept_quality); bilibili_api + saved login reaches 1080P+."),
    ("bili_login", "bili-tools-danger", _h_login, {}, [],
    "DANGEROUS: start QR login for bilibili_api (write + high-res access). Returns qrcode_path (png, show it to the user as an attachment; they scan in the Bilibili App) — then poll bili_login_check. Only registered when HERMES_BILIBILI_ENABLE_DANGER_FEATURE=1."),
    ("bili_login_check", "bili-tools-danger", _h_login_check, {}, [],
    "DANGEROUS: poll QR login state (scanning/confirming/done/timeout). On done the credential saves to plugins-data/bili-tools/credential.json."),
    ("bili_comment_post", "bili-tools-danger", _h_comment_post, {
        "bvid": {"type": "string"},
        "text": {"type": "string"},
        "reply_to_rpid": {"type": "integer", "description": "0=new top-level comment"},
        "root_rpid": {"type": "integer", "description": "thread root for nested reply"},
    }, ["bvid", "text"],
    "DANGEROUS WRITE: post a comment/reply AS THE USER with their own account (ToS ban risk). Requires bili_login first. ONLY on explicit per-message user instruction; show the exact text and get confirmation before sending."),
    ("bili_danmaku_post", "bili-tools-danger", _h_danmaku_post, {
        "bvid": {"type": "string"},
        "text": {"type": "string"},
        "time_offset_sec": {"type": "number", "description": "flight time, seconds; default 0"},
    }, ["bvid", "text"],
    "DANGEROUS WRITE (experimental raw-API path): send a danmaku AS THE USER at a time offset. Highest ban risk. Requires bili_login. Explicit user instruction only."),
]

_HANDLERS = {name: h for name, _ts, h, _p, _r, _d in TOOL_DEFS}


def register(ctx) -> None:
    global _DL_DIR, _DATA_DIR
    try:
        home = getattr(ctx, "hermes_home", None) or os.environ.get("HERMES_HOME")
        if home:
            _DL_DIR = os.path.join(str(home), "cache", "scratch", "bili")
            _DATA_DIR = os.path.join(str(home), "plugins-data", "bili-tools")
    except Exception:
        pass

    danger = _danger_enabled()
    for name, toolset, handler, props, required, desc in TOOL_DEFS:
        if toolset == "bili-tools-danger" and not danger:
            continue  # invisible until the env gate is on
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": required}}
        ctx.register_tool(
            name=name, toolset=toolset, schema=schema, handler=handler,
            check_fn=None, description=desc, emoji="\U0001F3AF")

    section = (
        "## bili-tools (Bilibili access)\n"
        "Six read tools: bili_search, bili_video_info, bili_comments, bili_danmaku,\n"
        "bili_subtitle, bili_download. Every tool takes implementation=\"curl\" (default:\n"
        "anonymous, zero deps) or implementation=\"bilibili_api\" (Python lib: needs\n"
        "bilibili_api installed; with saved login from bili_login: higher download\n"
        "quality, AI subtitles, fuller danmaku). Default to curl; use the lib only when\n"
        "its capabilities are needed. Typical flow: search -> video_info (cid) ->\n"
        "subtitle/danmaku/comments; download for vision/Omni work.\n")
    if danger:
        section += (
            "DANGER mode is ON: bili_login / bili_login_check / bili_comment_post /\n"
            "bili_danmaku_post are live. They act AS THE USER with their own account —\n"
            "ban risk per platform ToS. NEVER invoke a write tool without an explicit\n"
            "per-message user instruction; always show the exact text to be posted and\n"
            "get a yes first. For login: attach qrcode_path via MEDIA, poll\n"
            "bili_login_check every 3s after the user scans.")
    else:
        section += "No write/login tools are registered (env gate off)."
    ctx.register_system_prompt_section("bili-tools", section, max_chars=4000)
