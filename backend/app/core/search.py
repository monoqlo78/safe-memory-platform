"""Vector + keyword hybrid search over memory entries."""

from __future__ import annotations

import math
import re
from typing import List, Sequence, Tuple

import numpy as np

from app.models.pack_schema import Entry, get_retrieval_text

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 when either vector is empty, mismatched, or zero-length.
    """
    if a is None or b is None:
        return 0.0
    if len(a) == 0 or len(b) == 0 or len(a) != len(b):
        return 0.0

    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)

    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na == 0.0 or nb == 0.0:
        return 0.0

    sim = float(np.dot(va, vb) / (na * nb))
    if math.isnan(sim):
        return 0.0
    # Clamp to a sane range.
    return max(-1.0, min(1.0, sim))


def _keyword_score(query_tokens: List[str], entry: Entry) -> float:
    """Score keyword overlap between the query and an entry in [0, 1]."""
    if not query_tokens:
        return 0.0

    query_set = set(query_tokens)
    entry_tokens = set(_tokenize(get_retrieval_text(entry)))
    entry_tokens.update(_tokenize(" ".join(entry.keywords)))

    if not entry_tokens:
        return 0.0

    overlap = len(query_set & entry_tokens)
    return overlap / len(query_set)


def hybrid_search(
    query: str,
    query_embedding: Sequence[float],
    entries: List[Entry],
    top_k: int = 12,
    vector_weight: float = 0.7,
    keyword_weight: float = 0.3,
) -> List[Tuple[Entry, float]]:
    """Rank ``entries`` against a query using vector + keyword scoring.

    Args:
        query: Raw query text (used for keyword matching).
        query_embedding: Embedding vector for the query.
        entries: Candidate memory entries.
        top_k: Maximum number of results to return.
        vector_weight: Weight applied to cosine similarity.
        keyword_weight: Weight applied to keyword overlap.

    Returns:
        A list of ``(entry, score)`` tuples sorted by descending score.
    """
    if not entries:
        return []

    query_tokens = _tokenize(query)
    scored: List[Tuple[Entry, float]] = []

    for entry in entries:
        vec_score = cosine_similarity(query_embedding, entry.embedding)
        # Map cosine [-1, 1] into [0, 1] for stable blending.
        vec_score_norm = (vec_score + 1.0) / 2.0
        kw_score = _keyword_score(query_tokens, entry)

        combined = vector_weight * vec_score_norm + keyword_weight * kw_score
        scored.append((entry, round(combined, 6)))

    scored.sort(key=lambda pair: pair[1], reverse=True)

    if top_k and top_k > 0:
        return scored[:top_k]
    return scored
