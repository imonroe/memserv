"""Optional post-search re-ranking that blends semantic similarity with recency.

mem0/Qdrant rank purely by vector similarity. For a personal memory store the
most *recent* relevant fact is often the one you actually want, so callers can
opt into a recency boost that re-orders an already-similar result set without
changing which memories are matched. With ``recency_weight=0`` (the default for
both REST and MCP) this module is a no-op and the original order is preserved.
"""

import math
from datetime import UTC, datetime

# Keys a mem0 result may carry a timestamp under, in most-preferred-first order.
_TIMESTAMP_KEYS = ("updated_at", "created_at")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Accept a trailing 'Z' (UTC) which datetime.fromisoformat rejects pre-3.11.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _item_timestamp(item: dict) -> datetime | None:
    for key in _TIMESTAMP_KEYS:
        ts = _parse_timestamp(item.get(key))
        if ts is not None:
            return ts
        meta = item.get("metadata")
        if isinstance(meta, dict):
            ts = _parse_timestamp(meta.get(key))
            if ts is not None:
                return ts
    return None


def rerank_by_recency(
    results: dict,
    recency_weight: float,
    half_life_days: float = 30.0,
    now: datetime | None = None,
) -> dict:
    """Re-order ``results['results']`` by a blend of similarity and recency.

    ``recency_weight`` in [0, 1]: 0 leaves the order untouched (pure similarity),
    1 sorts almost entirely by recency. Similarity scores are min-max normalized
    across the returned set so the two components are comparable. Items without a
    usable timestamp contribute a recency score of 0, so they are never boosted.

    The same dict is returned; its items list is reordered in place and each item
    gains a ``rerank_score`` for transparency. Anything that isn't the expected
    ``{"results": [ {...}, ... ]}`` shape is passed through unchanged.
    """
    if recency_weight <= 0 or not isinstance(results, dict):
        return results
    items = results.get("results")
    if not isinstance(items, list) or len(items) < 2:
        return results
    if not all(isinstance(it, dict) for it in items):
        return results

    weight = min(max(recency_weight, 0.0), 1.0)
    now = now or datetime.now(UTC)
    half_life = max(half_life_days, 1e-9)

    scores = [float(it.get("score") or 0.0) for it in items]
    lo, hi = min(scores), max(scores)
    span = hi - lo

    def _similarity(idx: int) -> float:
        # With no spread between scores, similarity carries no signal; treat all
        # results as equally similar so recency becomes the sole tiebreaker.
        return 1.0 if span <= 0 else (scores[idx] - lo) / span

    def _recency(item: dict) -> float:
        ts = _item_timestamp(item)
        if ts is None:
            return 0.0
        age_days = max((now - ts).total_seconds() / 86400.0, 0.0)
        return math.exp(-math.log(2) * age_days / half_life)

    for idx, item in enumerate(items):
        item["rerank_score"] = (1 - weight) * _similarity(idx) + weight * _recency(item)

    items.sort(key=lambda it: it.get("rerank_score", 0.0), reverse=True)
    return results
