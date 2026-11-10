"""``bilibili`` group — anonymous, login-free Bilibili reads.

Every tool here must work without cookies (口径 B): search / metadata / comments
/ danmaku / CC subtitle / anonymous-capped download / user & space reads.
Anything that needs a login cookie lives in ``bilibili_unsafe``.
"""

from __future__ import annotations

import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

from ..common import config
from ..common.util import (clean_title, dur_text, err, get_bytes, get_json,
                           get_text, ok, resolve_bvid, wbi_sign)

TOOLSET = "bilibili"

QN_LABEL = {127: "8K", 126: "Dolby", 125: "HDR", 120: "4K", 116: "1080P60",
            112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P",
            16: "360P"}

# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

_SEARCH_TYPES = {"video": "video", "user": "bili_user", "up": "bili_user",
                 "bangumi": "media_bangumi", "anime": "media_bangumi"}


def _search(args: dict) -> str:
    kw = (args.get("keyword") or "").strip()
    if not kw:
        return err("need 'keyword'")
    kind = _SEARCH_TYPES.get((args.get("type") or "video").strip().lower(), "video")
    page = max(1, int(args.get("page") or 1))
    order = (args.get("order") or "").strip()
    op = "&order=%s" % urllib.parse.quote(order) if order else ""
    base = "https://api.bilibili.com/x/web-interface/wbi/search/type"
    unsigned = "%s?search_type=%s&keyword=%s&page=%d%s" % (
        base, kind, urllib.parse.quote(kw), page, op)
    try:
        sig = wbi_sign({"search_type": kind, "keyword": kw, "page": str(page),
                        "page_size": "20"})
        signed = base + "?" + urllib.parse.urlencode(sig) + op
        d = get_json(signed)
        if d.get("code") != 0:
            d = get_json(unsigned)
    except Exception:
        d = get_json(unsigned)
    if d.get("code") != 0:
        return err("search code %s: %s" % (d.get("code"), d.get("message")))
    rows = []
    for v in (d.get("data") or {}).get("result") or []:
        if kind == "video":
            rows.append({"title": clean_title(v.get("title")), "bvid": v.get("bvid"),
                         "aid": v.get("aid"), "author": v.get("author"),
                         "play": v.get("play"), "duration": v.get("duration"),
                         "pubdate_text": v.get("pubdate_text")})
        elif kind == "bili_user":
            rows.append({"mid": v.get("mid"), "name": clean_title(v.get("uname")),
                         "sign": (v.get("usign") or "")[:120], "fans": v.get("fans"),
                         "videos": v.get("videos")})
        else:
            rows.append({"title": clean_title(v.get("title")),
                         "season_id": v.get("season_id"),
                         "media_id": v.get("media_id"),
                         "eps": len(v.get("eps") or [])})
    return ok({"keyword": kw, "type": kind, "page": page, "count": len(rows),
               "results": rows})


# ---------------------------------------------------------------------------
# video / comments / danmaku / subtitle
# ---------------------------------------------------------------------------

def _video_info(args: dict) -> str:
    bvid, aid, title, cid, data = resolve_bvid(args)
    stat = data.get("stat") or {}
    pages = [{"page": p.get("page"), "part": p.get("part"), "cid": p.get("cid"),
              "duration": p.get("duration")} for p in (data.get("pages") or [])]
    return ok({"bvid": bvid, "aid": aid, "title": title, "first_cid": cid,
               "author": (data.get("owner") or {}).get("name"),
               "desc": (data.get("desc") or "")[:500],
               "pubdate": data.get("pubdate"), "duration": data.get("duration"),
               "duration_text": dur_text(data.get("duration", 0)),
               "stat": {k: stat.get(k) for k in ("view", "danmaku", "reply",
                                                 "favorite", "coin", "share", "like")},
               "pages": pages})


