"""PM-backed sidecar clients for the DashScope SDK and local fastembed backends.

The heavy dependencies deliberately stay outside the shared Hermes venv. Hermes PM creates a
profile-owned isolated environment; a small persistent worker runs from that environment and
communicates over JSON/stdio. The worker keeps a loaded ONNX model resident across calls, so
recall does not pay model-load cost again.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Settings
from .embed import EmbeddingError
from .results import EmbeddingResult, coerce_embedding_result

logger = logging.getLogger(__name__)

DASHSCOPE_BATCH_CAPS = {
    "text-embedding-v4": 10,
    "text-embedding-v3": 10,
    # flash's docs say 20, but on real corpora 20 texts/request intermittently trips the
    # server's batching backend ("Receive batching backend response failed", HTTP 500):
    # same 20-text batch fails 3/3 at 20, passes 100% sliced at 10 (measured 2026-11-10).
    "qwen3.7-text-embedding": 10,
    "qwen3.7-text-embedding-flash": 10,
}
LOCAL_BATCH_CAP = 32
SECRET_KEYS = (
    "DASHSCOPE_API_KEY", "MNEMOSYNE_EMBEDDING_API_KEY", "MINIMEM_EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
)
# F6: the sidecar interpreter is a private PM environment that pip-installs third-party
# packages on demand (dashscope / fastembed). Handing it a copy of os.environ leaked every
# *other* provider credential the gateway holds (OPENAI_API_KEY and friends) to code taken
# from PyPI. Only this whitelist is forwarded; anything else must be passed explicitly.
_SIDECAR_BASE_ENV = (
    # process essentials: PATH is needed by some native wheels, SYSTEMROOT/WINDIR/COMSPEC
    # by the Windows CRT, PATHEXT by any subprocess the SDK spawns.
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "HOME", "TMPDIR", "LANG",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # per-user dirs: fastembed/huggingface resolve their cache under these.
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
    # Python runtime knobs. The worker runs with -I -X utf8=1, which already ignores the
    # PYTHON* variables, so these are belt-and-braces: UTF-8 stdout is what keeps the JSON
    # protocol from corrupting on a cp936/cp1252 console.
    "PYTHONUTF8", "PYTHONIOENCODING", "PYTHONPATH",
)
# Not secrets, and the local backend cannot download an ONNX model without them on a
# proxied/offline-CA machine. Deliberately a separate tuple so reviewers can see the
# distinction at a glance.
_SIDECAR_NETWORK_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HF_ENDPOINT",
)


def sidecar_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build the child environment: a whitelist, plus explicitly passed extras.

    Never starts from ``os.environ`` (F6). The embedding key does not travel through the
    environment at all — it is part of the JSON request (``worker_config``) — so a missing
    key here does not degrade the sidecar. ``extra`` values win over the whitelist, except
    for ``SECRET_KEYS``, which are stripped even then: an explicit dict is still not a
    licence to hand a provider key to the worker.
    """
    child = {key: os.environ[key] for key in _SIDECAR_BASE_ENV if key in os.environ}
    for key in _SIDECAR_NETWORK_ENV:
        if key in os.environ:
            child[key] = os.environ[key]
    if extra:
        child.update({str(k): str(v) for k, v in extra.items()})
        for key in SECRET_KEYS:
            child.pop(key, None)
    return child


def dashscope_native_base(base_url: str) -> Optional[str]:
    """Convert a compatible-mode URL to the native DashScope API root."""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        return None
    if url.endswith("/compatible-mode/v1"):
        return url[: -len("/compatible-mode/v1")] + "/api/v1"
    return url


@contextlib.contextmanager
def _temporary_index_url(url: str):
    old = os.environ.get("UV_INDEX_URL")
    try:
        if url:
            os.environ["UV_INDEX_URL"] = url
        yield
    finally:
        if old is None:
            os.environ.pop("UV_INDEX_URL", None)
        else:
            os.environ["UV_INDEX_URL"] = old


def backend_python(settings: Settings, root: Path, *, allow_install: bool, requirements: Sequence[str]) -> Optional[Path]:
    """Find the selected side environment, installing only when explicitly permitted."""
    from pm import ensure_environment, environment_python

    env_name = f"minimem-{settings.backend}"
    root = Path(root)
    try:
        found = environment_python(env_name, root=root)
    except Exception as exc:
        logger.debug("minimem: cannot read PM environment %s: %s", env_name, exc)
        found = None
    if found is not None and Path(found).is_file():
        return Path(found)
    if not allow_install:
        return None
    with _temporary_index_url(settings.package_index_url):
        ensure_environment(env_name, list(requirements), root=root, explicit=True, timeout=1800)
    found = environment_python(env_name, root=root)
    if found is None:
        raise EmbeddingError(f"PM 按需安装未返回可用解释器：{env_name}")
    return Path(found)


