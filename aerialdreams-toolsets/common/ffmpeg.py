"""ffmpeg / ffprobe wrappers (system binaries on PATH, no Python deps)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import List, Optional

FFMPEG_ENV = "AERIALDREAMS_FFMPEG"


def ffmpeg_bin() -> str:
    return os.environ.get(FFMPEG_ENV) or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    probe = os.environ.get("AERIALDREAMS_FFPROBE") or shutil.which("ffprobe")
    if probe:
        return probe
    candidate = os.path.join(os.path.dirname(ffmpeg_bin()), "ffprobe.exe")
    return candidate if os.path.exists(candidate) else "ffprobe"


def have_ffmpeg() -> bool:
    return bool(shutil.which(ffmpeg_bin()) or os.path.exists(ffmpeg_bin()))


def run(args: List[str], timeout: int = 600, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_bin()] + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=capture, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def run_probe(args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = [ffprobe_bin()] + [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def probe(path: str) -> dict:
    r = run_probe(["-v", "error", "-print_format", "json", "-show_format",
                   "-show_streams", path])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[:400])
    return json.loads((r.stdout or b"{}").decode("utf-8", "replace"))


def duration_seconds(path: str) -> float:
    info = probe(path)
    try:
        return float((info.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def size_mb(path: str) -> float:
    try:
        return round(os.path.getsize(path) / 1048576.0, 2)
    except OSError:
        return 0.0


def has_audio_stream(path: str) -> bool:
    try:
        info = probe(path)
    except Exception:
        return False
    return any(s.get("codec_type") == "audio" for s in (info.get("streams") or []))


def has_video_stream(path: str) -> bool:
    try:
        info = probe(path)
    except Exception:
        return False
    return any(s.get("codec_type") == "video" for s in (info.get("streams") or []))


def last_error(r: subprocess.CompletedProcess) -> str:
    lines = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else ""