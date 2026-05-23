from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import memory as memory_mod
from app.auth import require_bearer
from app.config import get_settings

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


class SearchRequest(BaseModel):
    query: str
    user_id: str | None = None
    agent_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


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
    memory = memory_mod.get_memory()
    payload = req.content if req.content is not None else [m.model_dump() for m in req.messages]
    kwargs = _scope_kwargs(req.user_id, req.agent_id, req.run_id)
    if req.metadata:
        kwargs["metadata"] = req.metadata
    return memory.add(payload, **kwargs)


@router.post("/memories/search")
def search_memories(req: SearchRequest) -> dict:
    memory = memory_mod.get_memory()
    kwargs = _scope_kwargs(req.user_id, req.agent_id)
    kwargs["limit"] = req.limit
    return memory.search(query=req.query, **kwargs)


@router.get("/memories")
def list_memories(user_id: str | None = None, agent_id: str | None = None, limit: int = 50) -> dict:
    memory = memory_mod.get_memory()
    kwargs = _scope_kwargs(user_id, agent_id)
    kwargs["limit"] = limit
    return memory.get_all(**kwargs)


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
