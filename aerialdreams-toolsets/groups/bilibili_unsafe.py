"""``bilibili_unsafe`` group — tools that need the user's login cookie.

Login is a stdlib QR flow against passport.bilibili.com; the credential lands
in ``plugins-data/aerialdreams-toolsets/credential.json`` (atomic write, 0600).
Write tools act AS THE USER and must only run on an explicit user instruction.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import uuid

from ..common import auth, config
from ..common.ffmpeg import ffmpeg_bin, have_ffmpeg, run as ff_run
from ..common.util import (clean_title, err, get_bytes, get_json, ok,
                           post_form, resolve_bvid, wbi_sign)

TOOLSET = "bilibili_unsafe"

QN_LABEL = {127: "8K", 126: "Dolby", 125: "HDR", 120: "4K", 116: "1080P60",
            112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P",
            16: "360P"}

_LOGIN_BOX: dict = {}


_BUVID = {"v": "", "ts": 0.0}


def _buvid3() -> str:
    if _BUVID["v"] and time.time() - _BUVID["ts"] < 3600:
        return _BUVID["v"]
    try:
        d = get_json("https://api.bilibili.com/x/frontend/finger/spi")
        value = ((d.get("data") or {}).get("b_3") or "")
        _BUVID.update(v=value, ts=time.time())
        return value
    except Exception:
        return ""


def _ck() -> tuple:
    """(cred, headers) or (None, None). Adds buvid3 (needed by space APIs)."""
    cred = auth.load_cred()
    if not cred:
        return None, None
    if not cred.get("buvid3"):
        b3 = _buvid3()
        if b3:
            cred = dict(cred)
            cred["buvid3"] = b3
    hdrs = auth.headers(cred)
    hdrs.update({"Origin": "https://www.bilibili.com",
                 "Referer": "https://www.bilibili.com/"})
    return cred, hdrs


def _need_login() -> str:
    return err("未登录：先调用 bili_login 生成二维码，扫码后 bili_login_check 确认")


# ---------------------------------------------------------------------------
# QR login (stdlib)
# ---------------------------------------------------------------------------

def _login(args: dict) -> str:
    if auth.load_cred():
        return ok({"status": "already_logged_in", "credential_file": auth.cred_path()})
    try:
        d = get_json("https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
                     "?source=main-fe-header")
    except Exception as e:
        return err("二维码生成失败：%s" % e)
    if d.get("code") != 0:
        return err("qrcode generate code %s: %s" % (d.get("code"), d.get("message")))
    data = d.get("data") or {}
    url = data.get("url") or ""
    key = str(data.get("qrcode_key") or "")
    if not url or not key:
        return err("二维码生成返回缺字段：%s" % list(data))
    _LOGIN_BOX["qrcode_key"] = key
    qr_path = ""
    qr_error = ""
    try:
        from ..common.qr import write_qr_png
        qr_path = os.path.join(config.plugin_data_dir(), "qrcode_login.png")
        write_qr_png(url, qr_path)
    except Exception as e:
        qr_path = ""
        qr_error = "%s: %s" % (type(e).__name__, str(e)[:200])
    payload = {"status": "qrcode_ready", "qrcode_url": url,
               "qrcode_path": qr_path, "qrcode_key": key,
               "ttl_note": "二维码约 180 秒有效；用户扫码后调用 bili_login_check 轮询",
               "media_hint": "把 qrcode_path 用 MEDIA: 发给用户扫码；或直接打开 qrcode_url"}
    if qr_error:
        payload["qrcode_error"] = qr_error
    return ok(payload)


def _parse_cookies(headers) -> dict:
    out = {}
    for line in headers or []:
        first = (line or "").split(";")[0]
        if "=" in first:
            k, v = first.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_redirect_url(url: str) -> dict:
    """The poll success URL carries credentials in its query string."""
    out = {}
    try:
        q = urllib.parse.urlparse(url).query
        for k, v in urllib.parse.parse_qsl(q):
            out[k] = v
    except Exception:
        pass
    return out


def _login_check(args: dict) -> str:
    key = str(args.get("qrcode_key") or _LOGIN_BOX.get("qrcode_key") or "")
    if not key:
        return err("没有进行中的登录会话；先调用 bili_login")
    url = ("https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
           "?qrcode_key=%s&source=main-fe-header" % urllib.parse.quote(key))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://www.bilibili.com/"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            cookies = _parse_cookies(r.headers.get_all("Set-Cookie") or [])
        d = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return err("登录轮询失败：%s" % e)
    data = d.get("data") or {}
    code = data.get("code")
    if code == 0:
        redirect = data.get("url") or ""
        extra = _parse_redirect_url(redirect)
        merged = dict(extra)
        merged.update({k: v for k, v in cookies.items() if v})
        cred = {
            "sessdata": merged.get("SESSDATA", ""),
            "bili_jct": merged.get("bili_jct", ""),
            "buvid3": merged.get("buvid3", ""),
            "dedeuserid": merged.get("DedeUserID", ""),
            "dedeuserid_ckmd5": merged.get("DedeUserID__ckMd5", ""),
        }
        if not cred["sessdata"]:
            return err("登录成功但没拿到 SESSDATA（响应缺 Set-Cookie 与跳转 URL）；重试 bili_login")
        path = auth.save_cred(cred)
        _LOGIN_BOX.pop("qrcode_key", None)
        return ok({"status": "done", "dedeuserid": cred["dedeuserid"],
                   "credential_saved": path})
    messages = {86101: ("scanning", "未扫码"),
                86090: ("confirming", "已扫码待确认"),
                86038: ("timeout", "二维码过期，重新 bili_login")}
    status, msg = messages.get(code, ("unknown", str(data.get("message") or code)))
    if code == 86038:
        _LOGIN_BOX.pop("qrcode_key", None)
    return ok({"status": status, "message": msg})


# ---------------------------------------------------------------------------
# high-quality download
# ---------------------------------------------------------------------------

def _download_hd(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    bvid, _aid, title, cid, _ = resolve_bvid(args, headers=h)
    if not cid:
        return err("could not resolve cid")
    qn = int(args.get("quality") or 80)
    sig = wbi_sign({"bvid": bvid, "cid": str(cid), "qn": str(qn),
                    "fnval": "4048", "fourk": "1"}, headers=h)
    d = get_json("https://api.bilibili.com/x/player/wbi/playurl?"
                 + urllib.parse.urlencode(sig), headers=h)
    if d.get("code") != 0:
        return err("playurl code %s: %s" % (d.get("code"), d.get("message")))
    dd = d.get("data") or {}
    dash = dd.get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if not videos:
        durls = dd.get("durl") or []
        if not durls:
            return err("没有可用流：%s" % list(dd)[:8])
        blob = get_bytes(durls[0]["url"], headers={"Referer": "https://www.bilibili.com/video/"})
        dest = os.path.join(config.scratch_dir("bili"), "%s_hd_q%s.flv" % (bvid, dd.get("quality")))
        with open(dest, "wb") as f:
            f.write(blob)
        return ok({"bvid": bvid, "title": title, "path": dest,
                   "size_mb": round(len(blob) / 1048576, 1),
                   "quality": dd.get("quality"), "dash": False})
    cand = [v for v in videos if int(v.get("id") or 0) <= qn] or videos
    best = max(cand, key=lambda v: int(v.get("id") or 0))
    got_q = best.get("id")
    base = os.path.join(config.scratch_dir("bili"), "%s_hd_q%s" % (bvid, got_q))
    vpath = base + "_video.m4s"
    apath = base + "_audio.m4s"
    vurl = best.get("base_url") or (best.get("backup_url") or [""])[0]
    with open(vpath, "wb") as f:
        f.write(get_bytes(vurl, headers={"Referer": "https://www.bilibili.com/video/"}))
    if audios:
        abest = max(audios, key=lambda a: int(a.get("bandwidth") or 0))
        aurl = abest.get("base_url") or (abest.get("backup_url") or [""])[0]
        with open(apath, "wb") as f:
            f.write(get_bytes(aurl, headers={"Referer": "https://www.bilibili.com/video/"}))
    out = {"bvid": bvid, "title": title, "quality": got_q,
           "quality_label": QN_LABEL.get(got_q, str(got_q)),
           "video_path": vpath, "audio_path": apath if os.path.exists(apath) else "",
           "size_mb": round(os.path.getsize(vpath) / 1048576, 1)}
    if args.get("mux", True) and os.path.exists(apath) and have_ffmpeg():
        mp4 = base + ".mp4"
        r = ff_run(["-y", "-i", vpath, "-i", apath, "-c", "copy", mp4])
        if r.returncode == 0 and os.path.exists(mp4):
            out["path"] = mp4
            out["muxed"] = True
        else:
            out["mux_error"] = (r.stderr or b"").decode("utf-8", "replace")[-300:]
            out["path"] = vpath
    else:
        out["path"] = vpath
        out["note"] = "dash 视频轨不含音轨；用 ffmpeg(action=mux) 合并" if not out.get("muxed") else ""
    return ok(out)


# ---------------------------------------------------------------------------
# interactions
# ---------------------------------------------------------------------------

def _interact(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    bvid, aid, title, _cid, _ = resolve_bvid(args, headers=h)
    action = (args.get("action") or "like").strip().lower()
    csrf = auth.csrf(cred)
    if action in ("like", "unlike"):
        d = post_form("https://api.bilibili.com/x/web-interface/archive/like",
                      {"bvid": bvid, "like": 1 if action == "like" else 2, "csrf": csrf}, headers=h)
    elif action == "coin":
        d = post_form("https://api.bilibili.com/x/web-interface/coin/add",
                      {"bvid": bvid, "multiply": int(args.get("multiply") or 1),
                       "select_like": 1 if args.get("select_like") else 0, "csrf": csrf},
                      headers=h)
    elif action == "fav":
        folder = args.get("folder_id") or _first_folder(cred, h)
        if not folder:
            return err("没有可用收藏夹；先在 B 站创建收藏夹或传 folder_id")
        d = post_form("https://api.bilibili.com/x/v3/fav/resource/deal",
                      {"rid": aid, "type": 2, "add_media_ids": folder, "csrf": csrf},
                      headers=h)
    elif action == "triple":
        d = post_form("https://api.bilibili.com/x/web-interface/archive/like/triple",
                      {"bvid": bvid, "csrf": csrf}, headers=h)
    else:
        return err("action 只能是 like/unlike/coin/fav/triple")
    if d.get("code") != 0:
        return err("%s code %s: %s" % (action, d.get("code"), d.get("message")))
    return ok({"bvid": bvid, "title": title, "action": action,
               "message": d.get("message") or "ok"})


def _first_folder(cred: dict, h: dict):
    mid = cred.get("dedeuserid") or ""
    try:
        d = get_json("https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=%s" % mid,
                     headers=h)
        lst = (d.get("data") or {}).get("list") or []
        return lst[0].get("id") if lst else None
    except Exception:
        return None


def _dm(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    action = (args.get("action") or "list").strip().lower()
    if action == "send":
        uid = args.get("uid")
        text = (args.get("text") or "").strip()
        if not uid or not text:
            return err("send 需要 'uid' 与 'text'")
        csrf = auth.csrf(cred)
        form = {
            "msg[sender_uid]": cred.get("dedeuserid", ""),
            "msg[receiver_id]": uid,
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "msg[timestamp]": int(time.time()),
            "msg[dev_id]": cred.get("buvid3") or uuid.uuid4().hex,
            "csrf": csrf,
            "csrf_token": csrf,
        }
        d = post_form("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg", form, headers=h)
        if d.get("code") != 0:
            return err("send_msg code %s: %s" % (d.get("code"), d.get("message")))
        return ok({"action": "send", "uid": uid, "message": d.get("message") or "ok"})
    d = get_json("https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions"
                 "?session_type=1&group_id=0&builder=0&mobi_app=web", headers=h)
    if d.get("code") != 0:
        return err("get_sessions code %s: %s" % (d.get("code"), d.get("message")))
    rows = []
    for s in (d.get("data") or {}).get("session_list") or []:
        rows.append({"talker_id": s.get("talker_id"),
                     "unread": s.get("unread_count"),
                     "timestamp": s.get("session_ts"),
                     "last_msg": ((s.get("last_msg") or {}).get("content") or "")[:120]})
    return ok({"action": "list", "count": len(rows), "sessions": rows})


def _comment_write(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    action = (args.get("action") or "post").strip().lower()
    bvid, aid, title, _cid, _ = resolve_bvid(args, headers=h)
    csrf = auth.csrf(cred)
    if action == "post":
        text = (args.get("text") or "").strip()
        if not text:
            return err("需要 'text'")
        form = {"oid": aid, "type": 1, "message": text, "csrf": csrf}
        rpid = int(args.get("reply_to_rpid") or 0)
        if rpid:
            form["parent"] = rpid
            form["root"] = int(args.get("root_rpid") or 0) or rpid
        d = post_form("https://api.bilibili.com/x/v2/reply/add", form, headers=h)
        if d.get("code") != 0:
            return err("reply/add code %s: %s" % (d.get("code"), d.get("message")))
        return ok({"action": "post", "bvid": bvid, "title": title,
                   "rpid": (d.get("data") or {}).get("rpid")})
    rpid = args.get("rpid")
    if not rpid:
        return err("delete 需要 'rpid'")
    d = post_form("https://api.bilibili.com/x/v2/reply/del",
                  {"oid": aid, "type": 1, "rpid": rpid, "csrf": csrf}, headers=h)
    if d.get("code") != 0:
        return err("reply/del code %s: %s" % (d.get("code"), d.get("message")))
    return ok({"action": "delete", "bvid": bvid, "rpid": rpid})


def _danmaku_post(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    text = (args.get("text") or "").strip()
    if not text:
        return err("需要 'text'")
    bvid, _aid, title, cid, _ = resolve_bvid(args, headers=h)
    if not cid:
        return err("could not resolve cid")
    offset = float(args.get("time_offset_sec") or 0)
    seg = int(offset // 600) + 1
    form = {"type": 1, "oid": cid, "msg": text, "bvid": bvid,
            "progress": int(offset * 1000), "color": 16777215,
            "fontsize": 25, "pool": 0, "mode": 1, "plat": 1,
            "csrf": auth.csrf(cred)}
    d = post_form("https://api.bilibili.com/x/v2/dm/post", form, headers=h)
    if d.get("code") != 0:
        return err("danmaku code %s: %s" % (d.get("code"), d.get("message")))
    return ok({"bvid": bvid, "title": title, "cid": cid, "segment": seg,
               "dm_id": (d.get("data") or {}).get("dm_id")})


# ---------------------------------------------------------------------------
# favorites / watch later / history / AI subtitle / follow
# ---------------------------------------------------------------------------

def _fav(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    action = (args.get("action") or "folders").strip().lower()
    mid = cred.get("dedeuserid", "")
    if action == "folders":
        d = get_json("https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=%s" % mid,
                     headers=h)
        if d.get("code") != 0:
            return err("fav folders code %s: %s" % (d.get("code"), d.get("message")))
        rows = [{"id": f.get("id"), "title": f.get("title"), "media_count": f.get("media_count")}
                for f in (d.get("data") or {}).get("list") or []]
        return ok({"action": "folders", "count": len(rows), "folders": rows})
    if action == "list":
        fid = args.get("folder_id")
        if not fid:
            return err("list 需要 'folder_id'")
        pn = max(1, int(args.get("page") or 1))
        ps = min(int(args.get("size") or 20), 40)
        d = get_json("https://api.bilibili.com/x/v3/fav/resource/list?media_id=%s&pn=%d&ps=%d&platform=web"
                     % (fid, pn, ps), headers=h)
        if d.get("code") != 0:
            return err("fav list code %s: %s" % (d.get("code"), d.get("message")))
        medias = (d.get("data") or {}).get("medias") or []
        rows = [{"bvid": m.get("bvid"), "aid": m.get("id"),
                 "title": clean_title(m.get("title")),
                 "upper": (m.get("upper") or {}).get("name")} for m in medias]
        return ok({"action": "list", "count": len(rows), "medias": rows})
    bvid, aid, title, _cid, _ = resolve_bvid(args, headers=h)
    fid = args.get("folder_id") or _first_folder(cred, h)
    if not fid:
        return err("没有可用收藏夹；传 folder_id")
    field = "add_media_ids" if action == "add" else "del_media_ids"
    if action not in ("add", "remove"):
        return err("action 只能是 folders/list/add/remove")
    d = post_form("https://api.bilibili.com/x/v3/fav/resource/deal",
                  {"rid": aid, "type": 2, field: fid, "csrf": auth.csrf(cred)}, headers=h)
    if d.get("code") != 0:
        return err("%s code %s: %s" % (action, d.get("code"), d.get("message")))
    return ok({"action": action, "bvid": bvid, "folder_id": fid})


def _watch_later(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    action = (args.get("action") or "list").strip().lower()
    if action == "list":
        d = get_json("https://api.bilibili.com/x/v2/history/toview?ps=30", headers=h)
        if d.get("code") != 0:
            return err("toview code %s: %s" % (d.get("code"), d.get("message")))
        rows = [{"bvid": v.get("bvid"), "aid": v.get("aid"),
                 "title": clean_title(v.get("title")),
                 "duration": v.get("duration")}
                for v in (d.get("data") or {}).get("list") or []]
        return ok({"action": "list", "count": len(rows), "videos": rows})
    bvid, aid, title, _cid, _ = resolve_bvid(args, headers=h)
    if action == "add":
        d = post_form("https://api.bilibili.com/x/v2/history/toview/add",
                      {"aid": aid, "csrf": auth.csrf(cred)}, headers=h)
    elif action == "remove":
        d = post_form("https://api.bilibili.com/x/v2/history/toview/del",
                      {"aid": aid, "csrf": auth.csrf(cred)}, headers=h)
    else:
        return err("action 只能是 list/add/remove")
    if d.get("code") != 0:
        return err("%s code %s: %s" % (action, d.get("code"), d.get("message")))
    return ok({"action": action, "bvid": bvid, "title": title})


def _history(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    pn = max(1, int(args.get("page") or 1))
    ps = min(int(args.get("size") or 20), 30)
    d = get_json("https://api.bilibili.com/x/v2/history?pn=%d&ps=%d" % (pn, ps), headers=h)
    if d.get("code") != 0:
        return err("history code %s: %s" % (d.get("code"), d.get("message")))
    data = d.get("data") or []
    if isinstance(data, dict):
        data = data.get("list") or []
    rows = [{"bvid": v.get("bvid"), "aid": v.get("aid"),
             "title": clean_title(v.get("title")),
             "author": v.get("author_name") or (v.get("owner") or {}).get("name"),
             "progress": v.get("progress"), "duration": v.get("duration")}
            for v in data]
    return ok({"page": pn, "count": len(rows), "history": rows})


def _subtitle_ai(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    bvid, _aid, title, cid, _ = resolve_bvid(args, headers=h)
    if not cid:
        return err("could not resolve cid")
    sig = wbi_sign({"bvid": bvid, "cid": str(cid)}, headers=h)
    d = get_json("https://api.bilibili.com/x/player/wbi/v2?"
                 + urllib.parse.urlencode(sig), headers=h)
    if d.get("code") != 0:
        return err("player code %s: %s" % (d.get("code"), d.get("message")))
    subs = ((d.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    if not subs:
        return ok({"bvid": bvid, "title": title, "available": False,
                   "note": "该视频没有字幕（含 AI 字幕）"})
    out = []
    for s in subs:
        url = s.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        row = {"lan": s.get("lan"), "name": s.get("lan_doc"), "ai": s.get("ai_type"),
               "lines": []}
        try:
            body = get_json(url, headers=h)
            if isinstance(body, dict):
                body = body.get("body") or []
            row["lines"] = [{"t": round(float(x.get("from", 0)), 1),
                             "text": (x.get("content") or "").strip()}
                            for x in (body or [])[:600]]
        except Exception as e:
            row["error"] = str(e)[:160]
        out.append(row)
    return ok({"bvid": bvid, "title": title, "available": True, "subtitles": out})


def _follow(args: dict) -> str:
    cred, h = _ck()
    if not cred:
        return _need_login()
    mid = args.get("mid")
    if not mid:
        return err("需要 'mid'")
    action = (args.get("action") or "follow").strip().lower()
    act = 1 if action == "follow" else 2
    if action not in ("follow", "unfollow"):
        return err("action 只能是 follow/unfollow")
    d = post_form("https://api.bilibili.com/x/relation/modify",
                  {"fid": mid, "act": act, "re_src": 11,
                   "csrf": auth.csrf(cred)}, headers=h)
    if d.get("code") != 0:
        return err("%s code %s: %s" % (action, d.get("code"), d.get("message")))
    return ok({"action": action, "mid": mid})


def _dynamic(args: dict) -> str:
    """Space dynamic feed: reliably needs the account cookie + buvid3."""
    cred, h = _ck()
    if not cred:
        return _need_login()
    mid = args.get("mid")
    if not mid:
        return err("need 'mid'")
    offset = (args.get("offset") or "").strip()
    limit = min(int(args.get("limit") or 20), 50)
    params = {"host_mid": str(mid), "features": "itemOpusStyle"}
    if offset:
        params["offset"] = offset
    try:
        sig = wbi_sign(params, headers=h)
        url = ("https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?"
               + urllib.parse.urlencode(sig))
        d = get_json(url, headers=dict(h, Referer="https://space.bilibili.com/%s/dynamic" % mid))
    except Exception as e:
        return err("dynamic: %s" % e)
    if d.get("code") != 0:
        return err("dynamic code %s: %s" % (d.get("code"), d.get("message")))
    data = d.get("data") or {}
    items = []
    for it in (data.get("items") or [])[:limit]:
        mods = it.get("modules") or {}
        author = mods.get("module_author") or {}
        dyn = mods.get("module_dynamic") or {}
        desc = (dyn.get("desc") or {}).get("text") or ""
        major = dyn.get("major") or {}
        arch = major.get("archive") or {}
        items.append({"id": it.get("id_str"), "type": it.get("type"),
                      "pub_time": author.get("pub_time"), "text": desc[:300],
                      "bvid": arch.get("bvid"), "title": clean_title(arch.get("title"))})
    return ok({"mid": mid, "count": len(items),
               "offset": data.get("offset"), "has_more": data.get("has_more"),
               "items": items})


# ---------------------------------------------------------------------------
# schemas + registration
# ---------------------------------------------------------------------------

_BVID_ARG = {
    "bvid": {"type": "string", "description": "BV id"},
    "aid": {"type": "integer", "description": "AV-number id (alternative)"},
    "cid": {"type": "integer", "description": "page cid (default: first page)"},
}

_TOOLS = [
    ("bili_login", _login, {}, [],
     "登录第一步（需要账号）：生成 B 站扫码登录二维码，返回 qrcode_path/qrcode_url。把图片用 MEDIA: 发给用户，扫码后调 bili_login_check 轮询。凭据存 plugins-data/aerialdreams-toolsets/credential.json。"),
    ("bili_login_check", _login_check, {
        "qrcode_key": {"type": "string", "description": "可选；默认用最近一次 bili_login 的 key"},
    }, [],
     "登录第二步：轮询扫码状态（scanning/confirming/done/timeout）。done 时凭据落盘。"),
    ("bili_download_hd", _download_hd, {
        **_BVID_ARG,
        "quality": {"type": "integer", "description": "qn: 80=1080p 112=1080P+ 116=1080P60 120=4K; default 80"},
        "mux": {"type": "boolean", "description": "mux dash video+audio with ffmpeg (default true)"},
    }, [],
     "登录后高清下载：dash 取最高 ≤qn 的视频轨与音频轨，auto-mux 成 mp4（ffmpeg），返回 path。"),
    ("bili_interact", _interact, {
        **_BVID_ARG,
        "action": {"type": "string", "enum": ["like", "unlike", "coin", "fav", "triple"],
                   "description": "like/unlike=点赞; coin=投币; fav=收藏; triple=一键三连"},
        "multiply": {"type": "integer", "description": "coin: 1 or 2 (default 1)"},
        "select_like": {"type": "boolean", "description": "coin: also like"},
        "folder_id": {"type": "integer", "description": "fav: target folder id (default first folder)"},
    }, ["bvid", "action"],
     "以用户账号互动：点赞/取消赞/投币/收藏/一键三连。写操作，仅在用户明确要求时调用。"),
    ("bili_dm", _dm, {
        "action": {"type": "string", "enum": ["send", "list"], "description": "send/list"},
        "uid": {"type": "integer", "description": "send: receiver uid"},
        "text": {"type": "string", "description": "send: message text"},
    }, ["action"],
     "私信：send 发一条私信（需要 uid+text），list 列最近会话。写操作，仅在用户明确要求时调用。"),
    ("bili_comment_write", _comment_write, {
        "action": {"type": "string", "enum": ["post", "delete"], "description": "post/delete"},
        **_BVID_ARG,
        "text": {"type": "string", "description": "post: comment text"},
        "reply_to_rpid": {"type": "integer", "description": "post: reply target rpid (0=new top-level)"},
        "root_rpid": {"type": "integer", "description": "post: thread root for nested reply"},
        "rpid": {"type": "integer", "description": "delete: comment rpid"},
    }, ["action"],
     "评论写操作：post 发评论/回复，delete 删除自己的评论。仅在用户明确要求时调用。"),
    ("bili_danmaku_post", _danmaku_post, {
        **_BVID_ARG,
        "text": {"type": "string"},
        "time_offset_sec": {"type": "number", "description": "flight time, seconds; default 0"},
    }, ["bvid", "text"],
     "发送弹幕（写操作，最高封禁风险）。仅在用户明确要求时调用。"),
    ("bili_fav", _fav, {
        "action": {"type": "string", "enum": ["folders", "list", "add", "remove"]},
        "folder_id": {"type": "integer", "description": "target folder id"},
        "bvid": {"type": "string"},
        "aid": {"type": "integer"},
        "page": {"type": "integer"},
        "size": {"type": "integer"},
    }, ["action"],
     "收藏夹：folders 列出我的收藏夹，list 看夹内视频，add/remove 把视频加入/移出收藏夹。写操作仅在明确要求时调用。"),
    ("bili_watch_later", _watch_later, {
        "action": {"type": "string", "enum": ["list", "add", "remove"]},
        "bvid": {"type": "string"},
        "aid": {"type": "integer"},
    }, ["action"],
     "稍后再看：list/add/remove。"),
    ("bili_history", _history, {
        "page": {"type": "integer", "description": "page, default 1"},
        "size": {"type": "integer", "description": "per page, max 30"},
    }, [],
     "我的播放历史（登录后）。"),
    ("bili_subtitle_ai", _subtitle_ai, {**_BVID_ARG}, [],
     "登录后读取字幕（含 AI 字幕），带时间戳。"),
    ("bili_follow", _follow, {
        "mid": {"type": "integer", "description": "UP mid"},
        "action": {"type": "string", "enum": ["follow", "unfollow"]},
    }, ["mid"],
     "关注/取关 UP 主（写操作）。"),
    ("bili_dynamic", _dynamic, {
        "mid": {"type": "integer", "description": "UP mid"},
        "offset": {"type": "string", "description": "分页 offset（上次返回的 offset，空=第一页）"},
        "limit": {"type": "integer", "description": "最多条数，默认 20"},
    }, ["mid"],
     "【需登录】读取 UP 空间动态（文字/图文），需登录 cookie + buvid3；返回 offset 供翻页。"),
]


def register(ctx) -> None:
    for name, handler, props, required, desc in _TOOLS:
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, description=desc, emoji="🔓")