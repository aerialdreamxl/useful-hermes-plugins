"""Settings resolution for hermes-minimem v0.8.

A memory provider's ``ctx`` is the collector object, which forwards only ``register_*``
methods, so configuration is read directly from ``plugins.entries.hermes-minimem.settings``.
v0.8 replaces the old seed-once rule: every schema key genuinely absent from settings is
filled individually through Hermes' official schema-validating writer. Existing explicit
values, including ``""``, ``false`` and ``0``, are never overwritten.

Secrets may either name an environment variable or store a plaintext value in settings by
the owner's explicit v0.8 decision. Runtime reporting keeps only the source label.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from hermes_cli.plugins_settings import save_plugin_settings
except Exception:  # standalone import/test fallback; the official writer is used inside Hermes
    def save_plugin_settings(_plugin_id, _plugin_dir, _values):
        raise RuntimeError("Hermes plugin settings writer is unavailable")

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-minimem"
BACKEND_CHOICES = ("openai", "dashscope", "local")
OUTPUT_CHOICES = ("dense", "sparse", "dense&sparse")

# The schema defaults must stay identical to plugin.yaml's config_schema, because the
# Settings panel renders ``default`` and we fall back to it in code. v0.8 tests assert it.
DEFAULTS: Dict[str, Any] = {
    "embedding_backend": "openai",
    "embedding_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "embedding_model": "",
    "embedding_dim": 1024,
    "embedding_api_key": "",
    "embedding_api_key_env": "MINIMEM_EMBEDDING_API_KEY",
    "embedding_output_type": "dense",
    "sparse_mode": "bm25",
    "local_sparse_model": "",
    "local_cache_dir": "",
    "allow_pm_install": False,
    "package_index_url": "",
    "dashscope_workspace": "",
    "dashscope_region": "cn-beijing",
}
DASHSCOPE_REGIONS = (
    "cn-beijing", "ap-southeast-1", "cn-hongkong",
    "us-east-1", "eu-central-1", "ap-northeast-1",
)
# NOTE: there is deliberately no ``fusion_alpha`` default. A numeric default here would make
# ``_get("fusion_alpha", ...)`` always return a value, pinning alpha and silently disabling
# the per-query adaptation of §5.4. Absent = adaptive.

# Historical env fallbacks. A configured plaintext key or an explicitly named env wins first.
SECRET_ENV_ORDER = ("MINIMEM_EMBEDDING_API_KEY", "MNEMOSYNE_EMBEDDING_API_KEY", "DASHSCOPE_API_KEY")

_seed_lock = threading.Lock()
_seeded_in_process = False


def _hermes_home() -> Optional[str]:
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _dotenv_lookup(name: str) -> Optional[str]:
    """Minimal ``.env`` reader (KEY=VALUE, optional ``export``, optional quotes).

    Used when ``hermes_cli.config`` cannot be imported — the CLI entry point runs inside
    Hermes, but tests and standalone runs may not have the framework's dependencies.
    """
    path = os.path.join(_hermes_home() or "", ".env")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                if key != name:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    except OSError:
        return None
    return None

# Model -> allowed dims. Used for the API-backed dense models; local models use the table
# below. Unknown models skip the check and are validated by their returned vector width.
MODEL_DIMS: Dict[str, Tuple[int, ...]] = {
    "qwen3.7-text-embedding": (2560, 2048, 1536, 1024, 768, 512, 256),
    "qwen3.7-text-embedding-flash": (1024, 768, 512, 256),
    "text-embedding-v4": (2048, 1536, 1024, 768, 512, 256, 128, 64),
    "text-embedding-v3": (1024, 768, 512, 256, 128, 64),
}

# Documented fastembed choices. Unknown/custom local names remain allowed; the worker's
# returned vector width is checked against the stored index before use.
LOCAL_MODEL_DIMS: Dict[str, int] = {
    "BAAI/bge-base-en": 768,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-base-zh-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "BAAI/bge-large-zh-v1.5": 1024,
    "BAAI/bge-small-en": 384,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-small-zh-v1.5": 512,
    "Qdrant/clip-ViT-B-32-text": 512,
    "Qdrant/constella-nano": 1024,
    "Qdrant/constella-zero": 1024,
    "Qdrant/stella-en-400M-v5-doc-onnx": 1024,
    "ibm-granite/granite-embedding-small-english-r2": 384,
    "intfloat/multilingual-e5-large": 1024,
    "jinaai/jina-clip-v1": 768,
    "jinaai/jina-embeddings-v2-base-code": 768,
    "jinaai/jina-embeddings-v2-base-de": 768,
    "jinaai/jina-embeddings-v2-base-en": 768,
    "jinaai/jina-embeddings-v2-base-es": 768,
    "jinaai/jina-embeddings-v2-base-zh": 768,
    "jinaai/jina-embeddings-v2-small-en": 512,
    "jinaai/jina-embeddings-v3": 1024,
    "minishlab/potion-base-8M": 256,
    "minishlab/potion-multilingual-128M": 256,
    "minishlab/potion-retrieval-32M": 512,
    "mixedbread-ai/mxbai-embed-large-v1": 1024,
    "nomic-ai/nomic-embed-text-v1": 768,
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "nomic-ai/nomic-embed-text-v1.5-Q": 768,
    "Qwen/Qwen3-Embedding-0.6B": 1024,
    "Qwen/Qwen3-Embedding-0.6B-Q": 1024,
    "google/embeddinggemma-300m": 768,
    "google/siglip2-base-patch16-224": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 768,
    "snowflake/snowflake-arctic-embed-l": 1024,
    "snowflake/snowflake-arctic-embed-m": 768,
    "snowflake/snowflake-arctic-embed-m-long": 768,
    "snowflake/snowflake-arctic-embed-s": 384,
    "snowflake/snowflake-arctic-embed-xs": 384,
    "thenlper/gte-base": 768,
    "thenlper/gte-large": 1024,
    "thenlper/gte-small": 384,
}


BACKEND_MODEL_DEFAULTS = {
    "openai": "qwen3.7-text-embedding-flash",
    "dashscope": "qwen3.7-text-embedding",
    "local": "BAAI/bge-small-en-v1.5",
}


def _load_config() -> Dict[str, Any]:
    """Read the config through Hermes when available, with a standalone fallback."""
    try:
        from hermes_cli.config import load_config_readonly

        value = load_config_readonly() or {}
        if isinstance(value, dict):
            return value
    except Exception as exc:
        logger.debug("minimem: hermes_cli config read failed: %s", exc)
    home = _hermes_home()
    if not home:
        return {}
    try:
        import hermes_yaml

        cfg = hermes_yaml.load_yaml(Path(home, "config.yaml").read_text(encoding="utf-8-sig")) or {}
    except ImportError:
        try:
            import yaml

            with open(Path(home, "config.yaml"), "r", encoding="utf-8-sig") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception as exc:
            logger.warning("minimem: cannot read config.yaml (%s); using defaults", exc)
            return {}
    except Exception as exc:
        logger.warning("minimem: cannot read config.yaml (%s); using defaults", exc)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _entry_from_config(cfg: Any) -> Optional[Dict[str, Any]]:
    """``plugins.entries.<id>`` for this plugin, tolerating a nested install directory.

    Deliberately reads the plain dict instead of a private core helper: the shape of
    ``plugins.entries`` is the documented config surface and this plugin is standalone-testable.
    """
    try:
        entries = ((cfg or {}).get("plugins") or {}).get("entries") or {}
    except AttributeError:
        return None
    if not isinstance(entries, dict):
        return None
    entry = entries.get(PLUGIN_ID)
    if isinstance(entry, dict):
        return entry
    for candidate, value in entries.items():
        if isinstance(candidate, str) and candidate.endswith("/" + PLUGIN_ID) and isinstance(value, dict):
            return value
    return None


def _settings_from_config(cfg: Any) -> Dict[str, Any]:
    entry = _entry_from_config(cfg) or {}
    settings = entry.get("settings")
    return settings if isinstance(settings, dict) else {}


def _read_entry(cfg: Any, key: str) -> Any:
    entry = _entry_from_config(cfg)
    if not entry:
        return None
    for subtree in ("settings", "config"):  # "config" = pre-0.6 location
        node = entry.get(subtree)
        if isinstance(node, dict) and key in node:
            return node[key]
    return None


def _from_config_yaml(key: str) -> Any:
    return _read_entry(_load_config(), key)


def _env_value(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value

        value = get_env_value(name)
    except Exception:
        value = None
    value = value or os.environ.get(name) or _dotenv_lookup(name)
    return str(value).strip() if value else ""


def resolve_api_key(plaintext: str, env_name: str) -> Tuple[str, str]:
    """Resolve a key without returning its source path; the label never contains the value."""
    plain = (plaintext or "").strip()
    if plain:
        return plain, "plaintext"
    names = [env_name, *SECRET_ENV_ORDER]
    for name in dict.fromkeys(n for n in names if n):
        value = _env_value(name)
        if value:
            return value, f"env:{name}"
    return "", "missing"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _seed_value(key: str, raw: Any) -> Any:
    """Normalize legacy/default values so the official schema writer receives valid types."""
    if key == "embedding_dim":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return DEFAULTS[key]
    if key == "allow_pm_install":
        return _as_bool(raw)
    if key in {"embedding_backend", "embedding_output_type", "sparse_mode"}:
        normalized = str(raw or "").strip().lower()
        allowed = {
            "embedding_backend": BACKEND_CHOICES,
            "embedding_output_type": OUTPUT_CHOICES,
            "sparse_mode": ("bm25", "model", "off"),
        }[key]
        return normalized if normalized in allowed else DEFAULTS[key]
    return str(raw if raw is not None else DEFAULTS[key])


def ensure_defaults(ctx: Any = None, plugin_dir: Optional[Path] = None) -> None:
    """Seed only keys absent from settings, one official writer call per missing key."""
    global _seeded_in_process
    with _seed_lock:
        if _seeded_in_process:
            return
        directory = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent
        cfg = _load_config()
        settings = _settings_from_config(cfg)
        for key, default in DEFAULTS.items():
            if key in settings:
                continue
            legacy = _read_entry(cfg, key)
            value = _seed_value(key, default if legacy is None else legacy)
            try:
                save_plugin_settings(PLUGIN_ID, directory, {key: value})
                settings[key] = value
            except Exception as exc:
                # A refused write must not break the session: use the resolved value in this
                # process and try again after the next startup.
                logger.warning("minimem: could not seed setting %s (%s); using it in memory", key, exc)
        _seeded_in_process = True


def reset_seed_guard_for_tests() -> None:
    global _seeded_in_process
    _seeded_in_process = False


def _get(key: str, ctx: Any = None, default: Any = None) -> Any:
    """ctx first (for a real PluginContext), direct settings next, schema default last."""
    if ctx is not None:
        try:
            value = ctx.get_config(key, None)
            if value is not None:
                return value
        except (AttributeError, TypeError):
            pass  # _ProviderCollector: not a real PluginContext (the documented trap)
        except Exception as exc:
            logger.debug("minimem: ctx.get_config(%s) failed: %s", key, exc)
    value = _from_config_yaml(key)
    if value is not None:
        return value
    if default is not None:
        return default
    return DEFAULTS.get(key)


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    dim: int
    sparse_mode: str
    api_key: Optional[str]
    alpha_fixed: Optional[float] = None
    backend: str = "openai"
    api_key_source: str = "missing"
    output_type: str = "dense"
    local_sparse_model: str = ""
    local_cache_dir: str = ""
    allow_pm_install: bool = False
    package_index_url: str = ""
    instruct: str = ""
    dashscope_workspace: str = ""
    dashscope_region: str = "cn-beijing"

    @property
    def dims_known(self) -> bool:
        if self.backend == "local":
            return self.model in LOCAL_MODEL_DIMS
        return self.model in MODEL_DIMS

    @property
    def wants_dense(self) -> bool:
        return self.output_type in ("dense", "dense&sparse")

    @property
    def wants_model_sparse(self) -> bool:
        return self.output_type in ("sparse", "dense&sparse")

    @property
    def sparse_model(self) -> str:
        return self.local_sparse_model if self.backend == "local" else self.model

    @property
    def uses_model_sparse(self) -> bool:
        return self.wants_model_sparse and self.sparse_mode == "model"

    @property
    def effective_sparse_mode(self) -> str:
        if self.sparse_mode == "off":
            return "off"
        if self.sparse_mode == "model":
            return "model"
        return "bm25"

    def validate(self) -> List[str]:
        """Human-readable problems; empty means usable. Never mutates settings."""
        problems: List[str] = []
        if self.backend not in BACKEND_CHOICES:
            problems.append(f"embedding_backend {self.backend!r} must be openai | dashscope | local")
            return problems
        if self.output_type not in OUTPUT_CHOICES:
            problems.append(f"embedding_output_type {self.output_type!r} must be dense | sparse | dense&sparse")
        if self.sparse_mode not in ("bm25", "model", "off"):
            problems.append(f"sparse_mode {self.sparse_mode!r} must be bm25 | model | off")

        if self.wants_model_sparse and self.sparse_mode != "model":
            problems.append("sparse or dense&sparse output requires sparse_mode=model")
        if not self.wants_model_sparse and self.sparse_mode == "model":
            problems.append("sparse_mode=model requires sparse or dense&sparse output")

        if self.backend == "openai":
            if self.output_type != "dense":
                problems.append("OpenAI-compatible /v1/embeddings supports dense output only")
            if not self.api_key:
                problems.append(f"no embedding API key for openai backend ({' or '.join(SECRET_ENV_ORDER[:2])})")
            if self.dim <= 0:
                problems.append("embedding_dim must be positive for the openai backend")
            elif self.model in MODEL_DIMS and self.dim not in MODEL_DIMS[self.model]:
                problems.append(
                    f"dim {self.dim} is not supported by {self.model} (allowed: {list(MODEL_DIMS[self.model])})"
                )
        elif self.backend == "dashscope":
            if not self.api_key:
                problems.append(f"no embedding API key for dashscope backend ({' or '.join(SECRET_ENV_ORDER[:2])})")
            if self.dashscope_region not in DASHSCOPE_REGIONS:
                problems.append(
                    f"dashscope_region '{self.dashscope_region}' is not one of {list(DASHSCOPE_REGIONS)}"
                )
            if self.dim <= 0:
                problems.append("embedding_dim must be positive for the dashscope backend")
            elif self.model in MODEL_DIMS and self.dim not in MODEL_DIMS[self.model]:
                problems.append(
                    f"dim {self.dim} is not supported by {self.model} (allowed: {list(MODEL_DIMS[self.model])})"
                )
        else:
            if self.wants_model_sparse and not self.local_sparse_model.strip():
                problems.append("local sparse output requires local_sparse_model")
        if not self.base_url and self.backend == "openai":
            problems.append("embedding_base_url is empty")
        return problems


def load_settings(ctx: Any = None) -> Settings:
    """Resolve all settings once, at initialize()."""
    raw_backend = str(_get("embedding_backend", ctx) or "openai").strip().lower()
    backend = raw_backend if raw_backend in BACKEND_CHOICES else "openai"

    raw_dim = _get("embedding_dim", ctx, DEFAULTS["embedding_dim"])
    try:
        dim = int(str(raw_dim).strip())
    except (TypeError, ValueError):
        dim = int(DEFAULTS["embedding_dim"])

    raw_model = str(_get("embedding_model", ctx, DEFAULTS["embedding_model"]) or "").strip()
    model = raw_model or BACKEND_MODEL_DEFAULTS[backend]
    if backend == "local":
        dim = LOCAL_MODEL_DIMS.get(model, dim)

    raw_alpha = _get("fusion_alpha", ctx, None)
    alpha_fixed: Optional[float] = None
    if raw_alpha is not None:
        try:
            alpha_fixed = min(0.9, max(0.1, float(raw_alpha)))
        except (TypeError, ValueError):
            alpha_fixed = None

    plaintext_key = str(_get("embedding_api_key", ctx, DEFAULTS["embedding_api_key"]) or "")
    env_name = str(_get("embedding_api_key_env", ctx, DEFAULTS["embedding_api_key_env"]) or
                    DEFAULTS["embedding_api_key_env"]).strip()
    api_key, api_key_source = resolve_api_key(plaintext_key, env_name)

    base_url = str(_get("embedding_base_url", ctx, DEFAULTS["embedding_base_url"]) or "").strip()
    # Tolerate a pasted "/embeddings" suffix: the compatible client appends the path itself.
    if base_url.endswith("/embeddings"):
        base_url = base_url[: -len("/embeddings")]
    raw_output = str(_get("embedding_output_type", ctx, DEFAULTS["embedding_output_type"]) or
                     DEFAULTS["embedding_output_type"]).strip().lower()
    output_type = raw_output if raw_output in OUTPUT_CHOICES else "dense"

    return Settings(
        base_url=base_url.rstrip("/"),
        model=model,
        dim=dim,
        sparse_mode=str(_get("sparse_mode", ctx, DEFAULTS["sparse_mode"]) or DEFAULTS["sparse_mode"]).strip().lower(),
        api_key=api_key,
        alpha_fixed=alpha_fixed,
        backend=backend,
        api_key_source=api_key_source,
        output_type=output_type,
        local_sparse_model=str(_get("local_sparse_model", ctx, DEFAULTS["local_sparse_model"]) or "").strip(),
        local_cache_dir=str(_get("local_cache_dir", ctx, DEFAULTS["local_cache_dir"]) or "").strip(),
        allow_pm_install=_as_bool(_get("allow_pm_install", ctx, DEFAULTS["allow_pm_install"])),
        package_index_url=str(_get("package_index_url", ctx, DEFAULTS["package_index_url"]) or "").strip(),
        dashscope_workspace=str(_get("dashscope_workspace", ctx, DEFAULTS["dashscope_workspace"]) or "").strip(),
        dashscope_region=(str(_get("dashscope_region", ctx, DEFAULTS["dashscope_region"]) or
                               DEFAULTS["dashscope_region"]).strip().lower() or "cn-beijing"),
    )


def storage_path(hermes_home: str) -> str:
    """``$HERMES_HOME/hermes-minimem/minimem.db`` (profile-scoped, never hardcoded)."""
    base = os.path.join(hermes_home, PLUGIN_ID)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "minimem.db")
