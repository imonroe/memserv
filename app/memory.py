import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache

from app.config import Settings, get_settings
from app.ranking import _parse_timestamp


def _provider_config(model: str, api_key: str | None) -> dict:
    config = {"model": model}
    # mem0's provider clients otherwise read the key from os.environ, which is
    # not populated when keys come only from a .env file via pydantic-settings.
    if api_key:
        config["api_key"] = api_key
    return config


def _build_config(s: Settings) -> dict:
    # mem0's Qdrant store has no `https` flag; it only honors scheme via `url`.
    # Build a scheme-aware URL so an HTTPS Qdrant on 443 isn't hit over plain HTTP.
    scheme = "https" if s.qdrant_https else "http"
    qdrant_url = f"{scheme}://{s.qdrant_host}:{s.qdrant_port}"
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": s.mem0_collection,
                "url": qdrant_url,
                "api_key": s.qdrant_api_key,
                "embedding_model_dims": s.mem0_embed_dims,
            },
        },
        "llm": {
            "provider": s.mem0_llm_provider,
            "config": _provider_config(s.mem0_llm_model, s.anthropic_api_key),
        },
        "embedder": {
            "provider": s.mem0_embed_provider,
            "config": _provider_config(s.mem0_embed_model, s.openai_api_key),
        },
        "version": "v1.1",
    }


@lru_cache
def get_memory():
    # Imported lazily so tests can mock without a real mem0/Qdrant install.
    from mem0 import Memory

    return Memory.from_config(_build_config(get_settings()))


def _normalize_text(text: str) -> str:
    # Lowercase and collapse all runs of whitespace (incl. newlines/tabs) to a
    # single space, so trivial formatting differences fingerprint the same.
    return " ".join(text.split()).lower()


def content_fingerprint(content) -> str:
    """A deterministic fingerprint of the raw add() input, for cheap dedup.

    Normalizes case and whitespace so trivial formatting differences fingerprint
    the same, then SHA-256s the result. For a message transcript, each message's
    role and text are normalized individually (so whitespace/case differences in
    the text don't defeat dedup) before hashing.
    """
    if isinstance(content, str):
        normalized = _normalize_text(content)
    elif isinstance(content, list):
        parts = []
        for message in content:
            if isinstance(message, dict):
                role = str(message.get("role", "")).strip().lower()
                parts.append(f"{role}\x1f{_normalize_text(str(message.get('content', '')))}")
            else:
                parts.append(_normalize_text(str(message)))
        normalized = "\x1e".join(parts)
    else:
        normalized = _normalize_text(json.dumps(content, sort_keys=True))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _existing_fingerprint_id(memory, fingerprint: str, user_id: str | None) -> str | None:
    """Return the id of an already-stored memory with this fingerprint, or None.

    Best-effort: the fingerprint is matched against the `content_fp` payload
    field via the vector store's filter. Any error (store quirk, transient
    failure) returns None so the dedup check never blocks a write — it only ever
    saves work, never prevents it.
    """
    filters: dict = {"content_fp": fingerprint}
    if user_id:
        filters["user_id"] = user_id
    try:
        result = memory.vector_store.list(filters=filters, top_k=1)
    except Exception:
        return None
    # mem0's Qdrant store returns a (points, next_offset) tuple; normalize that
    # and a bare-list return to the points list.
    points = result[0] if isinstance(result, tuple) else result
    if not points:
        return None
    return getattr(points[0], "id", None)


def add_memory(content, *, dedup: bool = True, **kwargs) -> dict:
    """Add a memory, optionally skipping mem0's LLM extraction for exact repeats.

    With `dedup` on (default), a normalized SHA-256 fingerprint of the raw
    content is computed and stored in metadata as `content_fp`. If a memory with
    the same fingerprint already exists for the user, the add is skipped — no LLM
    fact-extraction call — and a `{"deduplicated": True}` marker is returned.
    Pass `dedup=False` to force a normal add (e.g. to re-extract).
    """
    memory = get_memory()
    if not dedup:
        return memory.add(content, **kwargs)
    fingerprint = content_fingerprint(content)
    existing_id = _existing_fingerprint_id(memory, fingerprint, kwargs.get("user_id"))
    if existing_id is not None:
        return {"results": [], "deduplicated": True, "memory_id": existing_id}
    metadata = dict(kwargs.pop("metadata", None) or {})
    metadata["content_fp"] = fingerprint
    return memory.add(content, metadata=metadata, **kwargs)


# Upper bound on how many of the user's memories a keyword search scans in one
# pass. Generous for a single-user store; keyword search is a literal-match
# fallback, not the primary retrieval path.
DEFAULT_KEYWORD_SCAN_LIMIT = 5000


def _point_to_result(point) -> dict:
    """Shape a Qdrant point into a search-result dict (memory text + payload)."""
    payload = dict(getattr(point, "payload", None) or {})
    # Drop internal plumbing that shouldn't surface in results.
    payload.pop("text_lemmatized", None)  # BM25 helper
    payload.pop("content_fp", None)  # dedup fingerprint
    memory_text = payload.pop("data", None)
    return {"id": getattr(point, "id", None), "memory": memory_text, **payload}


def _point_recency(point) -> datetime:
    """Sort key for keyword results: updated_at (preferred) or created_at, parsed."""
    payload = getattr(point, "payload", None) or {}
    ts = _parse_timestamp(payload.get("updated_at")) or _parse_timestamp(payload.get("created_at"))
    return ts or datetime.min.replace(tzinfo=UTC)


def keyword_search(
    query: str,
    *,
    user_id: str | None = None,
    limit: int = 10,
    scan_limit: int = DEFAULT_KEYWORD_SCAN_LIMIT,
) -> dict:
    """Case-insensitive substring search over stored memory text.

    A literal-match fallback for terms semantic search misses (names, IDs, URLs,
    rare tokens). Scans up to `scan_limit` of the user's memories via the vector
    store's payload listing and matches `query` as a case-insensitive substring
    of each memory's text, returning the most recent matches first. Scoped by
    `user_id` only (it spans the whole user store, like the MCP read tools).
    An empty/whitespace query matches nothing. Fail-open: any store error
    returns no results.
    """
    needle = query.strip().casefold()
    if not needle:
        return {"results": []}
    memory = get_memory()
    filters = {"user_id": user_id} if user_id else None
    try:
        result = memory.vector_store.list(filters=filters, top_k=scan_limit)
    except Exception:
        return {"results": []}
    points = result[0] if isinstance(result, tuple) else result
    matches = [
        point
        for point in (points or [])
        if isinstance((getattr(point, "payload", None) or {}).get("data"), str)
        and needle in point.payload["data"].casefold()
    ]
    matches.sort(key=_point_recency, reverse=True)  # most recently touched first
    return {"results": [_point_to_result(p) for p in matches[:limit]]}