def _comments(args: dict) -> str:
    bvid, aid, title, _cid, _ = resolve_bvid(args)
    sort = int(args.get("sort") or 2)
    pn = max(1, int(args.get("page") or 1))
    ps = min(int(args.get("size") or 20), 30)
    d = get_json("https://api.bilibili.com/x/v2/reply?type=1&oid=%s&sort=%d&pn=%d&ps=%d"
                 % (aid, sort, pn, ps))
    if d.get("code") != 0:
        return err("reply code %s: %s" % (d.get("code"), d.get("message")))
    rows = [{"rpid": r.get("rpid"),
             "user": (r.get("member") or {}).get("uname"),
             "text": (r.get("content") or {}).get("message", "")[:300],
             "like": r.get("like"), "reply_count": r.get("rcount", 0)}
            for r in (d.get("data") or {}).get("replies") or []]
    page = (d.get("data") or {}).get("page") or {}
    total = page.get("count") or page.get("total") or 0
    return ok({"bvid": bvid, "title": title, "total": total, "page": pn,
               "count": len(rows), "comments": rows})


def _comment_replies(args: dict) -> str:
    bvid, aid, title, _cid, _ = resolve_bvid(args)
    rpid = args.get("root_rpid")
    if not rpid:
        return err("need 'root_rpid' (top-level comment rpid from bili_comments)")
    pn = max(1, int(args.get("page") or 1))
    ps = min(int(args.get("size") or 20), 30)
    d = get_json("https://api.bilibili.com/x/v2/reply/reply?type=1&oid=%s&root=%s&pn=%d&ps=%d"
                 % (aid, rpid, pn, ps))
    if d.get("code") != 0:
        return err("reply/reply code %s: %s" % (d.get("code"), d.get("message")))
    rows = [{"rpid": r.get("rpid"), "parent": r.get("parent"),
             "user": (r.get("member") or {}).get("uname"),
             "text": (r.get("content") or {}).get("message", "")[:300],
             "like": r.get("like")}
            for r in (d.get("data") or {}).get("replies") or []]
    return ok({"bvid": bvid, "title": title, "root_rpid": rpid,
               "count": len(rows), "replies": rows})


