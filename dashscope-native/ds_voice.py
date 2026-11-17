"""Voice plane: DashScope native ASR/TTS providers (PROMPT.md §10).

Two official plugin seams, zero core patches:
  - ctx.register_tts_provider          → agent/tts_registry.py
  - ctx.register_transcription_provider → agent/transcription_registry.py

Anti-shell clause extended: everything goes through the official
``dashscope.Client.audio.*`` resources (tts.http / tts WS family /
asr.recognize_file). No hand-rolled WS/HTTP, no compatible-mode audio.

Rulings baked in (PROMPT.md §10.6): provider name "dashscope"; no live
streaming STT in v1 (OV3); factory defaults qwen3-tts-flash / Cherry
(OV4). TTS dual-family routing (§10.7): model name starting with "qwen"
→ client.audio.tts.http (documented qwen-tts route); anything else →
client.audio.tts WS family (cosyvoice route).
"""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from .ds_uri import parse_dashscope_uri
from . import ds_settings

from agent.tts_provider import TTSProvider
from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger("hermes.dashscope_native.voice")

PROVIDER_NAME = "dashscope"
MIN_SDK = (2, 0, 4)

# ---------------------------------------------------------------------------
# Settings: v0.5 shared tree (ds_settings.DEFAULTS). Bare keys = metered lane;
# TP lane overrides live in the tp_ twins and are read via ds_settings.lane_value.
# ---------------------------------------------------------------------------
DEFAULTS = ds_settings.DEFAULTS

# Official Qwen-TTS voice list snapshot (48 rows actually listed, help.aliyun.com
# /zh/model-studio/qwen-tts-voice-list, fetched 2026-11-11; marketing copy says
# "49+" but the page lists 48 — the page wins. Refresh by hand).
QWEN_TTS_VOICES = (
    "Cherry", "Serena", "Ethan", "Chelsie", "Momo", "Vivian", "Moon", "Maia",
    "Kai", "Nofish", "Bella", "Jennifer", "Ryan", "Katerina", "Aiden",
    "Eldric Sage", "Mia", "Mochi", "Bellona", "Vincent", "Bunny", "Neil",
    "Elias", "Arthur", "Nini", "Seren", "Pip", "Stella", "Bodega", "Sonrisa",
    "Alek", "Dolce", "Sohee", "Ono Anna", "Lenn", "Emilien", "Andre",
    "Radio Gol", "Jada", "Dylan", "Li", "Marcus", "Roy", "Peter", "Sunny",
    "Eric", "Rocky", "Kiki",
)

# Hermes output formats → service-side format parameter (OV1: the grounded
# PCM case is 24 kHz s16le mono per the official streaming-playback sample;
# the rest of the set is validated at real-fire, V6).
_FORMAT_MAP = {
    "mp3": "mp3",
    "wav": "wav",
    "ogg": "opus",
    "opus": "opus",
    "flac": "wav",  # closest lossless container
    "pcm": "pcm",
}
_EXT_FOR = {"mp3": ".mp3", "wav": ".wav", "opus": ".opus", "pcm": ".pcm"}

# File extension → Recognition container format (1.x wire vocabulary kept by
# the 2.0.4 SDK: pcm/wav/mp3/opus/aac/amr/speex; ogg rides as opus).
_ASR_FORMAT_MAP = {
    "wav": "wav", "mp3": "mp3", "pcm": "pcm",
    "opus": "opus", "ogg": "opus",
    "m4a": "aac", "aac": "aac",
    "amr": "amr", "speex": "speex", "spx": "speex",
}

_PCM_STREAM_RATE = 24000  # grounded: official qwen-tts streaming sample

SettingGetter = Callable[[str, Any], Any]
ClientFactory = Callable[[], Any]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _sdk_version_ok() -> bool:
    try:
        from importlib.metadata import version

        parts = (version("dashscope").split(".") + ["0", "0"])[:3]
        return tuple(int(p) for p in parts) >= MIN_SDK
    except Exception:
        return False


def _guided_key_error(env_name: str) -> RuntimeError:
    return RuntimeError(
        f"dashscope-native voice: no API key. Set {env_name} to your METERED "
        "(pay-as-you-go) Bailian key (Token Plan keys are out of scope per "
        "PROMPT.md O1), or point the top-level key_env at the right variable."
    )


