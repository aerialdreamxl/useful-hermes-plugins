"""t2i plane merged from t2i-bailian v1.1 (PROMPT.md §6.1).

One provider class, two construction parameter sets (metered / Token Plan) —
the TP variant is the old "repoint DASHSCOPE_T2I_* env vars at the token-plan
endpoint" trick promoted to the built-in t2i.enabled_tp switch (owner ruling
2026-11-17). HTTP channel is HARD REQUIREMENT: stdlib urllib direct JSON,
key only in the Authorization header, curl/subprocess/shell forbidden
(the v1.1 fix: prompt apostrophe + shell=True = host RCE).

Model catalog (owner ruling 2026-11-17, supersedes the per-variant fetch):
``t2i.models`` holds the METERED upstream's FULL image-model list, and BOTH
variants return that same list — TP users pick what their plan covers
themselves. ``t2i.discover_models`` (default true) decides whether the list
refreshes from {metered base}/compatible-mode/v1/models (probed 2026-11-17:
182 ids upstream); refreshed lists are persisted back into settings via the
official writer. False = use the stored list as-is, no network. The endpoint
only gives ids — display is prettified locally, strengths/price stay empty
(never invent). Failure falls back to the stored list; a fetch failure must
never break registration or the picker.
"""
from typing import Any, Callable, Dict, List, Optional
import os
import time

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    success_response,
)

from . import ds_settings

SettingGetter = Callable[[str, Any], Any]

# id filter for the live catalog (§5.3): image families only. wan3.0-video is
# a VIDEO model riding the same list — excluded by the -video suffix rule.
def _is_image_model(model_id: str) -> bool:
    mid = model_id.lower()
    if mid.endswith("-video") or "-video-" in mid:
        return (False)
    return ("image" in mid or mid.startswith("wan") or mid.startswith("z-image"))


def _prettify(model_id: str) -> str:
    # local display only: strip date suffixes, Title-Case words (no invention)
    import re
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model_id)
    return (" ".join(w.capitalize() for w in base.replace("-", " ").replace(".", " ").split()))


