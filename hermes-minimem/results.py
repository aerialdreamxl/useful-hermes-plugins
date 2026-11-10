"""Backend-neutral results shared by the three v0.8 embedding backends."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

SparsePair = Tuple[int, float]


@dataclass(frozen=True)
class EmbeddingResult:
    """One embedding response. A channel is absent only when the backend did not request it."""

    dense: Optional[Sequence[float]] = None
    sparse: Optional[Sequence[SparsePair]] = None


def _coerce_sparse(raw: Union[Dict[Any, Any], Iterable[Any]]) -> List[SparsePair]:
    out: List[SparsePair] = []
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = []
        for entry in raw:
            if isinstance(entry, dict):
                # fastembed exposes indices/values; dict entries are accepted too.
                idx = entry.get("index", entry.get("indices", entry.get("token")))
                value = entry.get("value", entry.get("weight"))
                if idx is None or value is None:
                    raise ValueError("sparse entries require index and value")
                items.append((idx, value))
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                items.append((entry[0], entry[1]))
            else:
                raise ValueError("sparse entries must be mappings or (index, value) pairs")
    for idx, value in items:
        out.append((int(idx), float(value)))
    return out


def coerce_embedding_result(raw: Any) -> EmbeddingResult:
    """Accept an EmbeddingResult, legacy dense list, (dense, sparse) tuple, or mapping."""
    if isinstance(raw, EmbeddingResult):
        return raw
    if isinstance(raw, dict):
        dense = raw.get("dense", raw.get("embedding"))
        sparse = raw.get("sparse", raw.get("sparse_embedding"))
        return EmbeddingResult(
            dense=list(dense) if dense is not None else None,
            sparse=_coerce_sparse(sparse) if sparse is not None else None,
        )
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        first, second = raw
        looks_like_result = (
            isinstance(first, (list, tuple)) and
            (second is None or isinstance(second, dict) or (
                isinstance(second, (list, tuple)) and
                all(isinstance(pair, (list, tuple)) and len(pair) >= 2 for pair in second)
            ))
        )
        if looks_like_result:
            return EmbeddingResult(
                dense=list(first) if first is not None else None,
                sparse=_coerce_sparse(second) if second is not None else None,
            )
    if isinstance(raw, (list, tuple)):
        return EmbeddingResult(dense=list(raw), sparse=None)
    raise ValueError(f"unsupported embedding result type: {type(raw).__name__}")


def pack_sparse(sparse: Optional[Sequence[SparsePair]]) -> Optional[str]:
    """Stable compact JSON: [[index, value], ...]. None means no sparse vector."""
    if sparse is None:
        return None
    return json.dumps([[int(idx), float(value)] for idx, value in sparse], separators=(",", ":"))


def unpack_sparse(raw: Optional[str]) -> List[SparsePair]:
    if not raw:
        return []
    try:
        return _coerce_sparse(json.loads(raw))
    except (ValueError, TypeError):
        return []


def sparse_dot(a: Sequence[SparsePair], b: Union[Dict[int, float], Sequence[SparsePair]]) -> float:
    """Dot product over shared sparse indices. The shorter side drives lookups."""
    if not a:
        return 0.0
    if not isinstance(b, dict):
        b = dict(b)
    if len(a) > len(b):
        a_lookup = dict(a)
        return sum(a_lookup.get(idx, 0.0) * value for idx, value in b.items())
    return sum(b.get(idx, 0.0) * value for idx, value in a)
