"""Token Plan voice lane (PROMPT.md §11) — strictly per official docs.

Official sources (fetched 2026-11-11, platform.qianwenai.com):
  - docs/token-plan/best-practices/speech-synthesis
  - docs/token-plan/best-practices/speech-recognition
  - docs/developer-guides/speech/voice-list/qwen-audio-tts

This lane is plain documented REST on the Token Plan gateway
(token-plan.maas.qianwenaiapi.com), NOT the compatible-mode shell and NOT
the metered SDK lane in ds_voice.py. User ruling 2026-11-11: follow the
official docs verbatim; curl → Python HTTP library is the only permitted
substitution (httpx, already a Hermes/dashscope dependency).

Docs-established facts baked in:
  TTS: POST {host}/api/v1/services/audio/tts/SpeechSynthesizer
       body {"model", "input": {"text","voice","format","sample_rate"}}
       response output.audio.url (+ expires_at), usage.characters
       model qwen-audio-3.0-tts-plus, default voice longanhuan_v3.6,
       formats mp3/pcm/wav/opus, default sample_rate 22050
  ASR: POST {host}/api/v1/services/aigc/multimodal-generation/generation
       body {"model", "input": {"messages": [...input_audio...]},
             "parameters": {"format": <required>, "language_hints"?}}
       response output.text (+ output.sentence.*), usage.duration
       model qwen-audio-3.0-asr-flash, non-realtime, audio ≤ 5 min
  Key: sk-sp- Token Plan key; docs script reads TOKEN_PLAN_API_KEY first,
       then ANTHROPIC_AUTH_TOKEN — mirrored EXCEPT for the second step: an
       Anthropic-side token is not a Token Plan credential, so reusing it is
       opt-in (DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN=1) instead of implicit
       (F20: no silent cross-service credential reuse).
"""
from __future__ import annotations

import base64
import importlib.util
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.tts_provider import TTSProvider
from agent.transcription_provider import TranscriptionProvider

from . import ds_settings

logger = logging.getLogger("hermes.dashscope_native.voice_tp")

PROVIDER_NAME = "dashscope-tp"

# Documented constants (speech-synthesis / speech-recognition best-practices).
DEFAULT_HOST = "https://token-plan.cn-beijing.maas.aliyuncs.com"  # v0.5: owner template host
TTS_PATH = "/api/v1/services/audio/tts/SpeechSynthesizer"
ASR_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
TIMEOUT_S = 300  # docs script timeout
AUDIO_DOWNLOAD_MAX_BYTES = 50_000_000
AUDIO_DOWNLOAD_MAX_REDIRECTS = 3
FALLBACK_KEY_ENV = "ANTHROPIC_AUTH_TOKEN"  # docs script fallback env name
# F20：ANTHROPIC_AUTH_TOKEN 是 Anthropic 侧凭证，不是 Token Plan 凭证。
# 默认**不**拿它给 Token Plan 网关做 Bearer（凭证跨服务复用）；只有显式设
# DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN=1 才允许，且日志里会注明来源。
ALLOW_FALLBACK_KEY_ENV = "DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN"
_TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Settings: v0.5 shared tree (ds_settings.DEFAULTS). TP lane params read via
# ds_settings.lane_value(..., tp=True): tp_model/tp_voice/tp_sample_rate first,
# fall back to the bare metered key when the tp_ twin is absent (§4 ruling).
# ---------------------------------------------------------------------------
DEFAULTS = ds_settings.DEFAULTS

# qwen-audio-3.0-tts-plus system voices verified on the official voice-list
# page (2026-11-11). longanhuan_v3.6 is the best-practices doc's default
# pairing with this model (the flash section of the voice-list page also
# carries it — docs conflict noted in PROMPT.md; best-practices wins).
# 500+ base voices exist per-model via official Excel lists; not embedded.
TP_TTS_VOICES = ("longanhuan_v3.6", "longanlingxin", "longanlufeng")

