"""Embedding client for the Bailian (DashScope) OpenAI-compatible endpoint.

Only the compatible endpoint is used. Its sparse sibling is unavailable here: sending
``output_type=sparse`` returns HTTP 200 with ``embedding: null`` (silent failure), so
sparse retrieval is local BM25 by default (PROMPT.md 5.3, 7.5.3).

Standard library only (urllib) — the plugin's sole dependency is numpy, and that is for
the search matrix, not for HTTP.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Per-request batch caps (measured: the 11th row on v4 is a hard 400; flash accepts 20).
_BATCH_CAP = {
    "text-embedding-v4": 10,
    "text-embedding-v3": 10,
    "qwen3.7-text-embedding": 20,
    "qwen3.7-text-embedding-flash": 20,
}
_DEFAULT_BATCH = 10
_TIMEOUT = 20.0
_RETRY_DELAYS = (0.5, 1.5, 4.0)


class EmbeddingError(RuntimeError):
    """Raised when the endpoint cannot produce a vector after retries."""


class Embedder:
    """Stateless-per-call embedding client with a failure counter for tier downgrade.

    ``failures`` counts consecutive failed *calls*; the provider stops trying the API
    for the rest of the session after 3 (PROMPT.md 5.5, tier L2).
    """

    def __init__(self, base_url: str, model: str, dim: int, api_key: Optional[str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = int(dim)
        self.api_key = api_key
        self.failures = 0
        self.last_dim: Optional[int] = None  # what the service actually returned
        self._dim_warned = False
        self._batch = _BATCH_CAP.get(model, _DEFAULT_BATCH)

    @property
    def url(self) -> str:
        return f"{self.base_url}/embeddings"

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    # -- public ------------------------------------------------------------
    def embed(self, texts: Sequence[str], *, text_type: str = "document") -> List[List[float]]:
        """Embed ``texts`` (documents) or a single query.

        ``text_type`` is carried for symmetry with the native API; the compatible
        endpoint has no such parameter, so it only affects logging and caching intent.
        """
        out: List[List[float]] = []
        texts = list(texts)
        for start in range(0, len(texts), self._batch):
            chunk = texts[start : start + self._batch]
            out.extend(self._embed_chunk(chunk))
        return out

    def embed_one(self, text: str, *, text_type: str = "query") -> List[float]:
        result = self.embed([text], text_type=text_type)
        if not result:
            raise EmbeddingError("empty embedding response")
        return result[0]

    # -- internals ---------------------------------------------------------
    def _embed_chunk(self, chunk: List[str]) -> List[List[float]]:
        if not chunk:
            return []
        payload = {
            "model": self.model,
            "input": chunk,
            "encoding_format": "float",
        }
        # Measured: flash accepts `dimensions` although the docs claim only v3/v4 do.
        # Send it, then trust whatever comes back rather than failing the write.
        payload["dimensions"] = self.dim

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Optional[Exception] = None
        for attempt, delay in enumerate((0.0, *_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                data = self._post(body)
                vectors = self._parse(data, len(chunk))
                self.failures = 0
                return vectors
            except EmbeddingError as exc:
                last_error = exc
                # A 4xx that is not 429 will not fix itself; stop retrying.
                if getattr(exc, "retryable", True) is False:
                    break
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                last_error = exc
            except Exception as exc:  # malformed JSON etc.
                last_error = exc
        self.failures += 1
        raise EmbeddingError(f"embedding failed after retries: {last_error}") from last_error

    def _post(self, body: bytes) -> dict:
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "hermes-minimem/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            err = EmbeddingError(f"HTTP {exc.code}: {detail or exc.reason}")
            err.retryable = exc.code == 429 or exc.code >= 500  # type: ignore[attr-defined]
            raise err from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise EmbeddingError(f"non-JSON response: {raw[:200]!r}") from exc

    def _parse(self, data: dict, expected: int) -> List[List[float]]:
        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise EmbeddingError(f"no data in response: {json.dumps(data)[:300]}")
        items = sorted(items, key=lambda d: d.get("index", 0))
        vectors: List[List[float]] = []
        for item in items:
            vec = item.get("embedding")
            if vec is None:
                # The exact silent failure the sparse path hits on this endpoint.
                raise EmbeddingError(
                    "embedding is null (the compatible endpoint returns null for sparse output)"
                )
            vectors.append(vec)
        if len(vectors) != expected:
            logger.warning("minimem: asked for %d embeddings, got %d", expected, len(vectors))
        got_dim = len(vectors[0])
        self.last_dim = got_dim
        if got_dim != self.dim and not self._dim_warned:
            self._dim_warned = True
            logger.warning(
                "minimem: requested dim %d but %s returned %d — storing the actual dim; "
                "existing vectors of the old dim will be treated as stale",
                self.dim, self.model, got_dim,
            )
        return vectors

    def probe(self) -> Tuple[bool, str]:
        """One tiny call used by ``is_available()``-adjacent diagnostics (not by is_available,
        which must not touch the network)."""
        try:
            vec = self.embed_one("ping", text_type="query")
            return True, f"ok, dim={len(vec)}"
        except EmbeddingError as exc:
            return False, str(exc)

    def close(self) -> None:
        """Compatibility no-op; the HTTP client is stateless across calls."""


def create_embedder(settings, hermes_home: str):
    """Compatibility factory used by the provider and diagnostics scripts."""
    from .sidecar import create_embedder as backend_factory

    return backend_factory(settings, hermes_home)