class SidecarProcess:
    """One-line JSON protocol over stdio with a reader queue and request timeout.

    Stray stdout lines (a library printing inside the worker) are ignored, not fatal:
    ``request`` drains until it sees its own request id, so a noise line never poisons
    the queue or discards a healthy, model-loaded worker.
    """

    def __init__(self, interpreter: Path, worker_path: Path, *, timeout: float = 60.0,
                 env: Optional[Dict[str, str]] = None,
                 secrets: Sequence[str] = ()) -> None:
        child_env = sidecar_env(env)
        child_env["PYTHONUTF8"] = "1"
        command = [str(interpreter), "-I", "-X", "utf8=1", str(worker_path)]
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        self._timeout = timeout
        self._responses: queue.Queue[Optional[dict]] = queue.Queue()
        self._stderr_tail: List[str] = []
        self._lock = threading.Lock()
        # The plaintext key lives in settings, not os.environ (v0.8 §13.2): scrub it too.
        self._secrets = tuple(s for s in (list(secrets) + [os.environ.get(k, "") for k in SECRET_KEYS]) if s)
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="minimem-sidecar-reader")
        self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True, name="minimem-sidecar-stderr")
        self._reader.start()
        self._stderr_reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    # Stray library noise on stdout: ignore it. The request side drains
                    # until its own id, so dropping the line here keeps the worker alive.
                    continue
                if isinstance(message, dict):
                    self._responses.put(message)
        except (OSError, ValueError):
            # close() may close the pipe under this thread during its bounded drain.
            pass
        finally:
            self._responses.put(None)

    def _stderr_loop(self) -> None:
        try:
            for line in self._proc.stderr:
                self._stderr_tail = (self._stderr_tail + [line.strip()])[-20:]
        except (OSError, ValueError):
            return

    def _sanitise(self, value: str) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text[:2000]

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request_id = uuid.uuid4().hex
        message = {"id": request_id, **payload}
        if self._proc.poll() is not None or self._proc.stdin is None:
            detail = " | ".join(self._sanitise(line) for line in self._stderr_tail[-5:])
            raise EmbeddingError(f"sidecar worker 已退出：{detail or 'no stderr'}")
        with self._lock:
            try:
                self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._kill()
                raise EmbeddingError(f"sidecar worker 无法接收请求：{exc}") from exc
            deadline = time.monotonic() + self._timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill()
                    raise EmbeddingError(f"sidecar worker timed out after {self._timeout:.1f}s")
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty as exc:
                    self._kill()
                    raise EmbeddingError(f"sidecar worker timed out after {self._timeout:.1f}s") from exc
                if response is None:
                    # Give the stderr reader a moment to drain the crash traceback, so the
                    # (sanitised) tail we report is actually informative.
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    drain = time.monotonic() + 0.5
                    while time.monotonic() < drain and not self._stderr_tail:
                        time.sleep(0.02)
                    detail = " | ".join(self._sanitise(line) for line in self._stderr_tail[-5:])
                    raise EmbeddingError(f"sidecar worker exited without a response：{detail or 'no stderr'}")
                if response.get("id") == request_id:
                    break
                # A response for an older request (e.g. one that timed out and was
                # discarded): drop it and keep waiting inside this request's deadline.
        if not response.get("ok"):
            raise EmbeddingError(self._sanitise(response.get("error") or "unknown sidecar worker error"))
        return response

    def _kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill()
        except Exception:
            pass
        self._final_drain()

    def _final_drain(self) -> None:
        """Bounded post-mortem: reap the child, let both readers hit EOF, close the pipes (N8).

        A bare ``kill()`` after the 2 s grace left two loose ends: the process was never
        reaped (a zombie, or on Windows an ONNX model dir still locked by a half-dead child)
        and the reader threads kept a pipe open. Everything here is best effort by design —
        ``close()`` runs from failure paths and must never raise.
        """
        try:
            self._proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            logger.debug("minimem: sidecar worker still alive 1s after kill")
        except Exception:
            pass
        for thread in (self._reader, self._stderr_reader):
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


