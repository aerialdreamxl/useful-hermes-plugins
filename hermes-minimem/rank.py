"""Score fusion and the adaptive alpha.

Measured basis (PROMPT.md 5.4, 1650 real memories):
  * min-max weighted sum beats RRF by 7-15 points on fragment queries (RRF throws away
    score magnitude, which is the only thing separating a near-hit from noise here);
  * fragment queries (short, identifier-heavy) want the sparse side; long natural-language
    rewrites want the dense side, so alpha moves between 0.3 and 0.6 rather than being fixed.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

ALPHA_MIN = 0.30
ALPHA_MAX = 0.60
IMP_WEIGHT = 0.10
# There is deliberately no "top-1 normalised score < X" gate here. Min-max scaling always
# maps the best candidate to 1.0, so such a threshold can never fire (it would only ever
# compare 1.0 + 0.1*imp against itself). The real relevance gate lives in
# provider._recall_locked and runs on RAW scores: cosine < 0.25 AND bm25 < 1.5 -> no result.

# Identifiers a user pastes verbatim: acronyms, dates, versions, paths, flags.
_IDENT_RE = re.compile(
    r"(?:[A-Z]{2,}[A-Za-z0-9]*"        # PROMPT, API, HTTP2
    r"|\b\d{4}-\d{2}-\d{2}\b"          # 2026-11-05
    r"|\bv?\d+\.\d+(?:\.\d+)*\b"       # 2.5.4 / v0.7
    r"|[/\\][\w.\-/\\]+"               # /api/memory/providers
    r"|--?\w+)"                        # --flag
)


def has_identifiers(query: str) -> bool:
    return bool(_IDENT_RE.search(query or ""))


def alpha_for(query: str, fixed: float | None = None) -> float:
    """Dense weight for this query. ``fixed`` (a configured override) wins."""
    if fixed is not None:
        return max(ALPHA_MIN, min(ALPHA_MAX, float(fixed)))
    length = len((query or "").strip())
    if length < 25 or has_identifiers(query):
        return 0.35  # fragment recall: the sparse side carries it
    if length <= 60:
        return 0.50
    return 0.60  # a full sentence: semantics are reliable, prefer dense


def minmax(scores: Mapping[int, float]) -> Dict[int, float]:
    """Scale into [0, 1]. A single non-zero candidate becomes 1.0, not 0.0 — otherwise
    the only plausible answer would be dropped by the empty-threshold."""
    if not scores:
        return {}
    values = list(scores.values())
    lo, hi = min(values), max(values)
    if hi - lo <= 1e-12:
        return {k: (1.0 if v > 0 else 0.0) for k, v in scores.items()}
    span = hi - lo
    return {k: (v - lo) / span for k, v in scores.items()}


def fuse(
    dense: Mapping[int, float],
    sparse: Mapping[int, float],
    alpha: float,
    importance: Mapping[int, float] | None = None,
    *,
    limit: int = 8,
    sparse_enabled: bool = True,
) -> List[Tuple[int, float]]:
    """Weighted sum of the normalised channels plus a small importance bonus.

    A row missing from one channel scores 0 there (it did not match that modality),
    which is the intended behaviour: normalisation is per-channel over candidates.
    """
    dense_n = minmax(dense) if dense else {}
    sparse_n = minmax(sparse) if (sparse and sparse_enabled) else {}

    if not sparse_enabled:
        alpha = 1.0
    elif not dense:
        alpha = 0.0
    elif not sparse:
        alpha = 1.0

    candidates = set(dense_n) | set(sparse_n)
    if not candidates:
        return []
    importance = importance or {}
    scored: List[Tuple[int, float]] = []
    for idx in candidates:
        score = alpha * dense_n.get(idx, 0.0) + (1.0 - alpha) * sparse_n.get(idx, 0.0)
        score += IMP_WEIGHT * float(importance.get(idx, 0.5))
        scored.append((idx, score))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:limit]


def keyword_search(texts: Sequence[str], query: str, limit: int = 8) -> List[Tuple[int, float]]:
    """Tier L2/L3 fallback: substring match over the candidate texts, no embeddings.

    Scores are deliberately crude (count of query fragments found) — it exists to keep
    recall alive when the API or numpy is gone, not to be good.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    # Fragments: CJK bigrams plus ASCII words, so "用户偏好" matches text containing 偏好.
    frags = [q[i : i + 2] for i in range(len(q) - 1) if "\u4e00" <= q[i] <= "\u9fff"] if _has_cjk(q) else []
    frags += [w for w in re.findall(r"[a-z0-9_\-./+]{2,}", q)]
    frags = list(dict.fromkeys(frags)) or [q]
    hits: List[Tuple[int, float]] = []
    for idx, text in enumerate(texts):
        low = (text or "").lower()
        score = sum(1.0 for frag in frags if frag in low)
        if score:
            hits.append((idx, score / len(frags)))
    hits.sort(key=lambda kv: kv[1], reverse=True)
    return hits[:limit]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)
