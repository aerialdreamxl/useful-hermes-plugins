"""``multimedia-utils`` group: ffmpeg wrapper, DashScope ASR, Qwen-Omni analysis."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ..common import config
from ..common import ffmpeg as ff
from ..common.util import err, get_json, ok, post_json

TOOLSET = "multimedia-utils"

# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

_FF_ACTIONS = {"probe", "convert", "trim", "mux", "extract_audio",
               "extract_frames", "thumbnail", "burn_subtitle", "gif"}


def _out_path(args: dict, stem: str, ext: str) -> str:
    fn = args.get("output") or args.get("filename")
    if fn:
        os.makedirs(os.path.dirname(os.path.abspath(fn)), exist_ok=True)
        return fn
    return os.path.join(config.scratch_dir("media"), "%s_%s.%s" % (stem, int(time.time()), ext))


def _stem(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(os.path.basename(path))[0])[:40] or "out"


def _ffmpeg(args: dict) -> str:
    action = (args.get("action") or "probe").strip().lower()
    if action not in _FF_ACTIONS:
        return err("action 只能是 " + ", ".join(sorted(_FF_ACTIONS)))
    inp = (args.get("input") or "").strip()
    if action != "probe" and action != "mux" and not inp:
        return err("需要 'input'")
    if not ff.have_ffmpeg():
        return err("未找到 ffmpeg（PATH 或 AERIALDREAMS_FFMPEG）")
    try:
        if action == "probe":
            if not inp or not os.path.exists(inp):
                return err("probe 需要存在的本地文件")
            info = ff.probe(inp)
            fmt = info.get("format") or {}
            streams = [{"index": s.get("index"), "type": s.get("codec_type"),
                        "codec": s.get("codec_name"),
                        "width": s.get("width"), "height": s.get("height"),
                        "channels": s.get("channels"),
                        "bit_rate": s.get("bit_rate")}
                       for s in info.get("streams") or []]
            return ok({"action": "probe", "path": inp,
                       "duration": fmt.get("duration"),
                       "size_mb": round(int(fmt.get("size") or 0) / 1048576, 2),
                       "format": fmt.get("format_name"), "streams": streams})
        if action == "convert":
            out = _out_path(args, _stem(inp), (args.get("format") or "mp4").lstrip("."))
            r = ff.run(["-y", "-i", inp, "-c:v", "libx264", "-preset", "veryfast",
                        "-c:a", "aac", "-b:a", "128k", out])
        elif action == "trim":
            start = float(args.get("start") or 0)
            dur = float(args.get("duration") or 0)
            out = _out_path(args, _stem(inp), os.path.splitext(inp)[1].lstrip(".") or "mp4")
            cmd = ["-y", "-ss", str(start)]
            if dur > 0:
                cmd += ["-t", str(dur)]
            cmd += ["-i", inp, "-c", "copy", out]
            r = ff.run(cmd)
        elif action == "mux":
            video = args.get("video") or inp
            audio = args.get("audio") or ""
            if not video or not audio:
                return err("mux 需要 'video' 与 'audio'")
            out = _out_path(args, _stem(video), "mp4")
            r = ff.run(["-y", "-i", video, "-i", audio, "-c", "copy", out])
        elif action == "extract_audio":
            fmt = (args.get("format") or "mp3").lstrip(".")
            out = _out_path(args, _stem(inp) + "_audio", fmt)
            if fmt == "m4a":
                r = ff.run(["-y", "-i", inp, "-vn", "-c:a", "aac", "-b:a", "192k", out])
            else:
                r = ff.run(["-y", "-i", inp, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out])
        elif action == "extract_frames":
            fps = float(args.get("fps") or 0)
            count = int(args.get("count") or 0)
            outdir = args.get("output") or config.scratch_dir("frames", _stem(inp))
            os.makedirs(outdir, exist_ok=True)
            pattern = os.path.join(outdir, "frame_%04d.jpg")
            cmd = ["-y", "-i", inp]
            cmd += (["-vf", "fps=%s" % fps] if fps > 0 else ["-vframes", str(count or 12)])
            cmd += [pattern]
            r = ff.run(cmd)
        elif action == "thumbnail":
            t = float(args.get("time") or 0)
            out = _out_path(args, _stem(inp) + "_thumb", "jpg")
            r = ff.run(["-y", "-ss", str(t), "-i", inp, "-vframes", "1", out])
        elif action == "burn_subtitle":
            sub = args.get("subtitle") or ""
            if not sub:
                return err("burn_subtitle 需要 'subtitle'（srt/ass 路径）")
            out = _out_path(args, _stem(inp) + "_sub", "mp4")
            esc = sub.replace("\\", "/").replace(":", "\\:")
            r = ff.run(["-y", "-i", inp, "-vf", "subtitles='%s'" % esc,
                        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "copy", out])
        else:  # gif
            start = float(args.get("start") or 0)
            dur = float(args.get("duration") or 3)
            fps = float(args.get("fps") or 10)
            width = int(args.get("width") or 480)
            out = _out_path(args, _stem(inp) + "_gif", "gif")
            r = ff.run(["-y", "-ss", str(start), "-t", str(dur), "-i", inp,
                        "-vf", "fps=%s,scale=%d:-1:flags=lanczos" % (fps, width), out])
        if r.returncode != 0:
            tail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            return err("ffmpeg 失败：%s" % (" | ".join(tail[-3:])[:400]))
        if action == "extract_frames":
            files = sorted(os.path.join(outdir, f) for f in os.listdir(outdir)
                           if f.lower().endswith((".jpg", ".png")))
            return ok({"action": action, "dir": outdir, "count": len(files),
                       "frames": files[:60]})
        size = os.path.getsize(out) if os.path.exists(out) else 0
        return ok({"action": action, "path": out,
                   "size_mb": round(size / 1048576, 2)})
    except Exception as e:
        return err("ffmpeg %s: %s: %s" % (action, type(e).__name__, str(e)[:300]))


# ---------------------------------------------------------------------------
# DashScope helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    env_name = config.cfg("multimedia.api_key_env", "DASHSCOPE_API_KEY")
    return os.environ.get(env_name, "") or os.environ.get("DASHSCOPE_API_KEY", "") or ""


def _base_url() -> str:
    base = (config.cfg("multimedia.base_url", "") or "").strip()
    return (base or "https://dashscope.aliyuncs.com").rstrip("/")


def _compat_url() -> str:
    base = _base_url()
    if base.endswith("/compatible-mode/v1"):
        return base
    return base + "/compatible-mode/v1"


def _auth_headers(extra: dict | None = None) -> dict:
    h = {"Authorization": "Bearer %s" % _api_key()}
    h.update(extra or {})
    return h


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


def _b64_data_uri(path: str) -> str:
    mime = _guess_mime(path)
    with open(path, "rb") as f:
        blob = f.read()
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii"))


def _post_json_auth(url: str, payload: dict, extra: dict | None = None,
                    timeout: int = 120) -> dict:
    body = json.dumps(payload).encode()
    headers = _auth_headers({"Content-Type": "application/json"})
    headers.update(extra or {})
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:1000]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s: %s" % (e.code, url.rsplit("/", 2)[-2:], detail or e.reason))


def _get_auth(url: str, extra: dict | None = None, timeout: int = 60) -> dict:
    headers = _auth_headers(extra)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _upload_temp(path: str, model: str) -> str:
    """Upload a local file to DashScope temp OSS; return oss:// url."""
    policy = _get_auth("%s/api/v1/uploads?action=getPolicy&model=%s"
                       % (_base_url(), urllib.parse.quote(model)))
    data = policy.get("data") or {}
    if not data.get("upload_host"):
        raise RuntimeError("upload policy missing: %s" % str(policy)[:300])
    key = "%s/%s" % (data.get("upload_dir", ""), os.path.basename(path))
    boundary = "----HermesBoundary" + uuid.uuid4().hex
    fields = [("OSSAccessKeyId", data.get("oss_access_key_id", "")),
              ("Signature", data.get("signature", "")),
              ("policy", data.get("policy", "")),
              ("x-oss-object-acl", data.get("x_oss_object_acl", "")),
              ("x-oss-forbid-overwrite", data.get("x_oss_forbid_overwrite", "")),
              ("key", key),
              ("success_action_status", "200")]
    chunks = []
    for name, value in fields:
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, name, value)).encode())
    with open(path, "rb") as f:
        blob = f.read()
    chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                   "Content-Type: %s\r\n\r\n" % (boundary, os.path.basename(path),
                                                 _guess_mime(path))).encode())
    chunks.append(blob)
    chunks.append(("\r\n--%s--\r\n" % boundary).encode())
    body = b"".join(chunks)
    req = urllib.request.Request(data["upload_host"], data=body, headers={
        "Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=300) as r:
        if r.status not in (200, 204):
            raise RuntimeError("upload failed HTTP %s" % r.status)
    return "oss://%s" % key


# ---------------------------------------------------------------------------
# ASR (sync flash / async filetrans + steer back)
# ---------------------------------------------------------------------------

_TASKS: dict = {}
_STEER_LOCK = threading.Lock()


def _session_key() -> str:
    try:
        from gateway.session_context import get_session_env
        key = get_session_env("HERMES_SESSION_KEY") or ""
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("HERMES_SESSION_KEY", "")


def _norm(key: str) -> str:
    return (key or "").split(":")[-1].strip()


def _live_agent(session_key: str):
    target = _norm(session_key)
    try:
        srv = sys.modules.get("tui_gateway.server")
        if srv is not None:
            for sid, sess in (getattr(srv, "_sessions", {}) or {}).items():
                if not isinstance(sess, dict):
                    continue
                agent = sess.get("agent")
                if not agent:
                    continue
                keys = {str(sid), str(sess.get("session_key") or ""),
                        str(getattr(agent, "session_id", "") or "")}
                if target and any(_norm(k) == target for k in keys if k):
                    return agent
    except Exception:
        pass
    return None


def _steer(session_key: str, text: str) -> bool:
    agent = _live_agent(session_key)
    if not agent or not hasattr(agent, "steer"):
        return False
    try:
        agent.steer(text)
        return True
    except Exception:
        return False


def _fmt_sentences(sentences: list, with_time: bool = True) -> str:
    lines = []
    for s in sentences:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if with_time:
            begin = s.get("begin_time") or 0
            lines.append("[%s] %s" % (_ms_text(begin), text))
        else:
            lines.append(text)
    return "\n".join(lines)


def _ms_text(ms) -> str:
    try:
        sec = int(ms) / 1000.0
    except (TypeError, ValueError):
        sec = 0.0
    m, s = divmod(sec, 60)
    return "%d:%05.2f" % (int(m), s)


def _asr_sync(path_or_url: str, language: str, prompt: str = "") -> dict:
    """qwen3-asr-flash is a dedicated ASR task: audio part ONLY.

    A sibling text part or an ``input_audio.format`` field makes the service
    answer 400 "dedicated task asr does not support this input" (verified).
    """
    model = config.cfg("multimedia.asr.sync_model", "qwen3-asr-flash")
    data = _b64_data_uri(path_or_url) if os.path.exists(path_or_url) else path_or_url
    part = {"type": "input_audio", "input_audio": {"data": data}}
    payload = {"model": model,
               "messages": [{"role": "user", "content": [part]}],
               "stream": False}
    if language:
        payload["asr_options"] = {"language": language}
    d = _post_json_auth(_compat_url() + "/chat/completions", payload, timeout=300)
    choices = d.get("choices") or []
    msg = (choices[0].get("message") if choices else {}) or {}
    return {"text": msg.get("content") or "", "model": model,
            "usage": d.get("usage") or {},
            "language": ((msg.get("annotations") or [{}])[0] or {}).get("language")}


def _asr_submit(src: str, language: str, diarization: bool) -> dict:
    model = config.cfg("multimedia.asr.async_model", "qwen-audio-3.1-asr-flash-filetrans")
    if os.path.exists(src):
        url = _upload_temp(src, model)
        extra = {"X-DashScope-OssResourceResolve": "enable"}
    else:
        url = src
        extra = {}
    params = {"diarization_enabled": bool(diarization)}
    if language:
        params["language_hints"] = [language]
    payload = {"model": model, "input": {"file_urls": [url]}, "parameters": params}
    d = _post_json_auth(_base_url() + "/api/v1/services/audio/asr/transcription",
                        payload, extra=dict(extra, **{"X-DashScope-Async": "enable"}),
                        timeout=120)
    task_id = (d.get("output") or {}).get("task_id") or ""
    if not task_id:
        raise RuntimeError("submit failed: %s" % str(d)[:300])
    return {"task_id": task_id, "url": url, "model": model}


def _asr_poll(task_id: str) -> dict:
    return _get_auth(_base_url() + "/api/v1/tasks/%s" % task_id)


def _asr_result(task_id: str) -> dict:
    d = _asr_poll(task_id)
    out = d.get("output") or {}
    status = out.get("task_status") or ""
    if status in ("PENDING", "RUNNING"):
        return {"status": status.lower(), "task_id": task_id}
    if status != "SUCCEEDED":
        return {"status": "failed", "task_id": task_id,
                "error": str(out.get("message") or d.get("message") or status)[:300]}
    results = out.get("results") or []
    if not results:
        return {"status": "failed", "task_id": task_id, "error": "empty results"}
    first = results[0]
    if first.get("subtask_status") not in (None, "SUCCEEDED"):
        return {"status": "failed", "task_id": task_id,
                "error": str(first.get("message") or first.get("subtask_status"))[:300]}
    turl = first.get("transcription_url")
    if not turl:
        return {"status": "failed", "task_id": task_id, "error": "no transcription_url"}
    body = _get_auth(turl)
    transcripts = body.get("transcripts") or []
    sentences = []
    text = ""
    for t in transcripts:
        text += (t.get("text") or "")
        sentences.extend(t.get("sentences") or [])
    return {"status": "done", "task_id": task_id, "text": text,
            "sentences": sentences, "transcription_url": turl}


def _watch_task(task_id: str, session_key: str, label: str) -> None:
    deadline = time.time() + float(config.cfg("multimedia.asr.watch_max_seconds", 7200))
    interval = float(config.cfg("multimedia.asr.poll_interval", 5))
    while time.time() < deadline:
        try:
            res = _asr_result(task_id)
        except Exception:
            res = {"status": "poll_error"}
        if res.get("status") == "done":
            _TASKS[task_id] = res
            text = res.get("text") or ""
            out = os.path.join(config.scratch_dir("asr"), "asr_%s.txt" % task_id)
            try:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                out = ""
            note = ("【ASR 完成】任务 %s（%s）已转写完毕。全文 %d 字，结果文件：%s\n"
                    "前 300 字：%s" % (task_id, label or "audio", len(text), out or "(未落盘)",
                                      text[:300]))
            with _STEER_LOCK:
                _steer(session_key, note)
            return
        if res.get("status") == "failed":
            _TASKS[task_id] = res
            with _STEER_LOCK:
                _steer(session_key, "【ASR 失败】任务 %s：%s" % (task_id, res.get("error")))
            return
        time.sleep(interval)
    _TASKS[task_id] = {"status": "timeout", "task_id": task_id}


def _asr(args: dict) -> str:
    action = (args.get("action") or "transcribe").strip().lower()
    if action == "status":
        tid = args.get("task_id") or ""
        if not tid:
            return err("status 需要 'task_id'")
        try:
            res = _asr_result(tid)
        except Exception as e:
            return err("poll: %s" % e)
        _TASKS[tid] = res
        return ok({"task_id": tid, "status": res.get("status"),
                   "text": res.get("text") if res.get("status") == "done" else "",
                   "error": res.get("error", "")})
    if action == "result":
        tid = args.get("task_id") or ""
        if not tid:
            return err("result 需要 'task_id'")
        res = _TASKS.get(tid) or _asr_result(tid)
        if res.get("status") != "done":
            return ok({"task_id": tid, "status": res.get("status", "unknown"),
                       "text": "", "error": res.get("error", ""),
                       "hint": "还没完成；用 action=status 轮询"})
        return ok({"task_id": tid, "status": "done", "text": res.get("text"),
                   "sentences": res.get("sentences", [])[:800]})
    src = (args.get("source") or args.get("file") or args.get("url") or "").strip()
    if not src:
        return err("需要 'source'（本地路径或 http(s) URL）")
    if not _api_key():
        return err("缺 API Key：环境变量 %s（或 DASHSCOPE_API_KEY）"
                   % config.cfg("multimedia.api_key_env", "DASHSCOPE_API_KEY"))
    language = (args.get("language") or "").strip()
    timestamps = bool(args.get("timestamps"))
    wait_seconds = min(max(int(args.get("wait_seconds") or 0), 0), 1800)
    if not wait_seconds:
        wait_seconds = int(config.cfg("multimedia.asr.wait_seconds", 300))
    try:
        local = os.path.exists(src)
        size = os.path.getsize(src) if local else 0
        # small local file + no timestamps -> sync flash (fast, cheap)
        if local and not timestamps and size <= 7 * 1024 * 1024:
            res = _asr_sync(src, language, (args.get("prompt") or "").strip())
            return ok({"action": "transcribe", "mode": "sync", "text": res["text"],
                       "model": res["model"], "usage": res.get("usage", {}),
                       "language": res.get("language", ""),
                       "note": "同步接口不返回时间戳；需要时间戳用 timestamps=true"})
        sub = _asr_submit(src, language, bool(args.get("diarization")))
        session_key = _session_key()
        label = os.path.basename(src) if local else src[:80]
        _TASKS[sub["task_id"]] = {"status": "pending", "task_id": sub["task_id"]}
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            res = _asr_result(sub["task_id"])
            if res.get("status") == "done":
                _TASKS[sub["task_id"]] = res
                return ok({"action": "transcribe", "mode": "async", "task_id": sub["task_id"],
                           "status": "done", "text": res.get("text"),
                           "sentences": res.get("sentences", [])[:800],
                           "model": sub["model"]})
            if res.get("status") == "failed":
                _TASKS[sub["task_id"]] = res
                return err("ASR 失败：%s" % res.get("error"))
            time.sleep(float(config.cfg("multimedia.asr.poll_interval", 5)))
        threading.Thread(target=_watch_task, args=(sub["task_id"], session_key, label),
                         daemon=True).start()
        return ok({"action": "transcribe", "mode": "async", "task_id": sub["task_id"],
                   "status": "pending",
                   "hint": "任务已提交，后台继续轮询；完成后会 steer 回本会话，或用 dashscope_asr(action='status', task_id=...) 主动查"})
    except Exception as e:
        return err("asr: %s: %s" % (type(e).__name__, str(e)[:400]))


# ---------------------------------------------------------------------------
# Qwen-Omni analysis (compress -> trim, never frames)
# ---------------------------------------------------------------------------

def _media_kind(path: str) -> str:
    if ff.has_video_stream(path):
        return "video"
    return "audio"


def _compress_to_limit(path: str, limit_mb: float) -> tuple:
    """Return (path, truncated_seconds, note). Compress then trim, never frames."""
    limit = int(limit_mb * 1024 * 1024)
    size = os.path.getsize(path)
    if size <= limit:
        return path, 0.0, ""
    kind = _media_kind(path)
    dur = max(ff.duration_seconds(path), 0.1)
    outdir = config.scratch_dir("omni")
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.splitext(os.path.basename(path))[0])[:40]
    current = path
    note = ""
    for attempt in range(3):
        size = os.path.getsize(current)
        if size <= limit:
            break
        dur = max(ff.duration_seconds(current), 0.1)
        target_bits = limit * 8 * 0.88
        vbr = max(int(target_bits / dur) - 64000, 120000)
        out = os.path.join(outdir, "%s_comp%d.mp4" % (stem, attempt))
        if kind == "video":
            r = ff.run(["-y", "-i", current, "-vf", "scale=-2:480",
                        "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vbr),
                        "-maxrate", str(int(vbr * 1.3)), "-bufsize", str(int(vbr * 2)),
                        "-c:a", "aac", "-b:a", "64k", out])
        else:
            out = os.path.join(outdir, "%s_comp%d.mp3" % (stem, attempt))
            r = ff.run(["-y", "-i", current, "-vn", "-c:a", "libmp3lame",
                        "-b:a", "48k", out])
        if r.returncode != 0 or not os.path.exists(out):
            note = "压缩失败，使用原文件"
            break
        current = out
        size = os.path.getsize(current)
        if size <= limit:
            note = "已压缩以适配 10MB Base64 限制"
            break
        # still too big -> cut at the limit boundary
        ratio = limit / float(size)
        cut = max(1.0, dur * ratio * 0.92)
        cut_out = os.path.join(outdir, "%s_cut%d%s" % (stem, attempt,
                                                        ".mp4" if kind == "video" else ".mp3"))
        if kind == "video":
            r = ff.run(["-y", "-t", str(cut), "-i", current, "-c", "copy", cut_out])
        else:
            r = ff.run(["-y", "-t", str(cut), "-i", current, "-c", "copy", cut_out])
        if r.returncode != 0 or not os.path.exists(cut_out):
            break
        note = "已截断到约 %.1f 秒（%gMB 上限）" % (cut, limit_mb)
        current = cut_out
        truncated = cut
        return current, truncated, note
    return current, 0.0, note


def _omni(args: dict) -> str:
    src = (args.get("source") or args.get("file") or args.get("url") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not src or not prompt:
        return err("需要 'source' 与 'prompt'")
    if not _api_key():
        return err("缺 DASHSCOPE_API_KEY")
    model = (args.get("model") or config.cfg("multimedia.omni.model", "qwen3.8-omni-flash")).strip()
    max_mb = float(config.cfg("multimedia.omni.max_file_mb", 7.4))
    truncated = 0.0
    note = ""
    try:
        if os.path.exists(src):
            kind = _media_kind(src)
            work, truncated, note = _compress_to_limit(src, max_mb)
            data = _b64_data_uri(work)
            if kind == "video":
                media_part = {"type": "video_url", "video_url": {"url": data}}
            else:
                media_part = {"type": "input_audio",
                              "input_audio": {"data": data,
                                              "format": os.path.splitext(work)[1].lstrip(".") or "mp3"}}
        else:
            kind = "video" if args.get("kind") == "video" else (
                "video" if re.search(r"\.(mp4|mov|mkv|webm)(\?|$)", src, re.I) else "audio")
            if kind == "video":
                media_part = {"type": "video_url", "video_url": {"url": src}}
            else:
                fmt = (os.path.splitext(urllib.parse.urlparse(src).path)[1]
                       .lstrip(".").lower() or "wav")
                media_part = {"type": "input_audio",
                              "input_audio": {"data": src, "format": fmt}}
        payload = {"model": model,
                   "messages": [{"role": "user",
                                 "content": [media_part, {"type": "text", "text": prompt}]}],
                   "modalities": ["text"], "stream": False}
        d = _post_json_auth(_compat_url() + "/chat/completions", payload, timeout=900)
        choices = d.get("choices") or []
        msg = (choices[0].get("message") if choices else {}) or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        out = {"action": "analyze", "model": model, "kind": kind,
               "content": content, "usage": d.get("usage") or {},
               "reasoning": (msg.get("reasoning_content") or "")[:2000]}
        if note:
            out["note"] = note
            out["truncated_seconds"] = round(truncated, 1)
        return ok(out)
    except Exception as e:
        return err("omni: %s: %s" % (type(e).__name__, str(e)[:400]))


# ---------------------------------------------------------------------------
# schemas + registration
# ---------------------------------------------------------------------------

_TOOLS = [
    ("ffmpeg", _ffmpeg, {
        "action": {"type": "string",
                   "enum": ["probe", "convert", "trim", "mux", "extract_audio",
                            "extract_frames", "thumbnail", "burn_subtitle", "gif"],
                   "description": "probe=元数据; convert=转码; trim=裁剪; mux=音视频合并; extract_audio=提取音轨; extract_frames=抽帧; thumbnail=封面; burn_subtitle=烧字幕; gif=转GIF"},
        "input": {"type": "string", "description": "输入文件绝对路径"},
        "output": {"type": "string", "description": "输出路径（可选，默认 scratch）"},
        "video": {"type": "string", "description": "mux: 视频文件"},
        "audio": {"type": "string", "description": "mux: 音频文件"},
        "subtitle": {"type": "string", "description": "burn_subtitle: srt/ass 路径"},
        "start": {"type": "number", "description": "trim/gif: 起始秒"},
        "duration": {"type": "number", "description": "trim/gif: 时长秒"},
        "fps": {"type": "number", "description": "extract_frames/gif: 帧率"},
        "count": {"type": "integer", "description": "extract_frames: 帧数（fps 未给时）"},
        "time": {"type": "number", "description": "thumbnail: 取帧时间秒"},
        "format": {"type": "string", "description": "convert/extract_audio: 目标格式"},
        "width": {"type": "integer", "description": "gif: 宽度"},
    }, ["action"],
     "FFmpeg 封装：probe/convert/trim/mux/extract_audio/extract_frames/thumbnail/burn_subtitle/gif。返回输出路径与大小。"),
    ("dashscope_asr", _asr, {
        "action": {"type": "string", "enum": ["transcribe", "status", "result"],
                   "description": "transcribe=提交转写; status=查任务; result=取结果"},
        "source": {"type": "string", "description": "本地路径或 http(s) URL"},
        "task_id": {"type": "string", "description": "status/result: 任务 id"},
        "language": {"type": "string", "description": "zh/en/ja/...（可选）"},
        "timestamps": {"type": "boolean", "description": "要时间戳（走 filetrans 异步）"},
        "diarization": {"type": "boolean", "description": "说话人分离（异步）"},
        "wait_seconds": {"type": "integer", "description": "异步最多等多久，默认 300，上限 1800"},
    }, ["action"],
     "DashScope 语音转写：短音频同步（qwen3-asr-flash），长音频/要时间戳走 filetrans 异步；等待超时会返回 task_id 并在完成后 steer 回会话。"),
    ("dashscope_qwenomni_analysis", _omni, {
        "source": {"type": "string", "description": "本地音视频路径或 http(s) URL"},
        "prompt": {"type": "string", "description": "自定义分析提示词（必填）"},
        "model": {"type": "string", "description": "Qwen-Omni 模型名，默认 qwen3.8-omni-flash"},
        "kind": {"type": "string", "enum": ["audio", "video"], "description": "URL 来源时显式指定媒体类型"},
    }, ["source", "prompt"],
     "把音频/视频交给 Qwen-Omni 分析：支持自定义 prompt，返回完整分析文本。本地文件 >10MB 先压缩，仍超限则截到约 9.9MB 并明确告知截断。"),
]


def register(ctx) -> None:
    for name, handler, props, required, desc in _TOOLS:
        schema = {"name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": required}}
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, description=desc, emoji="🎬")