from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import memory as memory_mod
from app.auth import require_bearer
from app.config import get_settings
from app.ranking import rerank_by_recency

router = APIRouter(dependencies=[Depends(require_bearer)])


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class AddMemoryRequest(BaseModel):
    content: str | None = None
    messages: list[Message] | None = None
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    metadata: dict | None = None
    # When true (default), content already stored is skipped before mem0's LLM
    # fact-extraction runs. Matching is on a normalized fingerprint (case-
    # insensitive, whitespace-collapsed), not raw bytes. Set false to force
    # re-extraction.
    dedup: bool = True


class SearchRequest(BaseModel):
    query: str
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    # Default 15 (not the list default of 50): sized so broad/exploratory
    # queries get enough context without the caller having to tune it, while a
    # narrow lookup can pass a small limit. Atomic-fact memories make a larger
    # limit cheap; under-fetching a broad query costs more than over-fetching.
    limit: int = Field(default=15, ge=1, le=100)
    # "semantic" (default, vector similarity) or "keyword" (case-insensitive
    # substring match for exact terms semantic search misses).
    mode: Literal["semantic", "keyword"] = "semantic"
    # Opt-in recency boost (semantic mode only). 0 = pure semantic similarity
    # (unchanged), 1 = order almost entirely by how recently a memory was touched.
    recency_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    recency_half_life_days: float = Field(default=30.0, gt=0.0)
    # Provenance/review-metadata filters (exact match); see the metadata convention.
    source: str | None = None
    confidence: str | None = None
    review_status: str | None = None
    exclude_expired: bool = False


class UpdateMemoryRequest(BaseModel):
    content: str


class BulkDeleteRequest(BaseModel):
    # Exact-match filters; at least one besides user_id is required, so a bare
    # POST can never wipe the whole store.
    agent_id: str | None = None
    run_id: str | None = None
    source: str | None = None
    confidence: str | None = None
    review_status: str | None = None
    user_id: str | None = None
    # False (default) = dry run: count + sample only, nothing deleted.
    confirm: bool = False


# --- Response models ---------------------------------------------------------
# These document and validate the *stable* parts of mem0's payloads without
# freezing them: extra="allow" passes unexpected mem0 fields through untouched,
# and every route sets response_model_exclude_unset=True so fields mem0 didn't
# send aren't fabricated as nulls — the wire format stays exactly what mem0
# returned, but /docs and client generators get a real schema.


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    memory: str | None = None
    hash: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    score: float | None = None
    metadata: dict | None = None


class MemoryResults(BaseModel):
    model_config = ConfigDict(extra="allow")

    results: list[MemoryItem] = Field(default_factory=list)


class AddMemoryResponse(MemoryResults):
    # Set when the add was skipped because identical content already exists.
    deduplicated: bool | None = None
    memory_id: str | None = None


class UpdateResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    # mem0's update() returns a success message, not the updated item.
    message: str | None = None


class DeleteResponse(BaseModel):
    deleted: bool
    memory_id: str


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    history: list = Field(default_factory=list)


def _provenance_filters(
    source: str | None, confidence: str | None, review_status: str | None
) -> dict:
    """Exact-match payload filters for the provenance/review metadata convention."""
    pairs = (("source", source), ("confidence", confidence), ("review_status", review_status))
    return {key: value for key, value in pairs if value}


def _scope_kwargs(
    user_id: str | None, agent_id: str | None = None, run_id: str | None = None
) -> dict:
    s = get_settings()
    kwargs: dict = {"user_id": user_id or s.mem0_default_user_id}
    if agent_id:
        kwargs["agent_id"] = agent_id
    if run_id:
        kwargs["run_id"] = run_id
    return kwargs


@router.post(
    "/memories", response_model=AddMemoryResponse, response_model_exclude_unset=True
)
def add_memory(req: AddMemoryRequest) -> dict:
    if not req.content and not req.messages:
        raise HTTPException(status_code=422, detail="Provide either 'content' or 'messages'")
    payload = req.content if req.content is not None else [m.model_dump() for m in req.messages]
    kwargs = _scope_kwargs(req.user_id, req.agent_id, req.run_id)
    if req.metadata:
        kwargs["metadata"] = req.metadata
    return memory_mod.add_memory(payload, dedup=req.dedup, **kwargs)


