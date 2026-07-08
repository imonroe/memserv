import hashlib
import json
import threading
from datetime import UTC, datetime
from functools import lru_cache

import structlog

from app.config import Settings, get_settings
from app.metrics import observe_bulk_delete
from app.ranking import _parse_timestamp

_log = structlog.get_logger()


def _provider_config(
    provider: str,
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    embedding_dims: int | None = None,
) -> dict:
    """Build a mem0 provider `config` block, keyed to the provider.

    Cloud providers (anthropic/openai/…) get the API key injected explicitly:
    mem0's clients otherwise read it from os.environ, which is not populated when
    keys come only from a .env file via pydantic-settings. A local Ollama server
    takes no key — it needs its base URL under mem0's `ollama_base_url` key
    instead, plus the vector dimension for the embedder.
    """
    config: dict = {"model": model}
    if provider.strip().lower() == "ollama":
        if base_url:
            config["ollama_base_url"] = base_url
        if embedding_dims is not None:
            config["embedding_dims"] = embedding_dims
    elif api_key:
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
            "config": _provider_config(
                s.mem0_llm_provider,
                s.mem0_llm_model,
                api_key=s.anthropic_api_key,
                base_url=s.ollama_base_url,
            ),
        },
        "embedder": {
            "provider": s.mem0_embed_provider,
            "config": _provider_config(
                s.mem0_embed_provider,
                s.mem0_embed_model,
                api_key=s.openai_api_key,
                base_url=s.ollama_base_url,
                embedding_dims=s.mem0_embed_dims,
            ),
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


# Tri-state cache for the full-text index on the `data` payload field:
# None = not attempted yet, True = created/confirmed, False = creation failed
# (old Qdrant, permissions) — don't retry on every search, use the scan path.
_keyword_index_state: bool | None = None
_keyword_index_lock = threading.Lock()


def reset_keyword_index_state() -> None:
    """Forget whether the keyword index exists (test hook / ops escape hatch)."""
    global _keyword_index_state
    with _keyword_index_lock:
        _keyword_index_state = None


def _ensure_keyword_index(memory) -> bool:
    """Idempotently create the full-text index on `data`; cache the outcome.

    Locked so concurrent first searches (FastAPI runs sync endpoints in a
    threadpool) attempt creation exactly once — without it, a transient failure
    in a racing duplicate attempt could overwrite a successful True with False.
    """
    global _keyword_index_state
    with _keyword_index_lock:
        if _keyword_index_state is None:
            from qdrant_client import models

            try:
                memory.vector_store.client.create_payload_index(
                    collection_name=memory.vector_store.collection_name,
                    field_name="data",
                    field_schema=models.TextIndexParams(
                        type=models.TextIndexType.TEXT,
                        tokenizer=models.TokenizerType.WORD,
                        lowercase=True,
                    ),
                )
                _keyword_index_state = True
            except Exception:
                _keyword_index_state = False
        return _keyword_index_state


def _indexed_keyword_points(memory, query: str, filters: dict, scan_limit: int) -> list:
    """Fetch candidate points server-side via the full-text index.

    Qdrant's MatchText requires every (lowercased) query token to appear in the
    document, so this transfers only plausible candidates instead of the whole
    store. It is a prefilter, not the final answer — substring verification
    still happens in keyword_search().
    """
    from qdrant_client import models

    conditions = [
        models.FieldCondition(key=key, match=models.MatchValue(value=value))
        for key, value in filters.items()
    ]
    conditions.append(
        models.FieldCondition(key="data", match=models.MatchText(text=query))
    )
    points, _ = memory.vector_store.client.scroll(
        collection_name=memory.vector_store.collection_name,
        scroll_filter=models.Filter(must=conditions),
        limit=scan_limit,
        with_payload=True,
        with_vectors=False,
    )
    return points or []


def _scanned_points(memory, filters: dict, scan_limit: int) -> list:
    """Legacy path: pull up to scan_limit payloads and match client-side."""
    result = memory.vector_store.list(filters=filters or None, top_k=scan_limit)
    points = result[0] if isinstance(result, tuple) else result
    return list(points or [])


def _substring_matches(points: list, needle: str, limit: int) -> list[dict]:
    matches = [
        point
        for point in points
        if isinstance((getattr(point, "payload", None) or {}).get("data"), str)
        and needle in point.payload["data"].casefold()
    ]
    matches.sort(key=_point_recency, reverse=True)  # most recently touched first
    return [_point_to_result(p) for p in matches[:limit]]


def keyword_search(
    query: str,
    *,
    user_id: str | None = None,
    limit: int = 10,
    scan_limit: int = DEFAULT_KEYWORD_SCAN_LIMIT,
    extra_filters: dict | None = None,
) -> dict:
    """Case-insensitive substring search over stored memory text.

    A literal-match fallback for terms semantic search misses (names, IDs, URLs,
    rare tokens). Matching is done in two stages so the store isn't shipped over
    the network on every query:

    1. Indexed prefilter: a full-text payload index on `data` (created lazily,
       idempotent) lets Qdrant return only points containing every query token.
    2. Substring verification: the original case-insensitive substring check
       runs over the candidates, preserving exact semantics (e.g. phrase
       contiguity).

    If the index is unavailable, the indexed query fails, or it yields no
    surviving matches (a mid-token fragment like "hil" never token-matches),
    the legacy full scan of up to `scan_limit` payloads runs instead, so recall
    is never worse than before. Scoped by `user_id` only (it spans the whole
    user store, like the MCP read tools); `extra_filters` adds exact-match
    payload conditions (e.g. provenance fields). An empty/whitespace query
    matches nothing. Fail-open: any store error returns no results.
    """
    needle = query.strip().casefold()
    if not needle:
        return {"results": []}
    memory = get_memory()
    filters: dict = {}
    if user_id:
        filters["user_id"] = user_id
    if extra_filters:
        filters.update(extra_filters)
    if _ensure_keyword_index(memory):
        try:
            candidates = _indexed_keyword_points(
                memory, query.strip(), filters, scan_limit
            )
        except Exception:
            candidates = []
        results = _substring_matches(candidates, needle, limit)
        if results:
            return {"results": results}
    try:
        points = _scanned_points(memory, filters, scan_limit)
    except Exception:
        return {"results": []}
    return {"results": _substring_matches(points, needle, limit)}


# Hard ceiling on deletions per bulk_delete call. Bounds request duration (each
# delete is a mem0 round-trip) so a huge purge can't outlive the reverse proxy's
# timeout; callers loop on has_more instead.
BULK_DELETE_MAX = 1000


def bulk_delete(*, filters: dict, confirm: bool = False, max_delete: int = BULK_DELETE_MAX) -> dict:
    """Delete (or, by default, just count) every memory matching exact filters.

    Dry-run by default: with confirm=False nothing is deleted; the response
    reports how many memories matched and a small sample so the caller can
    verify the blast radius before re-posting with confirm=true.

    Deletions go through mem0's Memory.delete per ID — never a raw vector-store
    filter delete — so mem0's history/bookkeeping stays consistent. At most
    `max_delete` memories are removed per call; `has_more` tells the caller to
    loop. The caller is responsible for ensuring `filters` is meaningfully
    scoped (see the REST endpoint).
    """
    memory = get_memory()
    result = memory.vector_store.list(filters=filters, top_k=max_delete + 1)
    points = result[0] if isinstance(result, tuple) else result
    points = list(points or [])
    has_more = len(points) > max_delete
    points = points[:max_delete]
    sample = [
        {"id": item["id"], "memory": item.get("memory")}
        for item in (_point_to_result(p) for p in points[:10])
    ]
    response = {
        "matched": len(points),
        "deleted": 0,
        "dry_run": not confirm,
        "has_more": has_more,
        "sample": sample,
    }
    if not confirm:
        return response
    deleted = 0
    error: str | None = None
    for point in points:
        point_id = getattr(point, "id", None)
        if point_id is None:
            continue
        try:
            memory.delete(memory_id=point_id)
        except Exception:
            # Report partial progress instead of losing the response: deletes
            # are idempotent, so the caller just re-posts until has_more is
            # false. The traceback is logged below.
            error = "delete_failed_partway"
            _log.exception("bulk_delete_item_failed", memory_id=point_id)
            break
        deleted += 1
    observe_bulk_delete(deleted)
    _log.warning(
        "bulk_delete", filters=filters, matched=len(points), deleted=deleted,
        has_more=has_more, error=error,
    )
    response["deleted"] = deleted
    if error:
        response["error"] = error
        response["has_more"] = True  # the remainder was not attempted
    return response


# Upper bound on the list-pagination offset. Offset paging is emulated by
# over-fetching offset+limit+1 items and slicing (mem0's get_all has no native
# offset), so the bound caps the per-request fetch size. 10k is far beyond a
# single-user store's realistic page depth.
MAX_LIST_OFFSET = 10_000


def list_paginated(*, filters: dict, limit: int, offset: int = 0) -> dict:
    """List memories one page at a time, preserving mem0's result shaping.

    Fetches `offset + limit + 1` items via mem0's get_all (which has no offset
    parameter) and slices: the extra item only signals `has_more`, so a page is
    never silently truncated at the store's default cap. Ordering comes from
    the vector store's scroll (stable by point ID, not chronological) — pages
    are consistent across calls as long as the store isn't being written
    concurrently.
    """
    memory = get_memory()
    raw = memory.get_all(filters=filters, top_k=offset + limit + 1)
    items = raw.get("results") if isinstance(raw, dict) else raw
    items = list(items or [])
    has_more = len(items) > offset + limit
    return {
        "results": items[offset : offset + limit],
        "pagination": {"limit": limit, "offset": offset, "has_more": has_more},
    }


def _result_expiry(item) -> datetime | None:
    """Parse an `expires_at` from a result item (top-level or nested metadata)."""
    if not isinstance(item, dict):
        return None
    ts = _parse_timestamp(item.get("expires_at"))
    if ts is None and isinstance(item.get("metadata"), dict):
        ts = _parse_timestamp(item["metadata"].get("expires_at"))
    return ts


def drop_expired(results: dict, now: datetime | None = None) -> dict:
    """Remove memories whose `expires_at` is at/before `now` from a results dict.

    Supports the provenance convention's expiry field so stale facts can be
    filtered out of reads. Items without a (parseable) `expires_at` are kept.
    Anything not shaped like ``{"results": [...]}`` is returned unchanged.
    """
    if not isinstance(results, dict):
        return results
    items = results.get("results")
    if not isinstance(items, list):
        return results
    now = now or datetime.now(UTC)
    results["results"] = [
        item for item in items if (exp := _result_expiry(item)) is None or exp > now
    ]
    return results
