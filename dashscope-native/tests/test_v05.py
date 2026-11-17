"""dashscope-native v0.5 acceptance suite (PROMPT.md §8).

Run with the Hermes venv python; repo root added to sys.path so the REAL
core modules are exercised: agent.*_provider ABCs (isinstance gates),
agent.transports registry, providers.register_provider chain.
Fake ctx only emulates the settings facade (nested dict + dotted keys),
which mirrors hermes_cli.plugins_state._nested_plugin_value semantics.

Offline parts need no keys. Live probes (models catalog, MCP web) read
keys from Data/.env inside Python — never echoed, never on a command line.
"""
import importlib.util
import json
import os
import sys
import types

REPO = r"D:/01-AI/Hermes-Agent/Data/hermes-agent"
PLUGIN_DIR = r"D:/01-AI/Hermes-Agent/Data/plugins/custom-plugins/dashscope-native"
ENV_FILE = r"D:/01-AI/Hermes-Agent/Data/.env"
sys.path.insert(0, REPO)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[PASS] " if cond else "[FAIL] ") + name + ((" — " + detail) if detail and not cond else ""))


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "dashscope_native", os.path.join(PLUGIN_DIR, "__init__.py"),
        submodule_search_locations=[PLUGIN_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dashscope_native"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """Emulates ctx.get_config/set_config over a nested settings dict."""

    def __init__(self, settings=None):
        self.settings = settings if settings is not None else {}
        self.registered = {"tts": [], "stt": [], "image": [], "web": []}
        self.writes = []

    def _walk(self, key, default):
        cur = self.settings
        for seg in key.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                return default
            cur = cur[seg]
        return cur

    def get_config(self, key, default=None):
        return self._walk(key, default)

    def set_config(self, key, value):
        self.writes.append(key)
        segs = key.split(".")
        cur = self.settings
        for seg in segs[:-1]:
            cur = cur.setdefault(seg, {})
        cur[segs[-1]] = value

    def register_tts_provider(self, p):
        self.registered["tts"].append(p)

    def register_transcription_provider(self, p):
        self.registered["stt"].append(p)

    def register_image_gen_provider(self, p):
        self.registered["image"].append(p)

    def register_web_search_provider(self, p):
        self.registered["web"].append(p)


def names(ctx, kind):
    return sorted(p.name for p in ctx.registered[kind])


def main():
    pkg = load_plugin()
    from dashscope_native import ds_settings

    # -- ABC gates: real core base classes --------------------------------
    from agent.tts_provider import TTSProvider
    from agent.transcription_provider import TranscriptionProvider
    from agent.image_gen_provider import ImageGenProvider
    from agent.web_search_provider import WebSearchProvider

    # -- V1: full registration matrix, defaults all-on ---------------------
    ctx = FakeCtx()
    pkg.register(ctx)
    check("V1 tts names", names(ctx, "tts") == ["dashscope", "dashscope-tp"], str(names(ctx, "tts")))
    check("V1 stt names", names(ctx, "stt") == ["dashscope", "dashscope-tp"], str(names(ctx, "stt")))
    check("V1 image names", names(ctx, "image") == ["dashscope", "dashscope-tp"], str(names(ctx, "image")))
    check("V1 web names", names(ctx, "web") == ["dashscope"], str(names(ctx, "web")))
    for kind, base in (("tts", TTSProvider), ("stt", TranscriptionProvider),
                       ("image", ImageGenProvider), ("web", WebSearchProvider)):
        ok = all(isinstance(p, base) for p in ctx.registered[kind])
        check(f"V1 {kind} isinstance real core ABC", ok)

    # real transport registry
    from agent.transports import registered_api_modes
    modes = registered_api_modes()
    check("V1 api_mode 'dashscope' registered", "dashscope" in modes, str(modes))
    check("V8 old api_mode gone", "qwen_dashscope" not in modes)

    # real provider registry chain
    from providers import get_provider_profile
    prof = get_provider_profile("dashscope")
    check("V1 profile 'dashscope' resolves", prof is not None and prof.name == "dashscope",
          str(prof))
    check("V1 profile api_mode", prof is not None and prof.api_mode == "dashscope")
    for old in ("dashscope-native", "dashscope-native-api"):
        check(f"V8 old alias {old} not ours", (get_provider_profile(old) is None
              or getattr(get_provider_profile(old), "api_mode", None) != "dashscope"))

    # -- V4: seeding -------------------------------------------------------
    check("V4 seed wrote all default keys", set(ctx.writes) == set(ds_settings.DEFAULTS),
          str(set(ds_settings.DEFAULTS) - set(ctx.writes)))
    ctx2 = FakeCtx({"voice": {"tts": {"enabled": False, "model": ""}}})
    pkg.register(ctx2)
    check("V4 explicit false kept", ctx2.get_config("voice.tts.enabled") is False)
    check("V4 explicit empty kept", ctx2.get_config("voice.tts.model") == "")
    check("V4 absent key filled", ctx2.get_config("t2i.enabled") is True)

    # -- V3: switch matrix --------------------------------------------------
    def matrix(settings):
        c = FakeCtx(settings)
        pkg.register(c)
        return c

    c = matrix({"voice": {"tts": {"enabled": False}}})
    check("V3 tts metered off", names(c, "tts") == ["dashscope-tp"], str(names(c, "tts")))
    c = matrix({"voice": {"tts": {"enabled_tp": False}, "stt": {"enabled_tp": False}}})
    check("V3 voice tp off", names(c, "tts") == ["dashscope"] and names(c, "stt") == ["dashscope"])
    c = matrix({"t2i": {"enabled_tp": False}})
    check("V3 t2i tp off", names(c, "image") == ["dashscope"])
    c = matrix({"t2i": {"enabled": False, "enabled_tp": False}})
    check("V3 t2i both off", names(c, "image") == [])
    c = matrix({"web": {"enabled": False}})
    check("V3 web off", names(c, "web") == [])
    c = matrix({"model_api_mode": {"enabled": False}})
    check("V3 chat switch honored (no exception, other planes alive)",
          names(c, "image") == ["dashscope", "dashscope-tp"] and names(c, "web") == ["dashscope"])

    # -- lane_value: tp_ override + fallback --------------------------------
    g = lambda k, d=None: {"voice.tts.model": "qwen3-tts-flash",
                           "voice.tts.tp_model": "qwen-audio-3.0-tts-plus"}.get(k, d)
    check("lane metered reads bare", ds_settings.lane_value(g, "voice.tts.model", "x") == "qwen3-tts-flash")
    check("lane tp reads tp_", ds_settings.lane_value(g, "voice.tts.model", "x", tp=True) == "qwen-audio-3.0-tts-plus")
    g2 = lambda k, d=None: {"voice.stt.model": "qwen3-asr-flash"}.get(k, d)
    check("lane tp falls back to bare", ds_settings.lane_value(g2, "voice.stt.model", "x", tp=True) == "qwen3-asr-flash")

    # TP voice provider defaults come from tp_ twins
    tp_tts = next(p for p in ctx.registered["tts"] if p.name == "dashscope-tp")
    check("tp tts default_model from tp_model", tp_tts.default_model() == "qwen-audio-3.0-tts-plus",
          str(tp_tts.default_model()))
    check("tp tts default_voice from tp_voice", tp_tts.default_voice() == "longanxiaoxin",
          str(tp_tts.default_voice()))
    m_tts = next(p for p in ctx.registered["tts"] if p.name == "dashscope")
    check("metered tts default stays qwen3-tts-flash", m_tts.default_model() == "qwen3-tts-flash")

    # -- endpoint derivation -------------------------------------------------
    g3 = lambda k, d=None: {"base_url": "dashscope://cn-beijing"}.get(k, d)
    check("rest_host cn-beijing", ds_settings.rest_host(g3) == "https://dashscope.aliyuncs.com")
    g4 = lambda k, d=None: {"base_url": "dashscope://cn-beijing@llm-test123"}.get(k, d)
    check("rest_host workspace", ds_settings.rest_host(g4) == "https://llm-test123.cn-beijing.maas.aliyuncs.com")
    g5 = lambda k, d=None: {"base_url": "https://custom.example.com/"}.get(k, d)
    check("rest_host passthrough https", ds_settings.rest_host(g5) == "https://custom.example.com")

    # -- V2: SDK missing must not cascade ------------------------------------
    sdk_present = importlib.util.find_spec("dashscope") is not None
    if not sdk_present:
        check("V2 (natural) SDK absent: t2i+web still registered",
              names(ctx, "image") == ["dashscope", "dashscope-tp"] and names(ctx, "web") == ["dashscope"])
        check("V2 metered voice is_available False without SDK", not m_tts.is_available())
        check("V2 tp voice unaffected by SDK absence", tp_tts is not None)
    else:
        print("[SKIP] V2 natural-absence branch (SDK installed); simulated check below")
    # simulated: even with SDK present, t2i/web modules must not import it
    import ast
    for fname in ("ds_image.py", "ds_web.py"):
        tree = ast.parse(open(os.path.join(PLUGIN_DIR, fname), encoding="utf-8").read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imports.add(node.module.split(".")[0])
        check(f"V2 {fname} has no dashscope import", "dashscope" not in imports, str(imports))

    # -- V9: channel audit — no curl/subprocess/shell network paths ----------
    for fname in os.listdir(PLUGIN_DIR):
        if not fname.endswith(".py"):
            continue
        src = open(os.path.join(PLUGIN_DIR, fname), encoding="utf-8").read()
        tree = ast.parse(src)
        bad = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad.update(a.name for a in node.names if a.name.split(".")[0] in ("subprocess",))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in ("subprocess",):
                    bad.add(node.module)
        check(f"V9 {fname} no subprocess import", not bad, str(bad))
        # audit real CODE tokens only — comments/docstrings retelling the v1.1
        # shell-injection lesson legitimately contain the string "shell=True"
        import io
        import tokenize
        code_tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code_tokens.append(tok.string)
        joined = " ".join(code_tokens)
        check(f"V9 {fname} no shell=True in code",
              not ("shell" in code_tokens and "True" in code_tokens and "shell = True" in joined))

    # -- error envelope contracts (offline, injected failures) ---------------
    stt_metered = next(p for p in ctx.registered["stt"] if p.name == "dashscope")
    res = stt_metered.transcribe("Z:/nonexistent/audio.wav")
    check("STT never raises, error envelope", isinstance(res, dict) and res.get("success") is False)
    img = next(p for p in ctx.registered["image"] if p.name == "dashscope")
    r = img.generate("", "square")
    check("t2i empty prompt invalid_input", r.get("error_type") == "invalid_input")
    r = img.generate("cat", "square", image_url="http://x/y.png")
    check("t2i reference image rejected loudly", "NOT ready" in str(r.get("error", "")))
    web = ctx.registered["web"][0]
    check("web is_available no-network bool", isinstance(web.is_available(), bool))

    # -- live probes (keys read from .env in-process, never printed) ---------
    if os.environ.get("DSN_SKIP_LIVE") != "1" and os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        live_ctx = FakeCtx()
        pkg.register(live_ctx)
        for variant in ("dashscope", "dashscope-tp"):
            p = next(x for x in live_ctx.registered["image"] if x.name == variant)
            models = p.list_models()
            ids = [m["id"] for m in models]
            check(f"V6 live catalog {variant}: wan2.7-image present", "wan2.7-image" in ids, str(ids[:8]))
            check(f"V6 live catalog {variant}: no video models", not any("video" in i for i in ids), str(ids))
            check(f"V6 live catalog {variant}: no invented price", all(m.get("price", "") == "" for m in models))
            check(f"V6 live catalog {variant}: size sane", 2 <= len(ids) <= 60, str(len(ids)))
        # one real MCP search (cheap, metered)
        w = live_ctx.registered["web"][0]
        sr = w.search("阿里云百炼", limit=2)
        check("live web search success", sr.get("success") is True, str(sr)[:200])
        if sr.get("success"):
            rows = sr["data"]["web"]
            check("live web rows fenced F19", all(r["title"].startswith("[外部内容·不可信]") for r in rows))
            check("live web extract pure list", isinstance(w.extract([rows[0]["url"]]), list))

    print()
    print(f"TOTAL: {len(PASS)} pass, {len(FAIL)} fail")
    if FAIL:
        print("FAILED:", json.dumps(FAIL, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