def _danmaku(args: dict) -> str:
    bvid, _aid, title, cid, _ = resolve_bvid(args)
    if not cid:
        return err("could not resolve cid")
    txt = get_text("https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid)
    root = ET.fromstring(txt)
    items = []
    limit = int(args.get("limit") or 50)
    for d in root.iter("d"):
        attrs = (d.get("p") or "").split(",")
        items.append({"time_sec": round(float(attrs[0]), 1) if attrs and attrs[0] else None,
                      "text": (d.text or "").strip()[:120]})
        if len(items) >= limit:
            break
    return ok({"bvid": bvid, "title": title, "cid": cid, "count": len(items),
               "danmaku": items})


def _parse_sub_body(url: str) -> list:
    body = get_json(url)
    if isinstance(body, dict):
        body = body.get("body") or []
    return [{"t": round(float(line.get("from", 0)), 1),
             "text": (line.get("content") or "").strip()} for line in (body or [])[:400]]


def _subtitle(args: dict) -> str:
    bvid, _aid, title, cid, _ = resolve_bvid(args)
    if not cid:
        return err("could not resolve cid")
    sig = wbi_sign({"bvid": bvid, "cid": str(cid)})
    d = get_json("https://api.bilibili.com/x/player/wbi/v2?" + urllib.parse.urlencode(sig))
    if d.get("code") != 0:
        return err("player code %s: %s" % (d.get("code"), d.get("message")))
    subs = ((d.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not subs:
        return ok({"bvid": bvid, "title": title, "available": False,
                   "note": "匿名下没有公开 CC 字幕（AI 字幕需要登录，用 bili_subtitle_ai）"})
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
    return ok({"bvid": bvid, "title": title, "available": True, "subtitles": out})


# ---------------------------------------------------------------------------
# download (anonymous cap)
# ---------------------------------------------------------------------------

def _dest_path(args: dict, bvid: str, tag: str, ext: str = "flv") -> str:
    fn = args.get("filename")
    if fn and os.path.isabs(fn):
        os.makedirs(os.path.dirname(fn), exist_ok=True)
        return fn
    return os.path.join(config.scratch_dir("bili"), "%s_%s.%s" % (bvid, tag, ext))


def _download(args: dict) -> str:
    bvid, _aid, title, cid, _ = resolve_bvid(args)
    if not cid:
        return err("could not resolve cid")
    audio_only = bool(args.get("audio_only"))
    qn = int(args.get("quality") or (0 if audio_only else 16))
    fnval = 16 if audio_only else 1
    d = get_json("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=%d&fnval=%d"
                 % (urllib.parse.quote(bvid), cid, qn, fnval))
    if d.get("code") != 0:
        return err("playurl code %s: %s" % (d.get("code"), d.get("message")))
    dd = d.get("data") or {}

    if audio_only:
        audios = (dd.get("dash") or {}).get("audio") or []
        if audios:
            best = max(audios, key=lambda s: s.get("bandwidth", 0))
            url = best.get("base_url") or (best.get("backup_url") or [""])[0]
            dest = _dest_path(args, bvid, "audio_a%s" % best.get("id", 0), "m4a")
            if os.path.exists(dest) and os.path.getsize(dest) > 0 and not args.get("force"):
                return ok({"bvid": bvid, "title": title, "path": dest,
                           "size_mb": round(os.path.getsize(dest) / 1048576, 1),
                           "cached": True, "audio_only": True})
            blob = get_bytes(url)
            with open(dest, "wb") as f:
                f.write(blob)
            return ok({"bvid": bvid, "title": title, "path": dest,
                       "size_mb": round(len(blob) / 1048576, 1),
                       "audio_only": True, "a_id": best.get("id")})
        # anonymous dash refused -> fall back to durl (muxed stream), tell caller
        d = get_json("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=16&fnval=1"
                     % (urllib.parse.quote(bvid), cid))
        dd = d.get("data") or {}
        durls = dd.get("durl") or []
        if not durls:
            return err("anonymous audio-only unavailable; use bili_download then ffmpeg(extract_audio)")
        u = durls[0]
        dest = _dest_path(args, bvid, "p1", "flv")
        blob = get_bytes(u["url"])
        with open(dest, "wb") as f:
            f.write(blob)
        return ok({"bvid": bvid, "title": title, "path": dest,
                   "size_mb": round(len(blob) / 1048576, 1), "audio_only": False,
                   "note": "匿名 dash 音频不可达，已下载含音轨的整体流；用 ffmpeg(action=extract_audio) 提取音频"})

    durls = dd.get("durl") or []
    if not durls:
        return err("no direct stream (anonymous cap); try bili_download_hd after login")
    actual_q = dd.get("quality")
    part_no = min(int(args.get("page_no") or 1), len(durls))
    u = durls[part_no - 1]
    dest = _dest_path(args, bvid, "q%s_p%d" % (actual_q, part_no))
    if os.path.exists(dest) and os.path.getsize(dest) > 0 and not args.get("force"):
        return ok({"bvid": bvid, "title": title, "path": dest,
                   "size_mb": round(os.path.getsize(dest) / 1048576, 1),
                   "quality": actual_q, "cached": True})
    blob = get_bytes(u["url"])
    with open(dest, "wb") as f:
        f.write(blob)
    return ok({"bvid": bvid, "title": title, "path": dest,
               "size_mb": round(len(blob) / 1048576, 1), "quality": actual_q,
               "quality_label": QN_LABEL.get(actual_q, str(actual_q)),
               "accept_quality": dd.get("accept_quality") or [],
               "duration_sec": round(int(u.get("length", 0)) / 1000)})


# ---------------------------------------------------------------------------
# user / space / hot / related / collection / dynamic
# ---------------------------------------------------------------------------

def _user_info(args: dict) -> str:
    mid = args.get("mid")
    if not mid:
        return err("need 'mid' (UP 主数字 id)")
    d = get_json("https://api.bilibili.com/x/web-interface/card?mid=%s" % mid)
    if d.get("code") != 0:
        return err("card code %s: %s" % (d.get("code"), d.get("message")))
    data = d.get("data") or {}
    card = data.get("card") or {}
    lv = (card.get("level_info") or {}).get("current_level")
    return ok({"mid": card.get("mid") or mid, "name": card.get("name"),
               "sign": (card.get("sign") or "")[:300], "level": lv,
               "fans": card.get("fans"), "attention": card.get("attention"),
               "archive_count": data.get("archive_count"),
               "likes": data.get("like_num")})


def _user_videos(args: dict) -> str:
    import json as _json
    mid = args.get("mid")
    if not mid:
        return err("need 'mid' (UP 主数字 id)")
    pn = max(1, int(args.get("page") or 1))
    ps = min(int(args.get("size") or 20), 50)
    order = (args.get("order") or "pubdate").strip()
    params = {"mid": str(mid), "pn": str(pn), "ps": str(ps), "order": order,
              "platform": "web", "web_location": "1550101"}
    d = None
    try:
        sig = wbi_sign(params)
        d = get_json("https://api.bilibili.com/x/space/wbi/arc/search?"
                     + urllib.parse.urlencode(sig))
    except Exception:
        d = None
    if not d or d.get("code") != 0:
        # risk-controlled space API: fall back to search-by-name + author filter
        reason = (d or {}).get("message") or "space API unreachable"
        try:
            info = get_json("https://api.bilibili.com/x/web-interface/card?mid=%s" % mid)
            name = ((info.get("data") or {}).get("card") or {}).get("name") or ""
        except Exception:
            name = ""
        if name:
            try:
                sr = _json.loads(_search({"keyword": name, "type": "video",
                                          "page": pn, "order": order}))
                rows = [v for v in sr.get("results", []) if v.get("author") == name]
                return ok({"mid": mid, "page": pn, "count": len(rows),
                           "videos": rows, "source": "search_fallback",
                           "note": "空间接口被风控（%s）；已用搜索接口按 UP 名过滤，结果可能不全" % reason})
            except Exception as e:
                return err("space %s；搜索兜底也失败：%s" % (reason, e))
        return err("space code %s: %s（UP 可能关闭空间或风控）"
                   % ((d or {}).get("code"), reason))
    data = d.get("data") or {}
    lst = (data.get("list") or {}).get("vlist") or []
    rows = [{"bvid": v.get("bvid"), "aid": v.get("aid"),
             "title": clean_title(v.get("title")),
             "play": v.get("play"), "length": v.get("length"),
             "created": v.get("created")} for v in lst]
    return ok({"mid": mid, "page": pn, "count": len(rows),
               "total": (data.get("page") or {}).get("count"),
               "source": "space", "videos": rows})


def _hot(args: dict) -> str:
    pn = max(1, int(args.get("page") or 1))
    ps = min(int(args.get("size") or 20), 50)
    d = get_json("https://api.bilibili.com/x/web-interface/popular?pn=%d&ps=%d" % (pn, ps))
    if d.get("code") != 0:
        return err("popular code %s: %s" % (d.get("code"), d.get("message")))
    rows = [{"bvid": v.get("bvid"), "title": clean_title(v.get("title")),
             "owner": (v.get("owner") or {}).get("name"),
             "stat": {k: (v.get("stat") or {}).get(k) for k in ("view", "like", "reply")},
             "short_link": v.get("short_link_v2")}
            for v in (d.get("data") or {}).get("list") or []]
    return ok({"page": pn, "count": len(rows), "hot": rows})


def _related(args: dict) -> str:
    bvid, _aid, title, _cid, _ = resolve_bvid(args)
    d = get_json("https://api.bilibili.com/x/web-interface/archive/related?bvid=%s"
                 % urllib.parse.quote(bvid))
    if d.get("code") != 0:
        return err("related code %s: %s" % (d.get("code"), d.get("message")))
    rows = [{"bvid": v.get("bvid"), "title": clean_title(v.get("title")),
             "owner": (v.get("owner") or {}).get("name"),
             "stat": (v.get("stat") or {}).get("view")} for v in d.get("data") or []]
    return ok({"bvid": bvid, "title": title, "count": len(rows), "related": rows})


def _collection(args: dict) -> str:
    mid = args.get("mid")
    season_id = args.get("season_id")
    if not mid or not season_id:
        return err("need 'mid' and 'season_id' (合集/系列 id，来自 UP 空间或视频页)")
    pn = max(1, int(args.get("page") or 1))
    ps = min(int(args.get("size") or 20), 50)
    d = get_json("https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"
                 "?mid=%s&season_id=%s&page_num=%d&page_size=%d"
                 % (mid, season_id, pn, ps))
    if d.get("code") != 0:
        return err("collection code %s: %s" % (d.get("code"), d.get("message")))
    archives = (d.get("data") or {}).get("archives") or []
    rows = [{"bvid": a.get("bvid"), "aid": a.get("aid"),
             "title": clean_title(a.get("title")),
             "duration": a.get("duration"), "pubdate": a.get("pubdate")}
            for a in archives]
    return ok({"mid": mid, "season_id": season_id, "count": len(rows),
               "archives": rows})


# ---------------------------------------------------------------------------
# schemas + registration
# ---------------------------------------------------------------------------

_BVID_ARG = {
    "bvid": {"type": "string", "description": "BV id, e.g. BV1T5nL6rEPb (or use aid)"},
    "aid": {"type": "integer", "description": "AV-number id (alternative to bvid)"},
    "cid": {"type": "integer", "description": "page cid (default: first page)"},
}

_TOOLS = [
    ("bili_search", _search, {
        "keyword": {"type": "string", "description": "search words"},
        "type": {"type": "string", "enum": ["video", "user", "bangumi"],
                 "description": "result kind, default video"},
        "page": {"type": "integer", "description": "result page, default 1"},
        "order": {"type": "string", "description": "sort: '', totalrank, click, pubdate, dm, stow"},
    }, ["keyword"],
     "Search Bilibili by keyword (anonymous). type=video returns title/bvid/aid/author/play; type=user returns UP cards; type=bangumi returns seasons."),
    ("bili_video_info", _video_info, {**_BVID_ARG}, [],
     "Get video metadata: title, author, desc, stat, pages with cid per part. Prerequisite for danmaku/subtitle/download on multi-part videos."),
    ("bili_comments", _comments, {
        **_BVID_ARG,
        "sort": {"type": "integer", "description": "2=by like (default), 0=by time"},
        "page": {"type": "integer", "description": "comment page, default 1"},
        "size": {"type": "integer", "description": "per page, max 30"},
    }, [],
     "Read top-level comments of a video, with rpid (for replies) and like counts."),
    ("bili_comment_replies", _comment_replies, {
        **_BVID_ARG,
        "root_rpid": {"type": "integer", "description": "top-level comment rpid (from bili_comments)"},
        "page": {"type": "integer", "description": "reply page, default 1"},
        "size": {"type": "integer", "description": "per page, max 30"},
    }, ["root_rpid"],
     "Read sub-replies (楼中楼) under one top-level comment."),
    ("bili_danmaku", _danmaku, {
        **_BVID_ARG,
        "limit": {"type": "integer", "description": "max items, default 50"},
    }, [],
     "Read danmaku (bullet comments) of a video part: time_sec + text per item."),
    ("bili_subtitle", _subtitle, {**_BVID_ARG}, [],
     "Read anonymous CC subtitles (timestamped lines). Empty is normal when the video has no public CC; AI subtitles need login (bili_subtitle_ai)."),
    ("bili_download", _download, {
        **_BVID_ARG,
        "quality": {"type": "integer", "description": "qn: 16=360p 32=480p 64=720p 80=1080p; anonymous cap applies"},
        "page_no": {"type": "integer", "description": "which stream part, default 1"},
        "audio_only": {"type": "boolean", "description": "download the audio track only (dash); falls back to the muxed stream"},
        "force": {"type": "boolean", "description": "re-download even if cached"},
        "filename": {"type": "string", "description": "absolute path override"},
    }, [],
     "Download one video part to a local file (scratch dir), returns path. Anonymous quality is capped; use bili_download_hd after login for 1080P+."),
    ("bili_user_info", _user_info, {
        "mid": {"type": "integer", "description": "UP 主数字 id"},
    }, ["mid"],
     "UP profile card: name, sign, level, fans, archive count."),
    ("bili_user_videos", _user_videos, {
        "mid": {"type": "integer", "description": "UP 主数字 id"},
        "page": {"type": "integer", "description": "page, default 1"},
        "size": {"type": "integer", "description": "per page, max 50"},
        "order": {"type": "string", "description": "pubdate (default) | click | stow"},
    }, ["mid"],
     "List an UP's uploads (space API, WBI-signed but anonymous)."),
    ("bili_hot", _hot, {
        "page": {"type": "integer", "description": "page, default 1"},
        "size": {"type": "integer", "description": "per page, max 50"},
    }, [],
     "Bilibili popular/hot list."),
    ("bili_related", _related, {**_BVID_ARG}, [],
     "Related videos for a bvid (topic expansion)."),
    ("bili_collection", _collection, {
        "mid": {"type": "integer", "description": "UP mid"},
        "season_id": {"type": "integer", "description": "collection/season id"},
        "page": {"type": "integer", "description": "page, default 1"},
        "size": {"type": "integer", "description": "per page, max 50"},
    }, ["mid", "season_id"],
     "List a collection/season's videos (合集/系列)."),
]


def register(ctx) -> None:
    for name, handler, props, required, desc in _TOOLS:
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, description=desc, emoji="🎯")