from fastmcp import FastMCP

from app import memory as memory_mod
from app.auth import build_verifier
from app.config import get_settings


def build_mcp() -> FastMCP:
    s = get_settings()
    mcp = FastMCP("mem0-server", auth=build_verifier())
    memory = memory_mod.get_memory()
    default_user = s.mem0_default_user_id

    @mcp.tool
    def add_memory(content: str, agent_id: str | None = None, metadata: dict | None = None) -> dict:
        """Store a fact or observation in long-term memory.

        Use when the user shares preferences, project context, decisions,
        or anything they may want recalled in future conversations.
        """
        kwargs: dict = {"user_id": default_user}
        if agent_id:
            kwargs["agent_id"] = agent_id
        if metadata:
            kwargs["metadata"] = metadata
        return memory.add(content, **kwargs)

    @mcp.tool
    def search_memories(query: str, agent_id: str | None = None, limit: int = 10) -> dict:
        """Search long-term memory by semantic similarity."""
        filters: dict = {"user_id": default_user}
        if agent_id:
            filters["agent_id"] = agent_id
        return memory.search(query=query, filters=filters, top_k=limit)

    @mcp.tool
    def list_memories(agent_id: str | None = None) -> dict:
        """List all stored memories for the current user."""
        filters: dict = {"user_id": default_user}
        if agent_id:
            filters["agent_id"] = agent_id
        return memory.get_all(filters=filters)

    @mcp.tool
    def get_memory(memory_id: str) -> dict:
        """Fetch a single memory by ID."""
        return memory.get(memory_id=memory_id)

    @mcp.tool
    def update_memory(memory_id: str, content: str) -> dict:
        """Replace the content of an existing memory."""
        return memory.update(memory_id=memory_id, data=content)

    @mcp.tool
    def delete_memory(memory_id: str) -> dict:
        """Permanently delete a memory."""
        memory.delete(memory_id=memory_id)
        return {"deleted": True, "memory_id": memory_id}

    return mcp
