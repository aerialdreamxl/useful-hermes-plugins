"""BM25 sparse retrieval — pure Python, zero dependencies, no persistence.

Design decisions (measured on 1650 real memories, see PROMPT.md 5.3):
  * Chinese has no word boundaries we can trust without a segmenter, so a run of CJK
    characters contributes unigrams AND adjacent bigrams ("用户偏好" -> 用/户/偏/好/用户/户偏/偏好).
    Bigrams carry most of the signal; unigrams keep single-character queries alive.
  * ASCII runs are kept whole, lowercased, allowing ``_ - . / +`` inside (so
    ``hermes-minimem`` and ``qwen3.7-text-embedding-flash`` stay one token).
  * The index is rebuilt from ``mem.text`` at startup (156 ms / 1648 docs) and never
    persisted: an FTS5 trigram table would cost 7 MB on disk to save 1.3 ms per query.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Tuple

# k1/b are BM25 defaults; with short memories the exact values matter little, but
# b=0.75 still rewards matching a rare term inside a short entry over a long one.
K1 = 1.5
B = 0.75

# Query-time pruning. A term that appears in most documents has an idf near zero, so it
# contributes nothing to the ranking but costs a full posting-list walk. Measured at 20k
# rows on the real corpus this was the difference between ~68 ms and single-digit ms per
# query. MAX_SCORED_TERMS bounds the work for long queries (the lowest-idf terms go first
# because they cannot change the top-k order noticeably).
MIN_TERM_IDF = 0.20
MAX_SCORED_TERMS = 8

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf"
# One pass, two alternations: CJK runs (group 1) and ASCII words (group 2).
_TOKEN_RE = re.compile(f"([{_CJK}]+)|([A-Za-z0-9_./+-]+)")


def tokenize(text: str) -> List[str]:
    """CJK unigrams + bigrams, ASCII words whole (lowercased), in a single scan.

    One regex pass instead of two: measured ~40% faster on a 5,000-entry rebuild, which is
    half the cold-start budget (PROMPT.md §11 item 11).
    """
    if not text:
        return []
    tokens: List[str] = []
    # findall returns (cjk_run, ascii_word) tuples straight from the C layer; finditer
    # builds a Match object per hit and was measurably slower on a 1.8M-character rebuild.
    # The bigram slice list is built by a comprehension, not a generator: 3.5M generator
    # resumes cost more than the temporary list.
    for run, word in _TOKEN_RE.findall(text):
        if run:
            tokens.extend(run)
            if len(run) > 1:
                tokens.extend([run[i : i + 2] for i in range(len(run) - 1)])
        else:
            tokens.append(word.lower())
    return tokens


class BM25Index:
    """In-memory inverted index: ``term -> {doc_idx: tf}`` plus idf and doc lengths."""

    def __init__(self) -> None:
        self._postings: Dict[str, Dict[int, int]] = {}
        # A list, not a dict: the scoring loop reads this once per posting and a list
        # index is ~2x faster than dict.get. 0 marks a removed/empty document.
        self._doc_len: List[int] = []
        self._idf: Dict[str, float] = {}
        self._total_len = 0
        self._avgdl = 0.0
        self._built = False

    # -- lifecycle ---------------------------------------------------------
    def build(self, docs: Iterable[Tuple[int, str]]) -> None:
        """Replace the whole index. ``docs`` yields ``(doc_idx, text)``."""
        self._postings.clear()
        self._doc_len = []
        self._idf.clear()
        self._total_len = 0
        for idx, text in docs:
            self._index_doc(idx, text)
        self._recompute_idf()
        self._built = True

    def add_document(self, idx: int, text: str) -> None:
        """Incremental insert. Replaces the row when ``idx`` is already indexed."""
        if idx < len(self._doc_len) and self._doc_len[idx]:
            self.remove_document(idx)
        self._index_doc(idx, text)
        # idf drifts as N grows; recomputing on every insert would be O(vocab).
        # Cheap approach: recompute lazily when a query runs after enough inserts.
        self._built = False

    def remove_document(self, idx: int) -> None:
        """Drop one doc from the index (O(vocab) posting scan; callers usually rebuild instead)."""
        if idx >= len(self._doc_len) or not self._doc_len[idx]:
            return
        self._total_len -= self._doc_len[idx]
        self._doc_len[idx] = 0
        for term in [t for t, m in self._postings.items() if idx in m]:
            posting = self._postings[term]
            posting.pop(idx, None)
            if not posting:
                del self._postings[term]
        self._built = False

    # -- internals ---------------------------------------------------------
    def _index_doc(self, idx: int, text: str) -> None:
        tokens = tokenize(text)
        while len(self._doc_len) <= idx:
            self._doc_len.append(0)
        if not tokens:
            return
        counts = Counter(tokens)  # C-implemented; a .get() loop over 3.5M tokens is slower
        postings = self._postings
        for term, tf in counts.items():
            bucket = postings.get(term)
            if bucket is None:
                postings[term] = {idx: tf}
            else:
                bucket[idx] = tf
        self._doc_len[idx] = len(tokens)
        self._total_len += len(tokens)

    def _recompute_idf(self) -> None:
        live = sum(1 for length in self._doc_len if length)
        n = max(1, live)
        self._avgdl = (self._total_len / n) if live else 0.0
        idf: Dict[str, float] = {}
        for term, posting in self._postings.items():
            df = len(posting)
            # BM25+ style shifted idf: never negative, so a term in >half the docs
            # still contributes a little instead of penalising the match.
            idf[term] = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        self._idf = idf

    # -- query -------------------------------------------------------------
    def search(self, query: str, limit: int = 50) -> Dict[int, float]:
        """``doc_idx -> score`` for docs sharing at least one term with the query."""
        if not self._doc_len:
            return {}
        if not self._built:
            self._recompute_idf()
            self._built = True
        terms = tokenize(query)
        if not terms:
            return {}

        # Rank the query's terms by idf and keep the informative ones (see MIN_TERM_IDF).
        weighted: list[tuple[float, str]] = []
        seen: set[str] = set()
        for term in terms:
            if term in seen:
                continue
            seen.add(term)
            if term not in self._postings:
                continue
            idf = self._idf.get(term, 0.0)
            if idf < MIN_TERM_IDF:
                continue
            weighted.append((idf, term))
        if not weighted:
            # Every term is a near-stopword. Fall back to the full set rather than returning
            # nothing: the caller would otherwise degrade to a substring scan, which is worse.
            weighted = [(self._idf.get(t, 0.0), t) for t in seen if t in self._postings]
        if len(weighted) > MAX_SCORED_TERMS:
            weighted.sort(key=lambda pair: pair[0], reverse=True)
            weighted = weighted[:MAX_SCORED_TERMS]

        # Hoist the loop-invariant parts of the BM25 denominator and bind the hot dict
        # accessors locally: this loop runs once per posting (millions of times at 20k).
        doc_len = self._doc_len
        n_docs = len(doc_len)
        k1_1mb = K1 * (1.0 - B)
        k1b_avg = (K1 * B / self._avgdl) if self._avgdl else 0.0
        k1p1 = K1 + 1.0

        scores: Dict[int, float] = {}
        get_score = scores.get
        for idf, term in weighted:
            posting = self._postings[term]
            for idx, tf in posting.items():
                dl = doc_len[idx] if idx < n_docs else 0
                denom = tf + k1_1mb + k1b_avg * dl
                scores[idx] = get_score(idx, 0.0) + idf * tf * k1p1 / denom
        if limit and len(scores) > limit:
            top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
            return dict(top)
        return scores

    # -- introspection -----------------------------------------------------
    @property
    def size(self) -> int:
        return sum(1 for length in self._doc_len if length)

    @property
    def vocab_size(self) -> int:
        return len(self._postings)

    @property
    def posting_count(self) -> int:
        return sum(len(p) for p in self._postings.values())