# Hermes output format → docs-supported set {mp3, pcm, wav, opus}.
_FORMAT_MAP = {
    "mp3": "mp3",
    "wav": "wav",
    "ogg": "opus",
    "opus": "opus",
    "flac": "wav",  # closest lossless container
    "pcm": "pcm",
}
_EXT_FOR = {"mp3": ".mp3", "wav": ".wav", "opus": ".opus", "pcm": ".pcm"}

# MIME table verbatim from the official speech-recognition doc script.
_MIME_TYPES = {
    "aac": "audio/aac",
    "amr": "audio/amr",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "wav": "audio/wav",
    "webm": "audio/webm",
}

SettingGetter = Callable[[str, Any], Any]
Poster = Callable[[str, Dict[str, str], Dict[str, Any]], Dict[str, Any]]
Getter = Callable[[str], bytes]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _require_httpx() -> Any:
    if importlib.util.find_spec("httpx") is None:
        raise RuntimeError(
            "dashscope-native token-plan voice: httpx missing — it ships with "
            "Hermes/dashscope; reinstall the Hermes environment."
        )
    import httpx

    return httpx


def _allow_fallback_key() -> bool:
    """F20 开关：只有显式 DASHSCOPE_VOICE_ALLOW_ANTHROPIC_TOKEN=1 才复用
    Anthropic 凭证；缺省（未设/空/其它值）一律视为不允许。"""
    return str(os.environ.get(ALLOW_FALLBACK_KEY_ENV, "")).strip().lower() in _TRUTHY


def _key_envs(get_setting: SettingGetter) -> List[str]:
    """按既有优先级列出可用的 key 环境变量名（只列名，不读值、不外泄）。

    顺序：顶层 tp_key_env（默认 DASHSCOPE_TOKEN_PLAN_API_KEY）→
    ANTHROPIC_AUTH_TOKEN（仅在显式开关打开时追加）。把 tp_key_env 显式设成
    ANTHROPIC_AUTH_TOKEN 属于用户自己写明，不算隐式复用，照旧生效。
    """
    env_name = str(
        get_setting("tp_key_env", DEFAULTS["tp_key_env"])
        or ""
    ).strip()
    names: List[str] = [env_name] if env_name else []
    if _allow_fallback_key() and FALLBACK_KEY_ENV not in names:
        names.append(FALLBACK_KEY_ENV)
    return (names)


def _resolve_key(get_setting: SettingGetter) -> tuple:
    """返回 (key, 来源标签)。来源标签只写环境变量名与获取方式，绝不含 key 本身。

    优先级保持 docs 顺序；来源标签供日志/返回体注明（F20 要求可追溯）。
    """
    env_name = str(
        get_setting("tp_key_env", DEFAULTS["tp_key_env"])
        or ""
    ).strip()
    for name in _key_envs(get_setting):
        value = os.environ.get(name) or ""
        if not value:
            continue
        if name == env_name:
            return (value, f"env:{name}")
        return (value, f"env:{name} (explicit via {ALLOW_FALLBACK_KEY_ENV}=1)")
    raise RuntimeError(
        "dashscope-native token-plan voice: no Token Plan API key. Set "
        f"{env_name or 'DASHSCOPE_TOKEN_PLAN_API_KEY'} (sk-sp- key from the Token Plan "
        f"console). {FALLBACK_KEY_ENV} is NOT reused automatically — it is an "
        "Anthropic-side credential; set "
        f"{ALLOW_FALLBACK_KEY_ENV}=1 only if you deliberately share it."
    )


def _base_url(get_setting: SettingGetter) -> str:
    return ds_settings.tp_host(get_setting)