class BailianImageGenProvider(ImageGenProvider):
    def __init__(self, get_setting: SettingGetter, *, tp: bool = False,
                 set_setting: Optional[Callable[[str, Any], None]] = None) -> None:
        self._get = get_setting
        self._set = set_setting  # official writer for persisting refreshed t2i.models
        self._tp = tp
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_at: float = 0.0

    @property
    def name(self) -> str:
        # v0.5 命名统一：按量 dashscope / TP dashscope-tp（原 t2i-bailian 作废）
        return ("dashscope-tp" if self._tp else "dashscope")

    @property
    def display_name(self) -> str:
        return ("Aliyun Bailian API (Token Plan)" if self._tp else "Aliyun Bailian API")

    def _base_url(self) -> str:
        if self._tp:
            return (ds_settings.tp_host(self._get))
        return (ds_settings.rest_host(self._get))

    def _key_env(self) -> str:
        key = "tp_key_env" if self._tp else "key_env"
        return (str(self._get(key, ds_settings.DEFAULTS[key]) or ""))

    def is_available(self) -> bool:
        # cheap local check only — no network on picker paints
        env_name = self._key_env()
        return (bool(env_name and os.environ.get(env_name)))

    def _stored_models(self) -> List[Dict[str, Any]]:
        raw = self._get("t2i.models", [])
        if not isinstance(raw, list):
            return ([])
        out = []
        for item in raw:
            if isinstance(item, dict) and item.get("id"):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                # tolerate a hand-edited plain id list in settings
                out.append({"id": item.strip(), "display": _prettify(item.strip()), "strengths": "", "price": ""})
        return (out)

    def _fetch_live_models(self) -> Optional[List[Dict[str, Any]]]:
        # ALWAYS the metered upstream (owner ruling: one shared catalog, both
        # variants return it; TP users pick what their plan covers).
        import json
        import urllib.request

        env_name = str(self._get("key_env", ds_settings.DEFAULTS["key_env"]) or "")
        api_key = os.environ.get(env_name) or ""
        if not api_key:
            return (None)
        url = ds_settings.rest_host(self._get) + "/compatible-mode/v1/models"
        request = urllib.request.Request(
            url,
            headers={"Authorization": "Bearer " + api_key},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads((response.read() or b"").decode("utf-8", "replace"))
        data = body.get("data")
        if not isinstance(data, list):
            return (None)
        out = []
        for item in data:
            mid = str((item or {}).get("id") or "")
            if mid and _is_image_model(mid):
                out.append({"id": mid, "display": _prettify(mid), "strengths": "", "price": ""})
        return (out or None)

    def list_models(self) -> List[Dict[str, Any]]:
        stored = self._stored_models()
        if not bool(self._get("t2i.discover_models", True)):
            return (stored)  # frozen list, zero network (owner switch)
        try:
            ttl = float(self._get("t2i.models_ttl_s", ds_settings.DEFAULTS["t2i.models_ttl_s"]))
        except (TypeError, ValueError):
            ttl = 3600.0
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < ttl:
            return (self._cache)
        try:
            live = self._fetch_live_models()
            if live:
                self._cache = live
                self._cache_at = now
                if self._set is not None and live != stored:
                    try:
                        self._set("t2i.models", live)  # persist via official writer
                    except Exception:
                        pass  # persistence failure must not break the picker
                return (live)
        except Exception:
            pass  # fetch failure must never break the picker/registration
        if self._cache is not None:
            return (self._cache)  # last-good in-process value
        return (stored)

    def default_model(self) -> Optional[str]:
        return (str(self._get("t2i.model", ds_settings.DEFAULTS["t2i.model"]) or "wan2.7-image"))

    def get_setup_schema(self) -> Dict[str, Any]:
        env_key = self._key_env()
        return {
            "name": self.display_name,
            "badge": "token plan" if self._tp else "pay as you go",
            "tag": "Aliyun Bailian",
            "env_vars": [
                {
                    "key": env_key or ("DASHSCOPE_TOKEN_PLAN_API_KEY" if self._tp else "DASHSCOPE_API_KEY"),
                    "prompt": "Aliyun API key",
                    "url": "https://help.aliyun.com/zh/model-studio/get-api-key",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # text-only; reference images explicitly rejected below (v1.1 behavior kept)
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect_ratio = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required",
                error_type="invalid_input",
                provider=self.name,
                prompt="",
                aspect_ratio=aspect_ratio,
            )

        if image_url != None or reference_image_urls != None:
            return error_response(
                error="Reference image input is NOT ready for use",
                error_type="invalid_input",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        model_id = kwargs.get("model") or self.default_model() or "wan2.7-image"
        result_img_url = None
        stdout_text = ""

        try:
            # 纯 Python POST（继承 t2i v1.1 安全修复：curl 命令串 + shell=True 会让
            # prompt 里一个撇号变成宿主任意命令执行，API key 也经命令行外泄。
            # v0.5 重申：curl/subprocess/shell 全面禁用，只走 http 库直发 JSON。）
            import json
            import urllib.error
            import urllib.request

            if aspect_ratio=="landscape":
                size_text="1696*960"
            elif aspect_ratio=="portrait":
                size_text="960*1696"
            else:
                size_text="1280*1280"

            api_key=os.environ.get(self._key_env()) or ""
            base_url=self._base_url().rstrip("/")
            payload={
                "model": model_id,
                "input": {
                    "messages": [{"role": "user", "content": [{"text": prompt}]}]
                },
                "parameters": {"size": size_text},
            }
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request=urllib.request.Request(
                base_url + "/api/v1/services/aigc/multimodal-generation/generation",
                data=body,
                headers={
                    "Authorization": "Bearer " + api_key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    stdout_text=(response.read() or b"").decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                detail=""
                try:
                    detail=(exc.read() or b"").decode("utf-8", "replace")[:500]
                except Exception:
                    detail=""
                from .ds_client import sanitize_upstream_error_body  # F21：错误体不可信
                return error_response(
                    error=f"HTTP {exc.code}: {sanitize_upstream_error_body(detail or 'no body', 500)}",
                    error_type="RuntimeError",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            # 读取 output.choices[*].message.content[*].image 中的图片URL（v1.1 原逻辑）
            try:
                resp = json.loads(stdout_text)
            except json.JSONDecodeError as exc:
                return error_response(
                    error=str(exc),
                    error_type=type(exc).__name__,
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            image_url_found = None
            for choice in (resp.get("output") or {}).get("choices") or []:
                for item in (choice.get("message") or {}).get("content") or []:
                    if isinstance(item, dict) and item.get("image"):
                        image_url_found = str(item["image"])
                        break
                if image_url_found:
                    break
            if not image_url_found:
                from .ds_client import sanitize_upstream_error_body
                return error_response(
                    error="No image URL in backend response: " + sanitize_upstream_error_body(stdout_text, 500),
                    error_type="RuntimeError",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            result_img_url = image_url_found

            image = result_img_url

            return success_response(
                image=image,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                provider=self.name
            )
        except Exception as exc:
            return error_response(
                error=str(exc),
                error_type=type(exc).__name__,
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )


def register(ctx) -> None:
    """Register each enabled variant (PROMPT.md §1 matrix)."""
    getter: SettingGetter = lambda k, d=None: ctx.get_config(k, d)  # noqa: E731
    setter = lambda k, v: ctx.set_config(k, v)  # noqa: E731
    if bool(ctx.get_config("t2i.enabled", True)):
        ctx.register_image_gen_provider(BailianImageGenProvider(getter, tp=False, set_setting=setter))
    if bool(ctx.get_config("t2i.enabled_tp", True)):
        ctx.register_image_gen_provider(BailianImageGenProvider(getter, tp=True, set_setting=setter))