class SidecarEmbedder:
    """Persistent, failure-counting client used by both sidecar backends."""

    def __init__(self, settings: Settings, hermes_home: str, *, process_factory=None) -> None:
        self.settings = settings
        self.backend = settings.backend
        self.model = settings.model
        self.dim = settings.dim
        self.last_dim: Optional[int] = None
        self.failures = 0
        self._home = Path(hermes_home)
        self._root = self._home / "hermes-minimem" / "environments"
        self._worker_path = Path(__file__).resolve().with_name("sidecar_worker.py")
        self._process = None
        self._interpreter: Optional[Path] = None
        self._request_lock = threading.Lock()
        self._process_factory = process_factory
        self._batch = (
            LOCAL_BATCH_CAP if settings.backend == "local"
            else DASHSCOPE_BATCH_CAPS.get(settings.model, 10)
        )

    def is_configured(self) -> bool:
        if self.settings.backend == "dashscope":
            return bool(self.settings.api_key)
        return self.settings.backend == "local"

    @property
    def requirements(self) -> List[str]:
        if self.settings.backend == "dashscope":
            return ["dashscope>=2.0.1,<3"]
        return ["fastembed>=0.4,<1"]

    def _native_base(self) -> Optional[str]:
        return dashscope_native_base(self.settings.base_url) if self.settings.backend == "dashscope" else None

    def worker_config(self) -> Dict[str, Any]:
        cache_dir = self.settings.local_cache_dir or str(self._home / "hermes-minimem" / "models")
        timeout = 60.0 if self.settings.backend == "dashscope" else 1800.0
        workspace = getattr(self.settings, "dashscope_workspace", "") or ""
        region = (getattr(self.settings, "dashscope_region", "") or "") if workspace else ""
        # SDK 2.x first-class routing: workspace+region -> {ws}.{region}.maas.aliyuncs.com.
        # Only fall back to a hand-built base_url when no workspace is configured.
        base_url = None if workspace else self._native_base()
        return {
            "backend": self.settings.backend,
            "model": self.settings.model,
            "base_url": base_url,
            "workspace": workspace or None,
            "region": region or None,
            "api_key": self.settings.api_key or "",
            "dimension": int(self.settings.dim or 0),
            "output_type": self.settings.output_type,
            "text_timeout": timeout,
            "local_sparse_model": self.settings.local_sparse_model,
            "local_cache_dir": cache_dir,
            "instruct": self.settings.instruct,
        }

    def _ensure_process(self):
        if self._process is not None:
            return self._process
        if not self.is_configured():
            raise EmbeddingError("DashScope backend 缺少 API Key")
        selected = backend_python(
            self.settings, self._root, allow_install=self.settings.allow_pm_install,
            requirements=self.requirements,
        )
        if selected is None:
            raise EmbeddingError(
                f"{self.backend} 后端依赖尚未安装；请在 Settings 开启「允许按需安装后端依赖」（PM 按需安装）"
            )
        self._interpreter = Path(selected)
        factory = self._process_factory or SidecarProcess
        timeout = 60.0 if self.settings.backend == "dashscope" else 1800.0
        secrets = [self.settings.api_key] if self.settings.api_key else []
        self._process = factory(self._interpreter, self._worker_path, timeout=timeout, secrets=secrets)
        return self._process

    def _request_chunk(self, texts: List[str], text_type: str) -> List[EmbeddingResult]:
        process = self._ensure_process()
        response = process.request({
            "op": "embed",
            "texts": texts,
            "text_type": text_type,
            "config": self.worker_config(),
        })
        raw = response.get("result")
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EmbeddingError(f"sidecar worker returned {len(raw) if isinstance(raw, list) else '?'} results for {len(texts)} texts")
        results = [coerce_embedding_result(item) for item in raw]
        dims = [len(item.dense) for item in results if item.dense is not None]
        self.last_dim = dims[0] if dims else self.last_dim
        self.failures = 0
        return results

    def embed(self, texts: Sequence[str], *, text_type: str = "document") -> List[EmbeddingResult]:
        inputs = [str(text) for text in texts]
        if not inputs:
            return []
        output: List[EmbeddingResult] = []
        with self._request_lock:
            for start in range(0, len(inputs), self._batch):
                chunk = inputs[start:start + self._batch]
                try:
                    output.extend(self._request_chunk(chunk, text_type))
                except Exception as exc:
                    self.failures += 1
                    self._discard_process()
                    if isinstance(exc, EmbeddingError):
                        raise
                    raise EmbeddingError(str(exc)) from exc
        return output

    def embed_one(self, text: str, *, text_type: str = "query") -> EmbeddingResult:
        result = self.embed([text], text_type=text_type)
        if not result:
            raise EmbeddingError("empty embedding response")
        return result[0]

    def _discard_process(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            try:
                process.close()
            except Exception:
                pass

    def probe(self) -> Tuple[bool, str]:
        try:
            result = self.embed_one("ping", text_type="query")
            return True, f"ok dim={len(result.dense or [])} sparse={len(result.sparse or [])}"
        except Exception as exc:
            return False, str(exc)

    def close(self) -> None:
        self._discard_process()


def create_embedder(settings: Settings, hermes_home: str):
    """Return the configured client. v0.7 remains the default OpenAI-compatible path."""
    if settings.backend == "openai":
        from .embed import Embedder

        return Embedder(settings.base_url, settings.model, settings.dim, settings.api_key)
    return SidecarEmbedder(settings, hermes_home)
