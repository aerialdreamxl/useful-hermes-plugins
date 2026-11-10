"""hermes-minimem — a small, fast, self-contained memory provider for Hermes.

A :class:`agent.memory_provider.MemoryProvider` implementation backed by one SQLite file:
int8 vectors in a plain BLOB column (no vec0, no FAISS), a float32 matrix held in memory for
cosine search, and a pure-Python BM25 index rebuilt at startup. Hybrid fusion with an alpha
that follows the query's shape.

The loader in ``plugins/memory/__init__.py`` only recognises a directory as a provider when
its ``__init__.py`` mentions ``register_memory_provider`` or ``MemoryProvider`` — hence the
explicit import below rather than a lazy one.
"""

from __future__ import annotations

from .provider import MinimemProvider, register

__all__ = ["MinimemProvider", "register"]
