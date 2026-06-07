from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

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
    limit: int = Field(default=10, ge=1, le=100)
    # Opt-in recency boost. 0 = pure semantic similarity (unchanged behavior),
    # 1 = order almost entirely by how recently a memory was created/updated.
    recency_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    recency_half_life_days: float = Field(default=30.0, gt=0.0)


class UpdateMemoryRequest(BaseModel):
    content: str


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


@router.post("/memories")
def add_memory(req: AddMemoryRequest) -> dict:
    if not req.content and not req.messages:
        raise HTTPException(status_code=422, detail="Provide either 'content' or 'messages'")
    payload = req.content if req.content is not None else [m.model_dump() for m in req.messages]
    kwargs = _scope_kwargs(req.user_id, req.agent_id, req.run_id)
    if req.metadata:
        kwargs["metadata"] = req.metadata
    return memory_mod.add_memory(payload, dedup=req.dedup, **kwargs)


@router.post("/memories/search")
def search_memories(req: SearchRequest) -> dict:
    memory = memory_mod.get_memory()
    filters = _scope_kwargs(req.user_id, req.agent_id, req.run_id)
    results = memory.search(query=req.query, filters=filters, top_k=req.limit)
    return rerank_by_recency(results, req.recency_weight, req.recency_half_life_days)


@router.get("/memories")
def list_memories(
    user_id: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    memory = memory_mod.get_memory()
    filters = _scope_kwargs(user_id, agent_id, run_id)
    return memory.get_all(filters=filters, top_k=limit)


@router.get("/memories/{memory_id}")
def get_memory_by_id(memory_id: str) -> dict:
    memory = memory_mod.get_memory()
    result = memory.get(memory_id=memory_id)
    if not result:
        raise HTTPException(status_code=404, detail="Memory not found")
    return result


@router.put("/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateMemoryRequest) -> dict:
    memory = memory_mod.get_memory()
    return memory.update(memory_id=memory_id, data=req.content)


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str) -> dict:
    memory = memory_mod.get_memory()
    memory.delete(memory_id=memory_id)
    return {"deleted": True, "memory_id": memory_id}


@router.get("/memories/{memory_id}/history")
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