@router.post(
    "/memories/search", response_model=MemoryResults, response_model_exclude_unset=True
)
def search_memories(req: SearchRequest) -> dict:
    prov = _provenance_filters(req.source, req.confidence, req.review_status)
    if req.mode == "keyword":
        results = memory_mod.keyword_search(
            req.query,
            user_id=_scope_kwargs(req.user_id)["user_id"],
            limit=req.limit,
            extra_filters=prov or None,
        )
    else:
        filters = {**_scope_kwargs(req.user_id, req.agent_id, req.run_id), **prov}
        memory = memory_mod.get_memory()
        raw = memory.search(query=req.query, filters=filters, top_k=req.limit)
        results = rerank_by_recency(raw, req.recency_weight, req.recency_half_life_days)
    return memory_mod.drop_expired(results) if req.exclude_expired else results


@router.get(
    "/memories", response_model=MemoryResults, response_model_exclude_unset=True
)
def list_memories(
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    source: str | None = None,
    confidence: str | None = None,
    review_status: str | None = None,
    exclude_expired: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=memory_mod.MAX_LIST_OFFSET),
) -> dict:
    filters = {
        **_scope_kwargs(user_id, agent_id, run_id),
        **_provenance_filters(source, confidence, review_status),
    }
    results = memory_mod.list_paginated(filters=filters, limit=limit, offset=offset)
    # Expiry filtering happens after pagination, so a page may carry fewer than
    # `limit` items; `pagination.has_more` still reflects the unfiltered store.
    return memory_mod.drop_expired(results) if exclude_expired else results


@router.post("/memories/delete_bulk")
def delete_bulk(req: BulkDeleteRequest) -> dict:
    """Delete every memory matching the given filters; dry-run by default.

    A POST (not DELETE) for the same reason search is: it takes a JSON body,
    and DELETE request bodies are ambiguous across proxies/clients.
    """
    prov = _provenance_filters(req.source, req.confidence, req.review_status)
    if not (req.agent_id or req.run_id or prov):
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide at least one filter (agent_id, run_id, source, "
                "confidence, review_status). Deleting the entire store is not "
                "supported through this endpoint; if you really mean to start "
                "over, drop the Qdrant collection instead."
            ),
        )
    filters = {**_scope_kwargs(req.user_id, req.agent_id, req.run_id), **prov}
    return memory_mod.bulk_delete(filters=filters, confirm=req.confirm)


def _get_or_404(memory, memory_id: str) -> dict:
    result = memory.get(memory_id=memory_id)
    if not result:
        raise HTTPException(status_code=404, detail="Memory not found")
    return result


@router.get(
    "/memories/{memory_id}",
    response_model=MemoryItem,
    response_model_exclude_unset=True,
)
def get_memory_by_id(memory_id: str) -> dict:
    return _get_or_404(memory_mod.get_memory(), memory_id)


@router.put(
    "/memories/{memory_id}",
    response_model=UpdateResponse,
    response_model_exclude_unset=True,
)
def update_memory(memory_id: str, req: UpdateMemoryRequest) -> dict:
    memory = memory_mod.get_memory()
    # Depending on the mem0 version, update() on a missing id either raises or
    # silently no-ops; pre-checking makes it a 404 like GET.
    _get_or_404(memory, memory_id)
    return memory.update(memory_id=memory_id, data=req.content)


@router.delete("/memories/{memory_id}", response_model=DeleteResponse)
def delete_memory(memory_id: str) -> dict:
    memory = memory_mod.get_memory()
    _get_or_404(memory, memory_id)
    memory.delete(memory_id=memory_id)
    return {"deleted": True, "memory_id": memory_id}


@router.get(
    "/memories/{memory_id}/history",
    response_model=HistoryResponse,
    response_model_exclude_unset=True,
)
def memory_history(memory_id: str) -> dict:
    memory = memory_mod.get_memory()
    return {"history": memory.history(memory_id=memory_id)}


async def check_qdrant() -> bool:
    s = get_settings()
    scheme = "https" if s.qdrant_https else "http"
    url = f"{scheme}://{s.qdrant_host}:{s.qdrant_port}/collections"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url, headers={"api-key": s.qdrant_api_key})
            return resp.status_code == 200
    except Exception:
        return False
