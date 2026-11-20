"""v0.5.3 §6.5 reference-image/editing test block (PROMPT.md E1-E10).

Injected into tests/test_v05.py by exec() so the file stays reviewable;
every network path is frozen — zero upstream calls here.
"""
import json
import os
import urllib.error
import urllib.request


def run(storage_check, img, tmpdir):
    """storage_check(name, cond, detail) mirrors the suite's check()."""
    from dashscope_native import ds_image

    calls = []

    class _Resp:
        def __init__(self, body):
            self._b = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._b

    def ok_body(n_images=1):
        choices = []
        urls = [f"https://oss.example.com/img{i}.png?Expires=1" for i in range(n_images)]
        choices.append({"finish_reason": "stop",
                        "message": {"role": "assistant",
                                    "content": [{"image": u} for u in urls]}})
        return {"output": {"choices": choices},
                "usage": {"image_count": n_images}, "request_id": "frozen"}

    real_urlopen = urllib.request.urlopen

    def install(body=None, raises=None):
        def fake(request, timeout=None, **kw):
            payload = json.loads(request.data.decode("utf-8"))
            calls.append({"payload": payload, "timeout": timeout,
                          "url": request.full_url,
                          "auth": request.get_header("Authorization")})
            if raises is not None:
                raise raises
            return _Resp(body if body is not None else ok_body())
        urllib.request.urlopen = fake

    def uninstall():
        urllib.request.urlopen = real_urlopen

    def content(payload):
        return payload["input"]["messages"][0]["content"]

    def params(payload):
        return payload["parameters"]

    orig_sys = ds_image._system_image_model
    try:
        # ---- E1 capabilities follow the active model ------------------------
        for mid, exp_mods, exp_n in (
                ("qwen-image-3.0-pro", ["text", "image"], 3),
                ("wan2.7-image", ["text", "image"], 9),
                ("qwen-image-2.1-turbo", ["text", "image"], 10),
                ("qwen-image-2.0-pro-2026-06-22", ["text", "image"], 3),  # snapshot folds in
                ("z-image-turbo", ["text"], 0),
                ("brand-new-model-x", ["text"], 0)):  # unlisted -> fail-closed
            ds_image._system_image_model = lambda m=mid: m
            caps = img.capabilities()
            storage_check(f"E1 caps {mid}",
                          caps["modalities"] == exp_mods and caps["max_reference_images"] == exp_n, str(caps))

        # ---- E2 edit envelope: [image x N, text x 1] order ------------------
        ds_image._system_image_model = lambda: "wan2.7-image"
        install()
        calls.clear()
        r = img.generate("把图2的涂鸦喷在图1的车上", "landscape", model="wan2.7-image",
                         image_url="https://a/car.png",
                         reference_image_urls=["https://b/paint.png"])
        p = calls[-1]["payload"]
        c = content(p)
        storage_check("E2 envelope order images-then-text",
                      r.get("success") and [list(i)[0] for i in c] == ["image", "image", "text"]
                      and c[-1]["text"] == "把图2的涂鸦喷在图1的车上"
                      and len([m for m in p["input"]["messages"]]) == 1, str(c))
        storage_check("E2 same endpoint + auth header",
                      calls[-1]["url"].endswith("/api/v1/services/aigc/multimodal-generation/generation")
                      and str(calls[-1]["auth"]).startswith("Bearer"), calls[-1]["url"])

        # ---- E3 local file / failures ----------------------------------------
        png = os.path.join(tmpdir, "pic.png")
        open(png, "wb").write(b"\x89PNG\r\n\x1a\nfrozen")
        calls.clear()
        install()
        img.generate("make it sunset", "square", model="qwen-image-3.0-pro", image_url=png)
        uri = content(calls[-1]["payload"])[0]["image"]
        storage_check("E3 local png -> data URI", uri.startswith("data:image/png;base64,"), uri[:40])
        # file:// prefix
        calls.clear()
        img.generate("x", "square", model="qwen-image-3.0-pro", image_url="file:///" + png.replace(os.sep, "/"))
        storage_check("E3 file:// accepted",
                      content(calls[-1]["payload"])[0]["image"].startswith("data:image/png;base64,"))
        # nonexistent -> invalid_input, no network
        calls.clear()
        r = img.generate("x", "square", model="qwen-image-3.0-pro", image_url=png + ".nope")
        storage_check("E3 missing file fails loudly, zero net",
                      r.get("success") is False and r.get("error_type") == "invalid_input" and len(calls) == 0,
                      str(r)[:140])
        # non-image extension
        txt = os.path.join(tmpdir, "note.txt")
        open(txt, "w").write("hello")
        r = img.generate("x", "square", model="qwen-image-3.0-pro", image_url=txt)
        storage_check("E3 non-image MIME rejected",
                      r.get("success") is False and "recognised image" in str(r.get("error", "")), str(r)[:140])
        # oversized local file (cap 10MB for qwen family)
        big = os.path.join(tmpdir, "big.png")
        with open(big, "wb") as fh:
            fh.write(b"A" * 10_000_001)
        r = img.generate("x", "square", model="qwen-image-3.0-pro", image_url=big)
        storage_check("E3 oversized file rejected (no silent rescale)",
                      r.get("success") is False and "too large" in str(r.get("error", "")), str(r)[:140])
        # oversized data URI
        r = img.generate("x", "square", model="qwen-image-3.0-pro",
                         image_url="data:image/png;base64," + "A" * 10_000_001)
        storage_check("E3 oversized data URI rejected",
                      r.get("success") is False and "exceeds" in str(r.get("error", "")), str(r)[:120])

        # ---- E4 clamping ----------------------------------------------------
        calls.clear()
        img.generate("x", "square", model="wan2.7-image",
                     reference_image_urls=[f"https://i/{n}.png" for n in range(12)])
        got = [i for i in content(calls[-1]["payload"]) if "image" in i]
        storage_check("E4 wan2.7 clamps 12 -> 9", len(got) == 9, str(len(got)))
        calls.clear()
        img.generate("x", "square", model="qwen-image-3.0-pro",
                     reference_image_urls=[f"https://i/{n}.png" for n in range(5)])
        got = [i for i in content(calls[-1]["payload"]) if "image" in i]
        storage_check("E4 qwen-3.0 clamps 5 -> 3", len(got) == 3, str(len(got)))

        # ---- E5 text-only family: named refusal, zero network ---------------
        n0 = len(calls)
        r = img.generate("x", "square", model="z-image-turbo", image_url="https://a/b.png")
        storage_check("E5 z-image-turbo refusal is teaching copy",
                      r.get("success") is False and "hermes setup tools" in str(r.get("error", ""))
                      and len(calls) == n0, str(r)[:160])
        r = img.generate("x", "square", model="wan2.5-i2i-preview", image_url="https://a/b.png")
        storage_check("E5 legacy async id named in refusal",
                      r.get("success") is False and "老异步端点" in str(r.get("error", "")), str(r)[:160])

        # ---- E6 size policy --------------------------------------------------
        calls.clear()
        img.generate("x", "landscape", model="qwen-image-3.0-pro", image_url="https://a/b.png")
        storage_check("E6 qwen edit omits size (follows input)",
                      "size" not in params(calls[-1]["payload"]), str(params(calls[-1]["payload"])))
        calls.clear()
        img.generate("x", "landscape", model="wan2.7-image", image_url="https://a/b.png")
        storage_check("E6 wan edit keeps pixel map",
                      params(calls[-1]["payload"]).get("size") == "1696*960", str(params(calls[-1]["payload"])))
        calls.clear()
        img.generate("x", "portrait", model="wan2.7-image")
        storage_check("E6 t2i path regression: size kept",
                      params(calls[-1]["payload"]).get("size") == "960*1696", str(params(calls[-1]["payload"])))

        # ---- E7 optional kwargs ----------------------------------------------
        calls.clear()
        img.generate("x", "square", model="qwen-image-3.0-pro", image_url="https://a/b.png",
                     negative_prompt="blurry", seed=7, n=3)
        pp = params(calls[-1]["payload"])
        storage_check("E7 qwen-3.0 passes negative_prompt/seed/n",
                      pp.get("negative_prompt") == "blurry" and pp.get("seed") == 7 and pp.get("n") == 3, str(pp))
        calls.clear()
        img.generate("x", "square", model="qwen-image-2.1-pro", image_url="https://a/b.png",
                     negative_prompt="blurry")
        storage_check("E7 qwen-2.1 drops negative_prompt",
                      "negative_prompt" not in params(calls[-1]["payload"]), str(params(calls[-1]["payload"])))
        calls.clear()
        img.generate("x", "square", model="wan2.7-image", image_url="https://a/b.png",
                     negative_prompt="blurry")
        storage_check("E7 wan2.7 drops negative_prompt",
                      "negative_prompt" not in params(calls[-1]["payload"]), str(params(calls[-1]["payload"])))

        # ---- E7b multi-image full delivery ------------------------------------
        calls.clear()
        install(body={"output": {"choices": [
            {"message": {"content": [{"image": "https://o/1.png"}, {"image": "https://o/2.png"}]}},
            {"message": {"content": [{"image": "https://o/3.png"}, {"image": "https://o/4.png"}]}}]}})
        r = img.generate("x", "square", model="qwen-image-2.0-pro")
        storage_check("E7b 4 images: full list + first + count",
                      r.get("image") == "https://o/1.png" and r.get("images") ==
                      ["https://o/1.png", "https://o/2.png", "https://o/3.png", "https://o/4.png"]
                      and r.get("image_count") == 4, str(r)[:200])
        calls.clear()
        install(body=ok_body(1))
        r = img.generate("x", "square", model="wan2.7-image")
        storage_check("E7b single image shape constant",
                      isinstance(r.get("images"), list) and len(r.get("images") or []) == 1
                      and r["images"][0] == r.get("image") and r.get("image_count") == 1, str(r)[:200])
        # single choice x 2 content items (n=2 real shape)
        calls.clear()
        install(body={"output": {"choices": [
            {"message": {"content": [{"image": "https://o/a.png"}, {"image": "https://o/b.png"}]}}]}})
        r = img.generate("x", "square", model="qwen-image-2.0-pro", n=2)
        storage_check("E7b one choice two images both kept",
                      r.get("images") == ["https://o/a.png", "https://o/b.png"], str(r)[:160])

        # ---- E10 timeout semantics -------------------------------------------
        calls.clear()
        install(raises=TimeoutError())
        r = img.generate("x", "square", model="wan2.7-image", image_url="https://a/b.png")
        storage_check("E10 edit timeout: error_type=timeout, 240s, billed warning",
                      r.get("error_type") == "timeout" and calls[-1]["timeout"] == 240
                      and "billed server-side" in str(r.get("error", ""))
                      and "retry" in str(r.get("error", "")).lower(), str(r)[:180])
        n_after = len(calls)
        storage_check("E10 no auto-retry (single request)", n_after == 1, str(n_after))
        calls.clear()
        install(raises=urllib.error.URLError(TimeoutError()))
        r = img.generate("x", "square", model="wan2.7-image")
        storage_check("E10 t2i wrapped connect-timeout -> timeout envelope, 120s",
                      r.get("error_type") == "timeout" and calls[-1]["timeout"] == 120, str(r)[:160])

        # ---- E9 channel: everything went through the frozen urllib -----------
        storage_check("E9 all edit traffic on urllib (frozen capture)", len(calls) > 0)
    finally:
        urllib.request.urlopen = real_urlopen
        ds_image._system_image_model = orig_sys