def make_poster(get_setting: SettingGetter) -> Poster:
    """Default poster: httpx POST, timeout 300s per docs, raises on !ok.

    F21：错误体是上游（不可信）文本，拼接前一律过 ds_client 的净化器——
    状态码与错误类型保留，正文截断 800 字符并打断模板标记。
    """

    def post(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
        from .ds_client import upstream_error_message  # 函数内 import：避免包内循环

        httpx = _require_httpx()
        resp = httpx.post(url, headers=headers, json=payload, timeout=TIMEOUT_S)
        if resp.status_code >= 400:
            raise RuntimeError(
                upstream_error_message(resp.status_code, resp.text, "TokenPlanHTTPError")
            )
        return resp.json()

    return post


def _check_audio_url_allowed(url: str) -> bool:
    """只允许 http(s) 且不落字面私网/环回/保留地址。"""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or "").strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return False
        host = parts.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            return False
        # 先按 IP 字面量判（含 IPv6：urlsplit 已剥括号）；解析不了的再看形态
        try:
            ip = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            # 数字形态（含 0x 十六进制/混写：inet_aton 系解析器会把它折成 IP，
            # ipaddress 不吃就无法验证——一律拒，不给 OS 解析器留后门）；
            # 带冒号的残 IPv6 同理拒绝。域名照常放行（可达性由上游网关判）。
            if re.fullmatch(r"[0-9.]+", host) or re.search(r"0x[0-9a-f]*", host) or ":" in host:
                return False
            return True
        return not (
            ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified
        )
    except Exception:
        return False


def _bounded_audio_download(url: str) -> bytes:
    httpx = _require_httpx()
    current = str(url or "").strip()
    for _ in range(AUDIO_DOWNLOAD_MAX_REDIRECTS + 1):
        if not _check_audio_url_allowed(current):
            raise RuntimeError("audio download URL rejected by scheme/host policy")
        resp = httpx.get(current, timeout=TIMEOUT_S, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location") or ""
            if not location:
                raise RuntimeError("audio download redirect has no location")
            from urllib.parse import urljoin
            current = urljoin(current, location)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"audio download failed: {resp.status_code}")
        content_length = resp.headers.get("content-length")
        try:
            if content_length and int(content_length) > AUDIO_DOWNLOAD_MAX_BYTES:
                raise RuntimeError("audio download exceeds size limit")
        except ValueError:
            pass
        data = resp.content
        if len(data) > AUDIO_DOWNLOAD_MAX_BYTES:
            raise RuntimeError("audio download exceeds size limit")
        return data
    raise RuntimeError("audio download exceeded redirect limit")


def make_getter() -> Getter:
    """Default downloader for the time-limited output.audio.url."""

    def get(url: str) -> bytes:
        return _bounded_audio_download(url)

    return get


def _resolve(
    arg: Optional[str], get_setting: SettingGetter, key: str, default: str
) -> str:
    """Precedence: host call argument > TP-lane setting (tp_X → X) > default."""
    if isinstance(arg, str) and arg.strip():
        return arg.strip()
    value = ds_settings.lane_value(get_setting, key, None, tp=True)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
