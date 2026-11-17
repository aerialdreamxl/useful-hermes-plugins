# plugins/image_gen/my-backend/__init__.py
from typing import Any, Dict, List, Optional
import os

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)


class BailianImageGenProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        # Stable id used in image_gen.provider config. Lowercase, no spaces.
        return "t2i-bailian"

    @property
    def display_name(self) -> str:
        # Human label shown in `hermes tools`. Defaults to name.title() if omitted.
        return "Aliyun Bailian API"

    def is_available(self) -> bool:
        # Return False if credentials or deps are missing.
        # The tool's availability gate calls this before dispatch.
        if not os.environ.get("DASHSCOPE_T2I_API_KEY") or not os.environ.get("DASHSCOPE_T2I_BASE_URL"):
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        # Catalog shown in `hermes tools` model picker.
        return [
            {
                "id": "wan2.7-image",
                "display": "Wan 2.7 Image",
                "speed": "~5s",
                "strengths": "Quick iteration",
                "price": "",
            },
            {
                "id": "wan2.7-image-pro",
                "display": "Wan 2.7 Image (Pro)",
                "speed": "~30s",
                "strengths": "Highest fidelity",
                "price": "",
            },
            {
                "id": "qwen-image-3.0-pro",
                "display": "Qwen Image 3 (Pro)",
                "speed": "~30s",
                "strengths": "Highest fidelity",
                "price": "",
            },
        ]

    def default_model(self) -> Optional[str]:
        return "wan2.7-image"

    def get_setup_schema(self) -> Dict[str, Any]:
        # Metadata for the `hermes tools` picker — keys to prompt for at setup.
        return {
            "name": "Aliyun Bailian",
            "badge": "pay as you go",        # optional; shown as a short tag in the picker
            "tag": "Aliyun Bailian",
            "env_vars": [
                {
                    "key": "DASHSCOPE_T2I_API_KEY",
                    "prompt": "Aliyun API key",
                    "url": "https://www.qianwenai.com/",
                },
                {
                    "key": "DASHSCOPE_T2I_BASE_URL",
                    "prompt": "Aliyun Base URL",
                    "url": "https://www.qianwenai.com/",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # Declare whether this backend supports image-to-image / editing.
        # The tool layer surfaces this in the dynamic schema so the model
        # knows when `image_url` is honored. Default (if you omit this) is
        # text-only: {"modalities": ["text"], "max_reference_images": 0}.{"modalities": ["text", "image"], "max_reference_images": 4}
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

        # Model selection precedence: env var → config → default. The helper
        # _resolve_model() in the built-in openai plugin is a good reference.
        model_id = kwargs.get("model") or self.default_model() or "wan2.7-image"
        result_img_url = None
        stdout_text = ""

        try:
            # 1.纯 Python POST（2026-11-16 修复：原实现把 prompt/model 直接替换进
            #    curl 命令串并 shell=True —— prompt 里一个撇号就是宿主任意命令执行，
            #    API key 也经命令行与异常文本双路外泄。改标准库直发 JSON。）
            import json
            import urllib.error
            import urllib.request

            if aspect_ratio=="landscape":
                size_text="1696*960"
            elif aspect_ratio=="portrait":
                size_text="960*1696"
            else:
                size_text="1280*1280"

            api_key=os.environ.get("DASHSCOPE_T2I_API_KEY") or ""
            base_url=(os.environ.get("DASHSCOPE_T2I_BASE_URL") or "").rstrip("/")
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
                return error_response(
                    error=f"HTTP {exc.code}: {detail or 'no body'}",
                    error_type="RuntimeError",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            # 2.用json库加载json，读取"output.choices[*].message.content[*].image"中的图片URL，样例服务器返回信息在"return-msg.txt"
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
                # raise RuntimeError(
                #     f"No image URL in backend response: {stdout_text[:500]!r}"
                # )
                return error_response(
                    error=f"No image URL in backend response: {stdout_text[:500]!r}",
                    error_type="RuntimeError",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                )

            # 3.把这个URL赋值给下面的result_img_url
            result_img_url = image_url_found

            image = result_img_url
            
            # ------

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
    """Plugin entry point — called once at load time."""
    ctx.register_image_gen_provider(BailianImageGenProvider())