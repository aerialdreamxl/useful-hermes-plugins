"""The memory provider itself.

Contract points that are easy to get wrong (all confirmed against Hermes source):
  * ``plugin.yaml`` lives in ``$HERMES_HOME/plugins/hermes-minimem/`` — a FIRST-level
    directory. ``find_provider_dir`` only scans direct children of the plugins root and
    requires ``register_memory_provider`` or ``MemoryProvider`` to appear in ``__init__.py``.
  * Every store path is bound in ``initialize(hermes_home=...)`` and kept on the INSTANCE:
    one gateway process serves several profiles on different threads.
  * Background threads go through ``spawn_context_thread`` so they stay on this profile.
  * ``prefetch()`` reads a cache filled by a background worker; it never blocks on HTTP.
  * Memory is an enhancement: nothing here may raise into the conversation turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

from . import bm25 as bm25_mod
from . import config as config_mod
from . import rank
from .config import Settings, load_settings, storage_path
from .embed import Embedder, EmbeddingError, create_embedder
from .results import EmbeddingResult, coerce_embedding_result, sparse_dot
from .store import Store, new_id, pack_vector, unpack_vector, MAX_TEXT

logger = logging.getLogger(__name__)


def _embed_resilient(embedder, texts, *, min_batch=1, _state=None):
    """Embed a batch; on backend failure split in half and retry, depth-first.

    DashScope's batching backend 500s on some whole batches (2026-11-10: same 20-text
    batch failed 3/3 at 20, sliced at 10 passed every time). A batch-level failure is
    therefore often positional, not content-level: halving isolates it and rescues the
    rest. Two consecutive leaf-split failures (no success in between) mark the backend
    as dead for this run, bounding an outage to a handful of probes instead of one call
    per row. Returns a list matching ``texts`` with None where every attempted split
    failed.
    """
    if _state is None:
        _state = {"consecutive": 0, "dead": False}
    if _state["dead"]:
        return [None] * len(texts)
    try:
        out = list(embedder.embed(texts, text_type="document"))
        _state["consecutive"] = 0
        return out
    except Exception:  # noqa: BLE001 - any batch failure triggers the split path
        if len(texts) <= max(1, min_batch):
            _state["consecutive"] += 1
            _state["dead"] = _state["consecutive"] >= 2
            return [None] * len(texts)
        mid = len(texts) // 2
        return (_embed_resilient(embedder, texts[:mid], min_batch=min_batch, _state=_state)
                + _embed_resilient(embedder, texts[mid:], min_batch=min_batch, _state=_state))

TOOL_NAMES = (
    "minimem_remember",
    "minimem_recall",
    "minimem_update",
    "minimem_forget",
    "minimem_stats",
)

KINDS = ("preference", "fact", "insight", "identity", "task", "relation")
# Built-in memory target -> minimem kind. ``user`` reads as identity/preference, ``memory``
# as environment facts; the distinction only drives the stored enum, never retrieval.
_KIND_BY_TARGET = {"user": "preference", "memory": "fact"}

_WRITE_DEBOUNCE_S = 5.0
_WRITE_BATCH = 20
_MIRROR_DEDUPE_S = 30.0
# Built-in memory files separate entries with a lone "§" line.
_ENTRY_SEP_RE = re.compile(r"^\s*§\s*$", re.MULTILINE)
_FAILURES_BEFORE_L2 = 3
# Raw-score relevance gate (applied before min-max, which always yields a top score of 1.0).
DENSE_MIN_COS = 0.25
SPARSE_MIN_RAW = 1.5
MODEL_SPARSE_MIN_RAW = 1e-9


class _ModelSparseIndex:
    """Inverted index over learned/semantic sparse vectors.

    BM25 remains a separate lexical channel. This index scores ``sum(q_value * d_value)``
    over shared vocabulary indices, so a row with no shared term is a true zero rather than
    an artifact of min-max normalisation.
    """

    def __init__(self) -> None:
        self.ids: List[str] = []
        self.postings: Dict[int, Dict[int, float]] = {}
        self.id_row: Dict[str, int] = {}
        self._row_terms: List[List[int]] = []

    def build(self, docs: Sequence[Tuple[str, Sequence[Tuple[int, float]]]]) -> None:
        self.ids = []
        self.postings = {}
        self.id_row = {}
        self._row_terms = []
        for mem_id, pairs in docs:
            self._append(mem_id, pairs)

    def _append(self, mem_id: str, pairs: Sequence[Tuple[int, float]]) -> None:
        if mem_id in self.id_row:
            self.remove(mem_id)
        row = len(self.ids)
        self.ids.append(mem_id)
        self.id_row[mem_id] = row
        terms: List[int] = []
        for raw_idx, raw_value in pairs:
            idx, value = int(raw_idx), float(raw_value)
            if not terms:
                terms.append(idx)
            elif terms[-1] != idx:
                terms.append(idx)
            self.postings.setdefault(idx, {})[row] = value
        self._row_terms.append(terms)

    def append(self, mem_id: str, pairs: Sequence[Tuple[int, float]]) -> None:
        self._append(mem_id, list(pairs))

    def remove(self, mem_id: str) -> bool:
        """Swap-remove: move the last row into the hole, then pop the tail.

        Clearing only the postings left a tombstone — ``ids``/``_row_terms`` kept growing
        for the whole process, and a long-lived provider that replaces entries (every
        ``append`` of an existing id removes first) slowly leaked rows no id could reach.
        Same shape as ``_VectorMatrix.remove``; the extra work here is that postings are
        keyed by *row*, so every term of the moved row must be re-keyed from ``last`` to
        ``row`` — forgetting that is how a search starts returning the wrong entry.
        """
        row = self.id_row.pop(mem_id, None)
        if row is None:
            return False
        last = len(self.ids) - 1
        for term in self._row_terms[row]:
            bucket = self.postings.get(term)
            if bucket is not None:
                bucket.pop(row, None)
                if not bucket:
                    del self.postings[term]
        if row != last:
            moved_id = self.ids[last]
            moved_terms = self._row_terms[last]
            self.ids[row] = moved_id
            self._row_terms[row] = moved_terms
            self.id_row[moved_id] = row
            for term in moved_terms:
                bucket = self.postings.get(term)
                if bucket is None:
                    continue
                value = bucket.pop(last, None)
                if value is not None:
                    bucket[row] = value
                if not bucket:
                    del self.postings[term]
        self.ids.pop()
        self._row_terms.pop()
        return True

    def search(self, query: Sequence[Tuple[int, float]], topk: int) -> Dict[int, float]:
        scores: Dict[int, float] = {}
        if not self.ids or not query:
            return {}
        for idx, q_value in query:
            posting = self.postings.get(int(idx))
            if not posting:
                continue
            for row, d_value in posting.items():
                scores[row] = scores.get(row, 0.0) + float(q_value) * d_value
        if not scores or len(scores) <= topk:
            return scores
        return dict(sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:topk])

    @property
    def count(self) -> int:
        return len(self.ids)



class _VectorMatrix:
    """Append-mostly float32 matrix with an id<->row map.

    Rebuilding the whole matrix on every insert would cost 80 ms at 20k rows; we grow by
    doubling instead. Deletes swap the last row into the hole to stay compact.
    """

    def __init__(self, dim: int, capacity: int = 64) -> None:
        import numpy as np

        self.dim = dim
        self._np = np
        self.cap = max(8, capacity)
        self.mat = np.zeros((self.cap, dim), dtype=np.float32)
        self.count = 0
        self.ids: List[str] = []
        self.texts: List[str] = []
        self.id_row: Dict[str, int] = {}

    def load(self, ids: List[str], texts: List[str], blob: bytes) -> None:
        np = self._np
        n = len(ids)
        self.ids = list(ids)
        self.texts = list(texts)
        self.id_row = {mem_id: i for i, mem_id in enumerate(self.ids)}
        self.count = n
        cap = max(8, 1)
        while cap < n * 2:
            cap *= 2
        self.cap = cap
        self.mat = np.zeros((cap, self.dim), dtype=np.float32)
        if n:
            flat = np.frombuffer(blob, dtype=np.int8).astype(np.float32) / 127.0
            if flat.size != n * self.dim:
                logger.error(
                    "minimem: vector blob size mismatch (%d != %d*%d) — dense search disabled for this load",
                    flat.size, n, self.dim,
                )
                self.count = 0
                self.ids, self.texts, self.id_row = [], [], {}
                return
            self.mat[:n] = flat.reshape(n, self.dim)

    def _grow(self) -> None:
        np = self._np
        new_cap = self.cap * 2
        new_mat = np.zeros((new_cap, self.dim), dtype=np.float32)
        new_mat[: self.count] = self.mat[: self.count]
        self.mat = new_mat
        self.cap = new_cap

    def append(self, mem_id: str, text: str, vec: "Any") -> None:
        if len(vec) != self.dim:
            logger.warning(
                "minimem: refusing to index a %d-d vector into a %d-d matrix",
                len(vec), self.dim,
            )
            return
        if mem_id in self.id_row:  # replace in place
            row = self.id_row[mem_id]
            self.mat[row] = vec
            self.texts[row] = text
            return
        if self.count >= self.cap:
            self._grow()
        row = self.count
        self.mat[row] = vec
        self.ids.append(mem_id)
        self.texts.append(text)
        self.id_row[mem_id] = row
        self.count += 1

    def remove(self, mem_id: str) -> bool:
        row = self.id_row.pop(mem_id, None)
        if row is None:
            return False
        last = self.count - 1
        if row != last:
            self.mat[row] = self.mat[last]
            moved_id = self.ids[last]
            self.ids[row] = moved_id
            self.texts[row] = self.texts[last]
            self.id_row[moved_id] = row
        self.ids.pop()
        self.texts.pop()
        self.count -= 1
        return True

    def search(self, qvec: "Any", topk: int) -> Dict[int, float]:
        np = self._np
        if not self.count:
            return {}
        scores = self.mat[: self.count] @ qvec
        k = min(topk, self.count)
        if k <= 0:
            return {}
        if k < self.count:
            part = np.argpartition(-scores, k - 1)[:k]
        else:
            part = np.arange(self.count)
        return {int(i): float(scores[i]) for i in part}


class MinimemProvider(MemoryProvider):
    """SQLite + int8 matrix + BM25, five tools, no character budget."""

    def __init__(self) -> None:
        self._init_lock = threading.Lock()
        self._ready = False
        self._hermes_home: Optional[str] = None
        self._session_id: str = ""
        self._agent_context: str = "primary"

        self._settings: Optional[Settings] = None
        self._store: Optional[Store] = None
        self._embedder: Optional[Any] = None
        self._matrix: Optional[_VectorMatrix] = None
        self._bm25 = bm25_mod.BM25Index()
        self._sparse_index = _ModelSparseIndex()
        self._numpy_ok = False
        self._plugin_dir = Path(__file__).resolve().parent

        # Two row spaces, deliberately kept apart:
        #   BM25 row   = position in ``mem ORDER BY rowid`` -> covers EVERY entry
        #   matrix row = position in the model+dim-filtered load -> only scorable vectors
        # Fusion happens in BM25 row space; dense hits are translated through the id map.
        self._rows: List[str] = []
        self._texts: List[str] = []
        self._imps: List[float] = []
        self._id_to_row: Dict[str, int] = {}

        self._lock = threading.RLock()
        self._backend_alive = True         # False after 3 consecutive backend failures (tier L2)
        self._numpy_error: str = ""

        # prefetch cache: session_id -> (text, count)
        self._prefetch_cache: Dict[str, Tuple[str, int]] = {}
        self._prefetch_queue: deque = deque(maxlen=4)
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_stop = threading.Event()
        self._prefetch_wake = threading.Event()

        # mirror writes (on_memory_write) are debounced and batched
        self._pending: List[Dict[str, Any]] = []
        self._pending_lock = threading.Lock()
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_stop = threading.Event()
        self._writer_wake = threading.Event()
        self._last_write_at = 0.0
        self._mirror_dedupe: Dict[str, float] = {}

        self._last_recall: Optional[RecallStatus] = None
        self._stats_counters = {"writes": 0, "recalls": 0, "degraded": 0, "errors": 0}

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return "hermes-minimem"

    def is_available(self) -> bool:
        """Config + dependency check only — never the network (gated by the framework).

        A missing API key is NOT fatal: without it we still serve tier L2/L3 keyword
        recall over whatever is already stored, which is better than refusing to load.
        """
        return True

    def unavailable_reason(self) -> str:
        if self._settings is None:
            return "hermes-minimem: settings not loaded"
        problems = [p for p in self._settings.validate() if "API key" in p]
        return "; ".join(problems)

    # -- lifecycle ---------------------------------------------------------
    def initialize(self, session_id: str, **kwargs) -> None:
        with self._init_lock:
            if self._ready:
                return
            self._session_id = session_id or ""
            self._agent_context = str(kwargs.get("agent_context") or "primary")
            hermes_home = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME") or "."
            self._hermes_home = str(hermes_home)

            ctx = kwargs.get("ctx") or kwargs.get("plugin_context")
            # The v0.8 contract: fill only genuinely missing panel keys, through the official
            # writer, before the provider ever resolves the settings it runs on.
            config_mod.ensure_defaults(ctx=ctx, plugin_dir=self._plugin_dir)
            self._settings = load_settings(ctx)
            problems = self._settings.validate()
            non_key = [p for p in problems if "API key" not in p]
            for problem in problems:
                logger.warning("minimem: %s", problem)

            db_path = storage_path(self._hermes_home)
            self._store = Store(db_path)
            # A malformed backend combination must not leave half a client configured: it
            # gets a clear problem and runs in keyword/BM25 mode until the panel is fixed.
            if non_key:
                self._embedder = None
                logger.warning("minimem: backend configuration problems: %s", "; ".join(non_key))
            elif self._settings.backend == "openai" and not self._settings.api_key:
                self._embedder = Embedder(
                    self._settings.base_url, self._settings.model,
                    self._settings.dim, self._settings.api_key,
                )
            else:
                self._embedder = create_embedder(self._settings, self._hermes_home)

            try:
                import numpy  # noqa: F401  (proves the native import works before we rely on it)
                self._numpy_ok = True
            except Exception as exc:
                self._numpy_ok = False
                self._numpy_error = str(exc)
                logger.warning(
                    "minimem: numpy unavailable (%s) — dense search disabled, keyword tier only", exc
                )

            self._load_index()
            if non_key:
                logger.warning("minimem: configuration problems: %s", "; ".join(non_key))

            self._prefetch_thread = spawn_context_thread(
                self._prefetch_loop, name=f"minimem-prefetch-{session_id}", daemon=True
            )
            self._prefetch_thread.start()
            self._writer_thread = spawn_context_thread(
                self._writer_loop, name=f"minimem-writer-{session_id}", daemon=True
            )
            self._writer_thread.start()

            self._ready = True
            if self._numpy_ok:
                logger.debug(
                    "minimem ready: %d entries, dim %d, model %s, db %s",
                    self._matrix.count if self._matrix else 0,
                    self._settings.dim, self._settings.model, db_path,
                )

    def _load_index(self) -> None:
        """Build the dense matrix and the BM25 index from SQLite. Runs once per instance."""
        with self._lock:
            self._rebuild_indices()

    def _rebuild_indices(self) -> None:
        """Full rebuild of dense, BM25 and model-sparse indexes. Caller holds ``self._lock``.

        BM25 covers every row (a text match is a text match even when the row's vector
        came from another model); the matrix only holds rows whose backend+model+dim match
        the current settings. Model-sparse rows likewise carry their backend+sparse model,
        so a config change degrades to keyword search instead of scoring a foreign space.
        """
        assert self._store is not None and self._settings is not None
        settings = self._settings
        rows = self._store.all_ids_texts()
        self._rows = [mem_id for mem_id, _text, _imp in rows]
        self._texts = [text for _mem_id, text, _imp in rows]
        self._imps = [imp for _mem_id, _text, imp in rows]
        self._id_to_row = {mem_id: i for i, mem_id in enumerate(self._rows)}
        self._bm25.build(list(enumerate(self._texts)))

        if self._numpy_ok and settings.wants_dense:
            ids, texts, blob = self._store.all_vectors(settings.model, settings.dim, settings.backend)
            if self._matrix is None or self._matrix.dim != settings.dim:
                self._matrix = _VectorMatrix(settings.dim)
            self._matrix.load(ids, texts, blob)
        else:
            self._matrix = None

        if settings.uses_model_sparse:
            self._sparse_index.build(self._store.all_sparse(settings.sparse_model, settings.backend))
        else:
            self._sparse_index = _ModelSparseIndex()

    def shutdown(self) -> None:
        self._writer_stop.set()
        self._prefetch_stop.set()
        self._writer_wake.set()
        self._prefetch_wake.set()
        for thread in (self._writer_thread, self._prefetch_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        self._flush_pending(force=True)
        if self._embedder is not None:
            try:
                self._embedder.close()
            except AttributeError:
                pass
            except Exception:
                logger.debug("minimem: embedding backend close failed", exc_info=True)
        if self._store:
            try:
                self._store.close()
            except Exception:
                pass
        self._ready = False

    def backup_paths(self) -> List[str]:
        """Provider state outside HERMES_HOME — nothing here; the db lives inside it."""
        return []

    # -- prompt surface ----------------------------------------------------
    def system_prompt_block(self) -> str:
        """Static only. Recalled content goes through prefetch()."""
        return (
            "## Long-term memory (hermes-minimem)\n"
            "Durable memories persist in a local SQLite store and are recalled automatically "
            "when relevant. Use `minimem_remember` to store durable facts, `minimem_recall` to "
            "look something up explicitly. Memories are injected into the system prompt, so "
            "never write instructions into them."
        )

    # -- recall ------------------------------------------------------------
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Never blocks: returns the cached block the background worker produced."""
        cached = self._prefetch_cache.pop(session_id or self._session_id, None)
        if not cached:
            self._last_recall = None
            return ""
        text, count = cached
        self._last_recall = RecallStatus(provider_label="minimem", count=count)
        return text

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._ready or not (query or "").strip():
            return
        try:
            self._prefetch_queue.append((query, session_id or self._session_id))
            self._prefetch_wake.set()
        except Exception:
            logger.debug("minimem: queue_prefetch failed", exc_info=True)

    def _prefetch_loop(self) -> None:
        while not self._prefetch_stop.is_set():
            self._prefetch_wake.wait(timeout=1.0)
            self._prefetch_wake.clear()
            while self._prefetch_queue and not self._prefetch_stop.is_set():
                try:
                    query, session_id = self._prefetch_queue.popleft()
                except IndexError:
                    break
                try:
                    block, count = self._recall_block(query, limit=5)
                except Exception:
                    logger.debug("minimem: background recall failed", exc_info=True)
                    continue
                if block:
                    self._prefetch_cache[session_id] = (block, count)
                    # Bound the cache: only the newest session blocks matter.
                    while len(self._prefetch_cache) > 8:
                        self._prefetch_cache.pop(next(iter(self._prefetch_cache)))

    def _recall_block(self, query: str, limit: int = 5) -> Tuple[str, int]:
        results = self.recall(query, limit=limit)
        if not results:
            return "", 0
        lines = ["## Recalled memories", ""]
        for item in results:
            lines.append(f"- [{item['kind']}] {item['text']}")
        return "\n".join(lines), len(results)

    def recall(self, query: str, limit: int = 8, *, include_scores: bool = False) -> List[Dict[str, Any]]:
        """The actual hybrid search. Returns [] rather than raising, always.

        The embedding call happens BEFORE the lock is taken: holding the RLock across an
        HTTP call, PM helper startup, or local model inference would stall every writer --
        remember/forget/update and the mirror flush all queue behind it.
        """
        if not self._ready or not (query or "").strip():
            return []
        try:
            qdense, qsparse = self._prepare_query_embedding(query)
            with self._lock:
                return self._recall_locked(query, limit, include_scores, qdense, qsparse)
        except Exception:
            self._stats_counters["errors"] += 1
            logger.warning("minimem: recall failed", exc_info=True)
            return []

    def _prepare_query_embedding(self, query: str) -> Tuple[Optional["Any"], Optional[List[Tuple[int, float]]]]:
        """Embed outside the store lock. None channels mean L2/L3 keyword degradation."""
        assert self._settings is not None
        needs_backend = self._settings.wants_dense or self._settings.uses_model_sparse
        if not needs_backend or not self._embedder or not self._backend_alive:
            return None, None
        if not getattr(self._embedder, "is_configured", lambda: True)():
            # m6: a keyless backend must fail fast into the keyword tier instead of
            # burning a 401 + retry chain on every single recall.
            return None, None
        try:
            import numpy as np

            raw = self._embedder.embed_one(query, text_type="query")
            result = coerce_embedding_result(raw)
            dense = None
            if self._settings.wants_dense and result.dense:
                arr = np.asarray(result.dense, dtype=np.float32)
                norm = float(np.linalg.norm(arr)) or 1.0
                dense = arr / norm
            return dense, list(result.sparse or []) if self._settings.uses_model_sparse else None
        except Exception as exc:  # noqa: BLE001 - recall's contract is "never raise";
            # URLError, subprocess death or a bad worker response all degrade together.
            _ = exc
            self._stats_counters["degraded"] += 1
            failures = int(getattr(self._embedder, "failures", 0) or 0)
            if failures >= _FAILURES_BEFORE_L2:
                self._backend_alive = False
                logger.warning(
                    "minimem: embedding backend failing, keyword-only for this session: %s", exc
                )
            return None, None

    @property
    def _dense_alive(self) -> bool:
        """Compatibility alias for v0.7 tests and old diagnostics."""
        return self._backend_alive

    @_dense_alive.setter
    def _dense_alive(self, value: bool) -> None:
        self._backend_alive = bool(value)

    def _recall_locked(
        self, query: str, limit: int, include_scores: bool,
        qdense=None, qsparse: Optional[Sequence[Tuple[int, float]]] = None,
    ) -> List[Dict[str, Any]]:
        """Scoring + fusion under the lock. Query embeddings were made outside it."""
        assert self._store is not None and self._settings is not None
        if not self._texts:
            return []

        settings = self._settings
        bm25_scores: Dict[int, float] = {}
        model_sparse_scores: Dict[int, float] = {}
        if settings.sparse_mode == "bm25":
            bm25_scores = self._bm25.search(query, limit=max(limit * 8, 50))
        elif settings.uses_model_sparse and qsparse:
            indexed: Dict[int, float] = {}
            for sparse_row, score in self._sparse_index.search(qsparse, topk=max(limit * 8, 50)).items():
                row = self._id_to_row.get(self._sparse_index.ids[sparse_row])
                if row is not None:
                    indexed[row] = score
            model_sparse_scores = indexed

        sparse = model_sparse_scores if settings.uses_model_sparse else bm25_scores
        dense: Dict[int, float] = {}
        if qdense is not None and self._matrix is not None and self._matrix.count:
            if qdense.size != self._matrix.dim:
                self._stats_counters["degraded"] += 1
                logger.warning(
                    "minimem: query dim %d != index dim %d -- keyword tier for this query",
                    qdense.size, self._matrix.dim,
                )
            else:
                for mat_row, score in self._matrix.search(qdense, topk=max(limit * 8, 50)).items():
                    row = self._id_to_row.get(self._matrix.ids[mat_row])
                    if row is not None:
                        dense[row] = score

        best_dense = max(dense.values(), default=0.0)
        best_sparse = max(sparse.values(), default=0.0)
        sparse_floor = MODEL_SPARSE_MIN_RAW if settings.uses_model_sparse else SPARSE_MIN_RAW
        if dense and best_dense < DENSE_MIN_COS and best_sparse < sparse_floor:
            return []

        keyword_tier = False
        if not dense and not sparse:
            # Tier L2/L3: neither configured semantic channel produced anything.
            sparse = dict(rank.keyword_search(self._texts, query, limit=max(limit * 4, 20)))
            if not sparse:
                return []
            keyword_tier = True

        fused = rank.fuse(
            dense,
            sparse,
            alpha=rank.alpha_for(query, settings.alpha_fixed),
            importance={row: self._imps[row] for row in (set(dense) | set(sparse)) if row < len(self._imps)},
            limit=limit,
            # The fallback exists BECAUSE both real channels are gone; letting sparse_mode=off
            # switch it off too would return nothing while the text sat right there.
            sparse_enabled=keyword_tier or (settings.sparse_mode != "off"),
        )
        if not fused:
            return []
        self._stats_counters["recalls"] += 1

        out: List[Dict[str, Any]] = []
        for row_idx, score in fused:
            if row_idx >= len(self._rows):
                continue
            mem = self._store.get(self._rows[row_idx])
            if not mem:
                continue
            item = {
                "id": mem["id"],
                "text": mem["text"],
                "kind": mem["kind"],
                "importance": mem["imp"],
                "created": mem["created"],
            }
            if include_scores:
                item["score"] = round(score, 4)
            out.append(item)
        return out

    # -- writes ------------------------------------------------------------
    def _embed_document(self, text: str) -> Optional[EmbeddingResult]:
        """Document result, or None when the backend is unusable or under-configured."""
        assert self._settings is not None
        settings = self._settings
        if not settings.wants_dense and not settings.uses_model_sparse:
            return None
        if not self._backend_alive:
            # L2 contract (§13.6): once the backend is marked dead this session, writes
            # must not re-pay its retry chain either.
            return None
        if not self._embedder or not getattr(self._embedder, "is_configured", lambda: True)():
            return None
        try:
            result = coerce_embedding_result(self._embedder.embed_one(text, text_type="document"))
        except Exception as exc:  # noqa: BLE001 - same contract as query embedding
            logger.debug("minimem: document embedding failed: %s", exc)
            return None
        if settings.wants_dense and not result.dense:
            return None
        if settings.uses_model_sparse and result.sparse is None:
            return None

        if result.dense:
            squared = 0.0
            for value in result.dense:
                squared += float(value) * float(value)
            norm = squared ** 0.5 or 1.0
            dense = [float(value) / norm for value in result.dense]
        else:
            dense = None
        return EmbeddingResult(dense=dense, sparse=result.sparse)

    def remember(
        self,
        text: str,
        kind: str = "fact",
        imp: float = 0.5,
        *,
        source: Optional[Dict[str, Any]] = None,
        mem_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Synchronous write (tool path): the caller expects it to be recallable at once."""
        if not self._ready or not self._store or not self._settings:
            return {"ok": False, "error": "provider not initialized"}
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "text is empty"}
        kind = kind if kind in KINDS else "fact"
        result = self._embed_document(text)
        if result is None:
            return {
                "ok": False,
                "error": "embedding unavailable — configure the selected backend and check its output type "
                         "(nothing is stored without the requested vectors, so recall stays consistent)",
            }
        vec = pack_vector(result.dense) if result.dense else b""
        dim = len(vec)
        with self._lock:
            mem_id = self._store.add(
                text, kind, vec, self._settings.model, dim,
                imp=imp, source=source, mem_id=mem_id,
                sparse=result.sparse if self._settings.uses_model_sparse else None,
                sparse_model=self._settings.sparse_model if self._settings.uses_model_sparse else None,
                backend=self._settings.backend,
                output_type=self._settings.output_type,
            )
            try:
                self._index_new(mem_id, text, vec, dim, imp=imp,
                                sparse=result.sparse if self._settings.uses_model_sparse else None)
            except Exception:
                # Roll the row back rather than leaving a stored entry the indexes cannot
                # reach: an unreachable row is invisible until the next restart, and a retry
                # would insert a second copy of the same text.
                self._store.delete(mem_id)
                raise
        self._stats_counters["writes"] += 1
        return {"ok": True, "id": mem_id, "kind": kind}

    def _index_new(
        self, mem_id: str, text: str, vec: bytes, dim: int, *, imp: float = 0.5,
        sparse: Optional[Sequence[Tuple[int, float]]] = None,
    ) -> None:
        """Add or replace one row in all live indexes. Caller holds ``self._lock``."""
        assert self._settings is not None
        with self._lock:
            row = self._id_to_row.get(mem_id)
            if row is None:
                row = len(self._rows)
                self._rows.append(mem_id)
                self._texts.append(text)
                self._imps.append(imp)
                self._id_to_row[mem_id] = row
                self._bm25.add_document(row, text)
            else:
                self._texts[row] = text
                self._imps[row] = imp
                self._bm25.remove_document(row)
                self._bm25.add_document(row, text)
            # Against the matrix's real width: if the endpoint returned a different dimension
            # than configured, the row still belongs in SQLite and BM25, it just cannot be
            # scored densely until re-embedded.
            if self._numpy_ok and self._matrix is not None and self._settings.wants_dense and dim == self._matrix.dim:
                self._matrix.append(mem_id, text, unpack_vector(vec))
            if self._settings.uses_model_sparse and sparse is not None:
                self._sparse_index.append(mem_id, sparse)

    def _reindex_all(self) -> None:
        """Full rebuild after a delete or text edit: 156 ms at 1.6k entries, ~1.9 s at 20k."""
        with self._lock:
            self._rebuild_indices()

    # -- bulk maintenance (CLI: reembed / import) ---------------------------
    def reembed_all(self, *, batch: int = 8, dry_run: bool = False,
                    every: bool = False, verbose: bool = False) -> Dict[str, Any]:
        """Re-embed stale rows in place with the currently configured backend.

        A row is stale when its stored vector triplet (backend, model, dim) does not match
        live settings, or a requested channel (dense / model-sparse) is missing from it.
        ``every=True`` re-embeds all rows instead, which is how you move a corpus between
        backends while the old vectors still technically satisfy a fallback config.

        Rows are embedded in batches and updated individually; a batch that fails marks its
        rows as errors and the run continues. ``updated`` counts rows that actually changed;
        a row whose embedding failed keeps its old vectors and stays stale (never blanked).
        """
        if not self._ready or not self._store or not self._settings:
            return {"ok": False, "error": "provider not initialized"}
        settings = self._settings
        embedder = self._embedder
        if not embedder or not getattr(embedder, "is_configured", lambda: True)():
            return {"ok": False, "error": "embedding backend is not configured — set the backend first"}
        wants_dense = settings.wants_dense
        wants_sparse = settings.uses_model_sparse
        if not wants_dense and not wants_sparse:
            return {"ok": False, "error": "current settings request no vectors (output off / sparse bm25) — nothing to embed"}
        if every:
            targets = [mem_id for mem_id, _t, _i in self._store.all_ids_texts()]
        else:
            targets = self._store.stale_ids(
                settings.model, settings.dim, backend=settings.backend,
                wants_dense=wants_dense, wants_model_sparse=wants_sparse,
                sparse_model=settings.sparse_model,
            )
        summary = {"ok": True, "total": len(targets), "updated": 0, "errors": 0, "dry_run": dry_run}
        if dry_run or not targets:
            return summary
        batch = max(1, int(batch))
        for start in range(0, len(targets), batch):
            chunk = targets[start : start + batch]
            rows = [(mem_id, self._store.get(mem_id)) for mem_id in chunk]
            rows = [(mem_id, row) for mem_id, row in rows if row]
            if not rows:
                continue
            results = _embed_resilient(embedder, [row["text"] for _mid, row in rows])
            for (mem_id, row), result in zip(rows, results):
                if result is None:
                    summary["errors"] += 1
                    continue
                result = coerce_embedding_result(result)
                if wants_dense and not result.dense:
                    summary["errors"] += 1
                    continue
                if wants_sparse and result.sparse is None:
                    summary["errors"] += 1
                    continue
                if result.dense:
                    squared = 0.0
                    for value in result.dense:
                        squared += float(value) * float(value)
                    norm = squared ** 0.5 or 1.0
                    dense = [float(value) / norm for value in result.dense]
                    vec = pack_vector(dense)
                    dim = len(vec)
                else:
                    vec, dim = None, None
                self._store.update(
                    mem_id,
                    vec=vec, vec_model=settings.model, vec_dim=dim,
                    sparse=result.sparse if wants_sparse else None,
                    sparse_model=settings.sparse_model if wants_sparse else None,
                    backend=settings.backend,
                    output_type=settings.output_type,
                )
                summary["updated"] += 1
            if verbose:
                done = min(start + batch, len(targets))
                print(f"  {done}/{len(targets)} (updated {summary['updated']}, errors {summary['errors']})", flush=True)
        # Rows changed backend/model/dim or gained sparse channels: live indexes must be
        # rebuilt from the new columns before the next recall in this process.
        self._reindex_all()
        return summary

    def import_documents(
        self,
        records: List[Dict[str, Any]],
        *,
        batch: int = 8,
        dedupe: bool = True,
        default_kind: str = "fact",
        source_origin: str = "import",
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """Embed and insert external records (mnemosyne export, JSONL, any list of dicts).

        Each record needs ``text``; optional ``kind`` (mapped onto KINDS, unknown ->
        default_kind), ``importance`` (0..1, default 0.5) and ``source`` (dict merged with
        origin stamps). Vectors are ALWAYS re-embedded by the live backend — a foreign
        database's vectors belong to another space and are dropped on purpose (§no-mixing).
        ``dedupe`` skips a record whose exact text already exists, which makes an
        interrupted import safely re-runnable.
        """
        if not self._ready or not self._store or not self._settings:
            return {"ok": False, "error": "provider not initialized"}
        settings = self._settings
        embedder = self._embedder
        if not embedder or not getattr(embedder, "is_configured", lambda: True)():
            return {"ok": False, "error": "embedding backend is not configured — imports write vectors, they are mandatory here"}
        prepared = []
        for rec in records:
            text = str(rec.get("text") or "").strip()
            if not text:
                continue
            kind = rec.get("kind") if rec.get("kind") in KINDS else default_kind
            try:
                imp = float(rec.get("importance", 0.5))
            except (TypeError, ValueError):
                imp = 0.5
            source = dict(rec.get("source") or {})
            source.setdefault("origin", source_origin)
            source["imported_by"] = source_origin
            prepared.append((text[:MAX_TEXT], kind, max(0.0, min(1.0, imp)), source))
        summary = {"ok": True, "total": len(prepared), "imported": 0, "skipped_dupe": 0, "errors": 0}
        batch = max(1, int(batch))
        for start in range(0, len(prepared), batch):
            chunk = prepared[start : start + batch]
            if dedupe:
                kept = []
                for item in chunk:
                    if self._store.find_by_text(item[0]) is not None:
                        summary["skipped_dupe"] += 1
                    else:
                        kept.append(item)
                chunk = kept
            if not chunk:
                continue
            results = _embed_resilient(embedder, [t for t, _k, _i, _s in chunk])
            rows = []
            for (text, kind, imp, source), result in zip(chunk, results):
                if result is None:
                    summary["errors"] += 1
                    continue
                result = coerce_embedding_result(result)
                if settings.wants_dense and not result.dense:
                    summary["errors"] += 1
                    continue
                if settings.uses_model_sparse and result.sparse is None:
                    summary["errors"] += 1
                    continue
                if result.dense:
                    squared = 0.0
                    for value in result.dense:
                        squared += float(value) * float(value)
                    norm = squared ** 0.5 or 1.0
                    vec = pack_vector([float(value) / norm for value in result.dense])
                    dim = len(vec)
                else:
                    vec, dim = b"", 0
                rows.append((
                    text, kind, vec, settings.model, dim,
                    {"imp": imp, "source": source,
                     "sparse": result.sparse if settings.uses_model_sparse else None},
                ))
            with self._lock:
                for text, kind, vec, model, dim, extra in rows:
                    mem_id = self._store.add(
                        text, kind, vec, model, dim,
                        imp=extra["imp"], source=extra["source"],
                        sparse=extra["sparse"],
                        sparse_model=settings.sparse_model if extra["sparse"] is not None else None,
                        backend=settings.backend,
                        output_type=settings.output_type,
                    )
                    self._index_new(mem_id, text, vec, dim, imp=extra["imp"], sparse=extra["sparse"])
                summary["imported"] += len(rows)
            if verbose:
                done = min(start + batch, len(prepared))
                print(f"  {done}/{len(prepared)} (imported {summary['imported']}, dupe {summary['skipped_dupe']}, errors {summary['errors']})", flush=True)
        return summary

    # -- mirror writes from the built-in memory tool -----------------------
    def on_memory_write(
        self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Mirror a built-in MEMORY.md/USER.md write. Fast: queues, never blocks the tool.

        Only the primary agent context writes; subagents and cron jobs read but do not add
        to durable memory (the framework sets ``agent_context`` for exactly this reason).
        """
        if not self._ready or self._agent_context != "primary":
            return
        try:
            meta = metadata or {}
            origin = str(meta.get("write_origin") or "memory_tool")
            # Only ``previous_content`` is authoritative identity (PROMPT.md §6.5); an
            # ``old_text`` fragment is not enough to safely delete or rewrite a row.
            previous = meta.get("previous_content") or ""
            if action == "add":
                self._enqueue_mirror(content, target, origin, meta, previous=None)
            elif action == "replace":
                self._enqueue_mirror(content, target, origin, meta, previous=previous)
            elif action == "remove":
                self._enqueue_mirror("", target, origin, meta, previous=previous, remove=True)
        except Exception:
            logger.debug("minimem: on_memory_write failed", exc_info=True)

    def _enqueue_mirror(
        self,
        content: str,
        target: str,
        origin: str,
        meta: Dict[str, Any],
        *,
        previous: Optional[str] = None,
        remove: bool = False,
    ) -> None:
        text = (content or "").strip()
        if not remove and not text:
            return
        if not remove:
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            now = time.time()
            last = self._mirror_dedupe.get(digest, 0.0)
            if now - last < _MIRROR_DEDUPE_S:
                return  # same content re-reported within the window
            self._mirror_dedupe[digest] = now
            if len(self._mirror_dedupe) > 512:  # keep the window map bounded
                cutoff = now - _MIRROR_DEDUPE_S
                self._mirror_dedupe = {k: v for k, v in self._mirror_dedupe.items() if v >= cutoff}
        with self._pending_lock:
            self._pending.append({
                "action": "remove" if remove else ("replace" if previous else "add"),
                "text": text,
                "previous": previous or "",
                "kind": _KIND_BY_TARGET.get(target, "fact"),
                "origin": origin,
                "session_id": meta.get("session_id"),
                "tool_name": meta.get("tool_name"),
            })
        self._writer_wake.set()

    def _read_builtin_entries(self, path: str) -> List[str]:
        """Split a built-in memory file into entries on a lone ``§`` line."""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return []
        entries = []
        for part in _ENTRY_SEP_RE.split(text):
            # Drop heading lines, not whole blocks: " # Memory\n\nfirst entry" is one part,
            # and skipping the part would silently lose the entry underneath the heading.
            body = "\n".join(
                line for line in part.splitlines() if not line.strip().startswith("#")
            ).strip()
            if not body or len(body) < 8:
                continue
            entries.append(body)
        return entries

    def _sync_builtin_memory(self) -> None:
        """Reconcile MEMORY.md / USER.md into the store.

        Hermes forks the background reviewer with ``skip_memory=True`` (the fork's comment:
        an external provider scoped to the parent session would leak the harness prompt into
        the user's real memory namespace), so ``on_memory_write`` fires only when the MAIN
        agent calls the built-in memory tool itself. The reviewer's extractions therefore
        never arrive as a notification — the files on disk are the only place they show up.
        Reconciling against them at startup is what keeps the store from silently rotting.

        Entries are matched by exact text regardless of origin: if the same sentence is
        already stored (from either side), a second copy would only duplicate recall output.

        Non-primary contexts (``agent_context`` = "cron"/"subagent"/"flush") must not run
        this: the CLI builds a throwaway provider with ``agent_context="cron"``, and a plain
        ``hermes-minimem stats`` (a read-only command) would otherwise embed every missing
        MEMORY.md/USER.md entry and write rows into SQLite. Hermes' own contract for
        providers is the same — skip writes for non-primary contexts; the primary gateway
        provider owns the reconcile.
        """
        if self._agent_context != "primary":
            return
        if not self._store or not self._settings or not self._hermes_home:
            return
        candidates: List[tuple[str, str]] = []
        for target, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
            path = os.path.join(self._hermes_home, "memories", filename)
            for entry in self._read_builtin_entries(path):
                candidates.append((target, entry))
        if not candidates:
            return

        added = 0
        for target, text in candidates:
            try:
                if self._store.find_by_text(text) is not None:
                    continue
                result = self._embed_document(text)
                if result is None:
                    self._stats_counters["mirror_skipped_no_vector"] = (
                        self._stats_counters.get("mirror_skipped_no_vector", 0) + 1
                    )
                    continue
                vec = pack_vector(result.dense) if result.dense else b""
                with self._lock:
                    mem_id = self._store.add(
                        text, _KIND_BY_TARGET.get(target, "fact"), vec,
                        self._settings.model, len(vec), imp=0.6,
                        source={"origin": "memory_tool", "synced_from": target},
                        sparse=result.sparse if self._settings.uses_model_sparse else None,
                        sparse_model=self._settings.sparse_model if self._settings.uses_model_sparse else None,
                        backend=self._settings.backend,
                        output_type=self._settings.output_type,
                    )
                    self._index_new(mem_id, text, vec, len(vec), imp=0.6,
                                    sparse=result.sparse if self._settings.uses_model_sparse else None)
                added += 1
            except Exception:
                logger.debug("minimem: builtin sync failed for one entry", exc_info=True)
        self._stats_counters["mirror_synced"] = added
        if added:
            logger.info("minimem: synced %d entries from MEMORY.md/USER.md", added)

    def _writer_loop(self) -> None:
        try:
            self._sync_builtin_memory()
        except Exception:
            logger.debug("minimem: startup builtin sync failed", exc_info=True)
        while not self._writer_stop.is_set():
            self._writer_wake.wait(timeout=1.0)
            self._writer_wake.clear()
            with self._pending_lock:
                pending = len(self._pending)
            if not pending:
                continue
            elapsed = time.time() - self._last_write_at
            if pending >= _WRITE_BATCH or elapsed >= _WRITE_DEBOUNCE_S:
                self._flush_pending()

    def _flush_pending(self, force: bool = False) -> None:
        """Apply queued mirror writes one at a time.

        Draining the whole batch up front and re-queueing it on failure replayed entries
        that had already been applied — each ``add`` mints a new id, so a replay duplicated
        rows (and the 30 s dedupe window only guards the enqueue path). Popping as we go
        means a failure leaves only the failing item queued.

        The pop and the item hand-off happen in ONE lock acquisition: taking a reference
        under the lock and popping under a second one let the writer thread and an explicit
        force-flush grab the same item, which inserted the same mirror row twice.
        """
        applied = 0
        while True:
            with self._pending_lock:
                if not self._pending:
                    break
                item = self._pending.pop(0)
            try:
                self._apply_mirror(item)
            except Exception:
                self._stats_counters["errors"] += 1
                self._stats_counters["mirror_failed"] = self._stats_counters.get("mirror_failed", 0) + 1
                logger.warning("minimem: mirror write failed, keeping it queued", exc_info=True)
                with self._pending_lock:
                    self._pending.insert(0, item)  # retry this one later, keep the order
                break  # never re-pop the same failing item in one pass (would spin forever)
            applied += 1
            if applied >= _WRITE_BATCH:
                break
        if applied:
            self._last_write_at = time.time()

    def _apply_mirror(self, item: Dict[str, Any]) -> None:
        assert self._store is not None and self._settings is not None
        action = item["action"]
        if action == "remove" and item["previous"]:
            with self._lock:
                # exclude_origin="tool": a mirror remove must never delete a tool-written
                # row that happens to carry the same text.
                removed = self._store.delete_by_text(item["previous"], exclude_origin="tool")
                if removed:
                    self._reindex_all()
            return
        if action == "replace" and item["previous"]:
            with self._lock:
                old = self._store.find_by_text(item["previous"], exclude_origin="tool")
            if old:
                result = self._embed_document(item["text"])
                if result is None:
                    return
                vec = pack_vector(result.dense) if result.dense else b""
                with self._lock:
                    self._store.update(
                        old["id"], text=item["text"], vec=vec,
                        vec_model=self._settings.model, vec_dim=len(vec),
                        sparse=result.sparse if self._settings.uses_model_sparse else None,
                        sparse_model=self._settings.sparse_model if self._settings.uses_model_sparse else None,
                        backend=self._settings.backend, output_type=self._settings.output_type,
                    )
                    self._index_new(
                        old["id"], item["text"], vec, len(vec),
                        imp=float(old.get("imp") or 0.6),
                        sparse=result.sparse if self._settings.uses_model_sparse else None,
                    )
                return
            # fall through: a replace whose old text is gone becomes an add
        result = self.remember(
            item["text"], kind=item["kind"], imp=0.6,
            source={"origin": item["origin"], "session_id": item["session_id"],
                    "tool_name": item["tool_name"]},
        )
        if not result.get("ok"):
            # The built-in store already accepted this write, so dropping it here would make
            # the two stores diverge silently. Count it and let _flush_pending retry.
            self._stats_counters["mirror_dropped"] = self._stats_counters.get("mirror_dropped", 0) + 1
            logger.warning(
                "minimem: mirror write not stored (%s); will retry — the built-in memory "
                "now holds an entry this store does not",
                result.get("error"),
            )
            raise RuntimeError(f"mirror write failed: {result.get('error')}")

    # -- tools -------------------------------------------------------------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "minimem_remember",
                "description": (
                    "Store a durable memory (preference, fact, insight, identity, task, relation). "
                    "Memories are injected into the system prompt later, so never store instructions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The memory text, self-contained."},
                        "kind": {"type": "string", "enum": list(KINDS), "default": "fact"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional labels."},
                        "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.5},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "minimem_recall",
                "description": "Search stored memories (hybrid keyword + semantic). Returns matching entries with ids.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "What to look for."},
                        "limit": {"type": "integer", "default": 8, "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "minimem_update",
                "description": "Rewrite one memory by id. Changing the text re-embeds it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                        "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "minimem_forget",
                "description": "Permanently delete one memory by exact id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
            {
                "name": "minimem_stats",
                "description": "Entry count, database size, embedding backend, stale vectors, last writes.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "minimem_remember":
                result = self.remember(
                    str(args.get("text") or ""),
                    kind=str(args.get("kind") or "fact"),
                    imp=float(args.get("importance", 0.5) or 0.5),
                    source={
                        "origin": "tool",
                        "session_id": kwargs.get("session_id") or self._session_id,
                        "tags": args.get("tags") or [],
                    },
                )
                return json.dumps(result, ensure_ascii=False)
            if tool_name == "minimem_recall":
                query = str(args.get("query") or "")
                limit = int(args.get("limit") or 8)
                items = self.recall(query, limit=max(1, min(50, limit)), include_scores=True)
                return json.dumps(
                    {"ok": True, "count": len(items), "results": items}, ensure_ascii=False
                )
            if tool_name == "minimem_update":
                return json.dumps(self._tool_update(args), ensure_ascii=False)
            if tool_name == "minimem_forget":
                return json.dumps(self._tool_forget(args), ensure_ascii=False)
            if tool_name == "minimem_stats":
                return json.dumps(self.stats(), ensure_ascii=False)
            return json.dumps({"ok": False, "error": f"unknown tool {tool_name}"})
        except Exception as exc:
            self._stats_counters["errors"] += 1
            logger.warning("minimem: tool %s failed", tool_name, exc_info=True)
            return json.dumps({"ok": False, "error": self._redact(f"{type(exc).__name__}: {exc}")}, ensure_ascii=False)

    def _redact(self, text: str) -> str:
        """A18: exception text surfacing into tool output must never carry the key value."""
        key = getattr(self._settings, "api_key", None) if self._settings else None
        if key:
            text = text.replace(str(key), "[redacted]")
        return text[:2000]

    def _tool_update(self, args: Dict[str, Any]) -> Dict[str, Any]:
        mem_id = str(args.get("id") or "").strip()
        if not mem_id or not self._store:
            return {"ok": False, "error": "id is required"}
        existing = self._store.get(mem_id)
        if not existing:
            return {"ok": False, "error": f"no memory with id {mem_id}"}
        new_text = args.get("text")
        importance = args.get("importance")
        if new_text is None and importance is None:
            return {"ok": False, "error": "nothing to update (pass text and/or importance)"}
        result = None
        vec = None
        if isinstance(new_text, str) and new_text.strip():
            if not self._settings:
                return {"ok": False, "error": "provider not initialized"}
            result = self._embed_document(new_text.strip())
            if result is None:
                return {"ok": False, "error": "embedding unavailable, text not changed"}
            vec = pack_vector(result.dense) if result.dense else b""
        with self._lock:
            ok = self._store.update(
                mem_id,
                text=new_text.strip() if isinstance(new_text, str) and new_text.strip() else None,
                vec=vec,
                vec_model=self._settings.model if result is not None else None,
                vec_dim=len(vec) if result is not None else None,
                imp=float(importance) if importance is not None else None,
                sparse=result.sparse if self._settings.uses_model_sparse else None,
                sparse_model=self._settings.sparse_model if self._settings.uses_model_sparse else None,
                backend=self._settings.backend if result is not None else None,
                output_type=self._settings.output_type if result is not None else None,
            )
        if ok and result is not None:
            self._reindex_all()
        return {"ok": bool(ok), "id": mem_id}

    def _tool_forget(self, args: Dict[str, Any]) -> Dict[str, Any]:
        mem_id = str(args.get("id") or "").strip()
        if not mem_id or not self._store:
            return {"ok": False, "error": "id is required"}
        with self._lock:
            deleted = self._store.delete(mem_id)
            if deleted:
                self._reindex_all()
        return {"ok": bool(deleted), "id": mem_id, "deleted": bool(deleted)}

    def stats(self) -> Dict[str, Any]:
        if not self._store or not self._settings:
            return {"ok": False, "error": "provider not initialized"}
        settings = self._settings
        total = self._store.count()
        stale = self._store.count_stale(
            settings.model, settings.dim, backend=settings.backend,
            wants_dense=settings.wants_dense, wants_model_sparse=settings.uses_model_sparse,
            sparse_model=settings.sparse_model,
        )
        recent = self._store.recent(1)
        problems = settings.validate()
        return {
            "ok": True,
            "entries": total,
            "db_bytes": self._store.db_size(),
            "db_path": self._store.path,
            "backend": settings.backend,
            "model": settings.model,
            "dim": settings.dim,
            "output_type": settings.output_type,
            "api_key_source": settings.api_key_source,
            "sparse_mode": settings.sparse_mode,
            "sparse_mode_effective": settings.effective_sparse_mode,
            "dense_available": bool(self._numpy_ok and settings.wants_dense and self._backend_alive),
            "model_sparse_available": bool(settings.uses_model_sparse and self._backend_alive),
            "numpy_ok": self._numpy_ok,
            "numpy_error": self._numpy_error or None,
            "needs_reembed": stale,
            "config_invalid": problems or None,
            "bm25_docs": self._bm25.size,
            "bm25_vocab": self._bm25.vocab_size,
            "model_sparse_docs": self._sparse_index.count,
            "last_write": recent[0]["created"] if recent else None,
            "counters": dict(self._stats_counters),
            "pending_mirror": len(self._pending),
        }

    # -- session plumbing --------------------------------------------------
    def on_session_switch(
        self, new_session_id: str, *, parent_session_id: str = "",
        reset: bool = False, rewound: bool = False, **kwargs,
    ) -> None:
        self._session_id = new_session_id or self._session_id
        if reset:
            self._prefetch_cache.clear()

    def sync_turn(
        self, user_content: str, assistant_content: str, *,
        session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None,
        turn_author: Optional[Dict[str, Any]] = None,
    ) -> None:
        """No durable write here by design.

        Memory extraction belongs to Hermes' own background review, which reports through
        ``on_memory_write``; storing raw turns would duplicate the transcript at full size.
        The only thing this does is schedule the next turn's prefetch from the user's text.
        """
        self.queue_prefetch(user_content or "", session_id=session_id)


def register(ctx) -> None:
    """Entry point the loader looks for in ``__init__.py``."""
    ctx.register_memory_provider(MinimemProvider())