class TokenPlanTTS(TTSProvider):
    name = PROVIDER_NAME
    display_name = "Qwen Token Plan (native)"

    def __init__(
        self,
        get_setting: SettingGetter,
        poster: Optional[Poster] = None,
        getter: Optional[Getter] = None,
    ) -> None:
        self._get = get_setting
        self._poster = poster or make_poster(get_setting)
        self._getter = getter or make_getter()

    # -- catalog / availability -------------------------------------------
    def is_available(self) -> bool:
        """Local checks only (picker paints call this every frame)."""
        if importlib.util.find_spec("httpx") is None:
            return False
        # 与 _resolve_key 同一份 env 名单：不允许复用时就不把 Anthropic
        # 凭证算成"可用"（否则 picker 点亮、真调用必失败）。
        return any(os.environ.get(n) for n in _key_envs(self._get))

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "qwen-audio-3.0-tts-plus",
                "display": "Qwen-Audio 3.0 TTS Plus (Token Plan)",
            }
        ]

    def list_voices(self) -> List[Dict[str, Any]]:
        return [{"id": v} for v in TP_TTS_VOICES]

    def default_voice(self) -> Optional[str]:
        return str(ds_settings.lane_value(self._get, "voice.tts.voice", DEFAULTS["voice.tts.voice"], tp=True))

    def default_model(self) -> Optional[str]:
        return str(ds_settings.lane_value(self._get, "voice.tts.model", DEFAULTS["voice.tts.model"], tp=True))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "token-plan",
            "tag": "Token Plan sk-sp- key",
            "env_vars": [
                {
                    "key": "DASHSCOPE_TOKEN_PLAN_API_KEY",
                    "prompt": "Token Plan API key (sk-sp-...)",
                    "url": "https://platform.qianwenai.com/docs/token-plan/best-practices/speech-synthesis",
                }
            ],
        }

    # -- streaming-voice opt-in -------------------------------------------
    @property
    def streams_pcm(self) -> bool:
        return False  # docs return a time-limited audio URL; no stream wire

    @property
    def stream_sample_rate(self) -> Optional[int]:
        return None

    @property
    def voice_compatible(self) -> bool:
        return True  # gateway may ffmpeg-convert to Opus for voice bubbles

    # -- synthesis ----------------------------------------------------------
    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        del extra  # unknown keys ignored by contract; speed has no doc slot
        resolved_model = _resolve(
            model, self._get, "voice.tts.model", DEFAULTS["voice.tts.model"]
        )
        resolved_voice = _resolve(
            voice, self._get, "voice.tts.voice", DEFAULTS["voice.tts.voice"]
        )
        fmt = str(format or "mp3").strip().lower()
        service_format = _FORMAT_MAP.get(fmt, "mp3")
        try:
            sample_rate = int(
                ds_settings.lane_value(
                    self._get,
                    "voice.tts.sample_rate",
                    DEFAULTS["voice.tts.sample_rate"],
                    tp=True,
                )
            )
        except (TypeError, ValueError):
            sample_rate = DEFAULTS["voice.tts.tp_sample_rate"]
        key, key_source = _resolve_key(self._get)
        logger.info(
            "dashscope-native token-plan voice: TTS bearer source %s", key_source
        )
        url = _base_url(self._get) + TTS_PATH
        payload = {
            "model": resolved_model,
            "input": {
                "text": text,
                "voice": resolved_voice,
                "format": service_format,
                "sample_rate": sample_rate,
            },
        }
        data = self._poster(
            url,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload,
        )
        audio_url = ((data.get("output") or {}).get("audio") or {}).get("url")
        if not audio_url:
            from .ds_client import sanitize_upstream_error_body  # F21：响应体同属不可信

            raise RuntimeError(
                "dashscope-native token-plan voice: response has no "
                "output.audio.url — got: "
                f"{sanitize_upstream_error_body(data, 300)}"
            )
        audio = self._getter(audio_url)  # URL expires — download immediately
        if not audio:
            raise RuntimeError(
                "dashscope-native token-plan voice: downloaded audio is empty."
            )
        final_path = output_path
        wanted_ext = _EXT_FOR.get(service_format, ".mp3")
        if not final_path.lower().endswith(wanted_ext):
            final_path = str(Path(final_path).with_suffix(wanted_ext))
        Path(final_path).write_bytes(audio)
        return final_path


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------
class TokenPlanSTT(TranscriptionProvider):
    name = PROVIDER_NAME
    display_name = "Qwen Token Plan (native)"

    def __init__(
        self, get_setting: SettingGetter, poster: Optional[Poster] = None
    ) -> None:
        self._get = get_setting
        self._poster = poster or make_poster(get_setting)

    def is_available(self) -> bool:
        if importlib.util.find_spec("httpx") is None:
            return False
        # 同 TTS：key 名单只有一份来源（_key_envs），F20 开关在这里也生效。
        return any(os.environ.get(n) for n in _key_envs(self._get))

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "qwen-audio-3.0-asr-flash",
                "display": "Qwen-Audio 3.0 ASR Flash (Token Plan, non-realtime)",
            }
        ]

    def default_model(self) -> Optional[str]:
        return str(ds_settings.lane_value(self._get, "voice.stt.model", DEFAULTS["voice.stt.model"], tp=True))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "token-plan",
            "tag": "Token Plan sk-sp- key",
            "env_vars": [
                {
                    "key": "DASHSCOPE_TOKEN_PLAN_API_KEY",
                    "prompt": "Token Plan API key (sk-sp-...)",
                    "url": "https://platform.qianwenai.com/docs/token-plan/best-practices/speech-recognition",
                }
            ],
        }

    @property
    def streaming_capable(self) -> bool:
        return False  # docs: Token Plan ASR is non-realtime only

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        del extra  # prompt/vocabulary hints: no doc slot on this lane
        try:
            resolved_model = _resolve(
                model, self._get, "voice.stt.model", DEFAULTS["voice.stt.model"]
            )
            source = str(file_path)
            if source.startswith(("http://", "https://")):
                audio_data = source
                audio_format = (
                    Path(source.split("?")[0]).suffix.lstrip(".").lower() or "mp3"
                )
            else:
                path = Path(source)
                audio_format = path.suffix.lstrip(".").lower()
                mime_type = _MIME_TYPES.get(audio_format, f"audio/{audio_format}")
                audio_data = (
                    f"data:{mime_type};base64,"
                    + base64.b64encode(path.read_bytes()).decode()
                )
            parameters: Dict[str, Any] = {"format": audio_format}  # docs: required
            if language:
                parameters["language_hints"] = [language]  # docs: up to 4
            payload = {
                "model": resolved_model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": audio_data},
                                }
                            ],
                        }
                    ]
                },
                "parameters": parameters,
            }
            key, key_source = _resolve_key(self._get)
            logger.info(
                "dashscope-native token-plan voice: ASR bearer source %s", key_source
            )
            url = _base_url(self._get) + ASR_PATH
            data = self._poster(
                url,
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                payload,
            )
            output = data.get("output") or {}
            transcript = output.get("text")
            if not transcript:
                # docs: sentence-level detail lives in output.sentence.*
                sentence = output.get("sentence")
                if isinstance(sentence, dict):
                    transcript = str(sentence.get("text") or "")
                elif isinstance(sentence, list):
                    transcript = "".join(
                        str(s.get("text") or "") for s in sentence if isinstance(s, dict)
                    )
            return {
                "success": True,
                "transcript": str(transcript or ""),
                "provider": PROVIDER_NAME,
                # F20：凭证来源可追溯（只写环境变量名，不含 key 本身）
                "auth_source": key_source,
            }
        except Exception as exc:  # NEVER raise — error envelope by contract
            from .ds_client import sanitize_exception_text  # F21：错误体不可信

            logger.warning(
                "dashscope-native token-plan voice: transcribe failed", exc_info=True
            )
            return {
                "success": False,
                "transcript": "",
                "provider": PROVIDER_NAME,
                "error": sanitize_exception_text(exc),
            }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(ctx: Any) -> None:
    # seeding happens once in ds_settings.seed(ctx) at plugin entry
    tts_on = bool(ctx.get_config("voice.tts.enabled_tp", True))
    stt_on = bool(ctx.get_config("voice.stt.enabled_tp", True))
    getter: SettingGetter = lambda k, d=None: ctx.get_config(k, d)  # noqa: E731
    if tts_on:
        ctx.register_tts_provider(TokenPlanTTS(getter))
        logger.info("dashscope-native: TTS provider %r registered", PROVIDER_NAME)
    if stt_on:
        ctx.register_transcription_provider(TokenPlanSTT(getter))
        logger.info("dashscope-native: STT provider %r registered", PROVIDER_NAME)
    if not (tts_on or stt_on):
        logger.debug("dashscope-native: token-plan voice lane idle (both switches off)")