def make_client_factory(get_setting: SettingGetter) -> ClientFactory:
    """Default factory: build a fresh official Client per call (cheap; picks
    up settings/env changes without a restart)."""

    def build() -> Any:
        if importlib.util.find_spec("dashscope") is None:
            raise RuntimeError(
                "dashscope-native voice: official SDK missing — install with "
                "`uv pip install \"dashscope>=2.0.4\"` inside the Hermes venv."
            )
        if not _sdk_version_ok():
            raise RuntimeError(
                "dashscope-native voice: dashscope SDK is 1.x or too old — "
                "upgrade with `uv pip install -U \"dashscope>=2.0.4\"`."
            )
        import dashscope

        base = str(get_setting("base_url", DEFAULTS["base_url"]) or "")
        ref = parse_dashscope_uri(base)  # malformed → loud DashScopeUriError
        assert ref is not None, "base_url must use the dashscope:// grammar"
        env_name = str(
            get_setting("key_env", DEFAULTS["key_env"]) or ""
        )
        api_key = os.environ.get(env_name, "")
        if not api_key:
            raise _guided_key_error(env_name)
        return dashscope.Client(
            api_key=api_key, region=ref.region, workspace=ref.workspace
        )

    return build


def _resolve(
    arg: Optional[str], get_setting: SettingGetter, key: str, default: str
) -> str:
    """Precedence: host call argument > plugin settings > built-in default."""
    if isinstance(arg, str) and arg.strip():
        return arg.strip()
    value = get_setting(key, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _is_qwen_family(model: str) -> bool:
    return model.lower().startswith("qwen")


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
class DashScopeTTS(TTSProvider):
    name = PROVIDER_NAME
    display_name = "DashScope (Bailian native)"

    def __init__(
        self, get_setting: SettingGetter, client_factory: Optional[ClientFactory] = None
    ) -> None:
        self._get = get_setting
        self._factory = client_factory or make_client_factory(get_setting)

    # -- catalog / availability -------------------------------------------
    def is_available(self) -> bool:
        """Local checks only (picker paints call this every frame)."""
        if importlib.util.find_spec("dashscope") is None:
            return False
        env_name = str(self._get("key_env", DEFAULTS["key_env"]) or "")
        return bool(env_name and os.environ.get(env_name))

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": "qwen3-tts-flash", "display": "Qwen3-TTS-Flash (non-realtime)"},
        ]

    def list_voices(self) -> List[Dict[str, Any]]:
        return [{"id": v} for v in QWEN_TTS_VOICES]

    def default_voice(self) -> Optional[str]:
        return str(self._get("voice.tts.voice", DEFAULTS["voice.tts.voice"]))

    def default_model(self) -> Optional[str]:
        return str(self._get("voice.tts.model", DEFAULTS["voice.tts.model"]))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "native",
            "tag": "Bailian metered key",
            "env_vars": [
                {
                    "key": "DASHSCOPE_API_KEY",
                    "prompt": "Bailian METERED (pay-as-you-go) API key",
                    "url": "https://help.aliyun.com/zh/model-studio/get-api-key",
                }
            ],
        }

    # -- streaming-voice opt-in -------------------------------------------
    @property
    def streams_pcm(self) -> bool:
        if not bool(self._get("voice.tts.stream_pcm", DEFAULTS["voice.tts.stream_pcm"])):
            return False
        return _is_qwen_family(self.default_model() or "")

    @property
    def stream_sample_rate(self) -> Optional[int]:
        # v0.5：可配采样率（voice.tts.sample_rate，官方流式样例 24000 为缺省）
        try:
            return int(self._get("voice.tts.sample_rate", _PCM_STREAM_RATE)) if self.streams_pcm else None
        except (TypeError, ValueError):
            return _PCM_STREAM_RATE if self.streams_pcm else None

    @property
    def voice_compatible(self) -> bool:
        return True  # gateway may ffmpeg-convert to Opus for voice bubbles

    # -- synthesis ----------------------------------------------------------
    def _map_format(self, requested: Optional[str]) -> str:
        fmt = str(requested or "mp3").strip().lower()
        return _FORMAT_MAP.get(fmt, "mp3")

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
        del extra  # unknown keys are ignored by contract
        resolved_model = _resolve(
            model, self._get, "voice.tts.model", DEFAULTS["voice.tts.model"]
        )
        resolved_voice = _resolve(
            voice, self._get, "voice.tts.voice", DEFAULTS["voice.tts.voice"]
        )
        service_format = self._map_format(format)
        client = self._factory()
        try:
            if _is_qwen_family(resolved_model):
                # speed has no named slot on the http family — ignored (ABC allows).
                audio = b"".join(
                    chunk.audio
                    for chunk in client.audio.tts.http.stream(
                        text,
                        model=resolved_model,
                        voice=resolved_voice,
                        format=service_format,
                    )
                    if chunk.audio
                )
            else:
                kwargs: Dict[str, Any] = {
                    "model": resolved_model,
                    "voice": resolved_voice,
                    "format": service_format,
                }
                if speed is not None:
                    kwargs["rate"] = speed
                audio = client.audio.tts.synthesize(text, **kwargs)
        except Exception as exc:  # SDK 把上游错误体拼进了文案（F21）→ 净化后再抛
            from .ds_client import scrub_exception_message

            scrub_exception_message(exc)
            raise
        if not audio:
            raise RuntimeError(
                "dashscope-native voice: synthesis returned no audio bytes "
                f"(model={resolved_model}, voice={resolved_voice})."
            )
        final_path = output_path
        wanted_ext = _EXT_FOR.get(service_format, ".mp3")
        if not final_path.lower().endswith(wanted_ext):
            final_path = str(Path(final_path).with_suffix(wanted_ext))
        Path(final_path).write_bytes(audio)
        return final_path

    def stream(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        format: str = "opus",
        **extra: Any,
    ) -> Iterator[bytes]:
        del extra
        resolved_model = _resolve(
            model, self._get, "voice.tts.model", DEFAULTS["voice.tts.model"]
        )
        resolved_voice = _resolve(
            voice, self._get, "voice.tts.voice", DEFAULTS["voice.tts.voice"]
        )
        client = self._factory()
        fmt = str(format or "opus").lower()
        try:
            if _is_qwen_family(resolved_model):
                kwargs: Dict[str, Any] = {"format": "pcm" if fmt == "pcm" else fmt}
                if fmt == "pcm":
                    kwargs["sample_rate"] = self.stream_sample_rate or _PCM_STREAM_RATE
                for chunk in client.audio.tts.http.stream(
                    text, model=resolved_model, voice=resolved_voice, **kwargs
                ):
                    if chunk.audio:
                        yield chunk.audio
            else:
                yield from client.audio.tts.stream(
                    text, model=resolved_model, voice=resolved_voice, format=fmt
                )
        except Exception as exc:  # 同上：流式路径的 SDK 错误文案也要净化（F21）
            from .ds_client import scrub_exception_message

            scrub_exception_message(exc)
            raise


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------
class DashScopeSTT(TranscriptionProvider):
    name = PROVIDER_NAME
    display_name = "DashScope (Bailian native)"

    def __init__(
        self, get_setting: SettingGetter, client_factory: Optional[ClientFactory] = None
    ) -> None:
        self._get = get_setting
        self._factory = client_factory or make_client_factory(get_setting)

    def is_available(self) -> bool:
        if importlib.util.find_spec("dashscope") is None:
            return False
        env_name = str(self._get("key_env", DEFAULTS["key_env"]) or "")
        return bool(env_name and os.environ.get(env_name))

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": "qwen3-asr-flash", "display": "Qwen3-ASR-Flash (file)"}]

    def default_model(self) -> Optional[str]:
        return str(self._get("voice.stt.model", DEFAULTS["voice.stt.model"]))

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "native",
            "tag": "Bailian metered key",
            "env_vars": [
                {
                    "key": "DASHSCOPE_API_KEY",
                    "prompt": "Bailian METERED (pay-as-you-go) API key",
                    "url": "https://help.aliyun.com/zh/model-studio/get-api-key",
                }
            ],
        }

    @property
    def streaming_capable(self) -> bool:
        return False  # OV3 ruling: live streaming STT is out of v1 scope

    @staticmethod
    def _format_for(file_path: str) -> str:
        suffix = Path(file_path).suffix.lower().lstrip(".")
        return _ASR_FORMAT_MAP.get(suffix, suffix or "wav")

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        del extra  # prompt/vocabulary hints: v2 (customization resources)
        try:
            resolved_model = _resolve(
                model, self._get, "voice.stt.model", DEFAULTS["voice.stt.model"]
            )
            client = self._factory()
            kwargs: Dict[str, Any] = {
                "model": resolved_model,
                "format": self._format_for(file_path),
            }
            if language:
                kwargs["parameters"] = {"language": language}
            finals: List[str] = []
            last_text = ""
            for event in client.audio.asr.recognize_file(file_path, **kwargs):
                output = event.get("output") if isinstance(event, dict) else None
                sentence = (output or {}).get("sentence") or {}
                text = sentence.get("text")
                if text:
                    last_text = str(text)
                    if sentence.get("end_time") is not None:
                        finals.append(str(text))
            transcript = "".join(finals) or last_text
            return {
                "success": True,
                "transcript": transcript,
                "provider": PROVIDER_NAME,
            }
        except Exception as exc:  # NEVER raise — error envelope by contract
            from .ds_client import sanitize_exception_text  # F21：错误体不可信

            logger.warning("dashscope-native voice: transcribe failed", exc_info=True)
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
    tts_on = bool(ctx.get_config("voice.tts.enabled", True))
    stt_on = bool(ctx.get_config("voice.stt.enabled", True))
    getter: SettingGetter = lambda k, d=None: ctx.get_config(k, d)  # noqa: E731
    if tts_on:
        ctx.register_tts_provider(DashScopeTTS(getter))
        logger.info("dashscope-native: TTS provider %r registered", PROVIDER_NAME)
    if stt_on:
        ctx.register_transcription_provider(DashScopeSTT(getter))
        logger.info("dashscope-native: STT provider %r registered", PROVIDER_NAME)
    if not (tts_on or stt_on):
        logger.debug("dashscope-native: voice plane idle (both switches off)")
