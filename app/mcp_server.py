from fastmcp import FastMCP

from app import memory as memory_mod
from app.auth import build_verifier
from app.config import get_settings
from app.ranking import rerank_by_recency


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

        agent_id is an optional provenance tag recording which agent wrote the
        memory. It does NOT partition the store — search and list always span
        every memory for the user, so all connected agents share one memory.

        Submitting the same content again is automatically deduplicated and
        skips re-processing, so it's safe to call without checking first.
        """
        kwargs: dict = {"user_id": default_user}
        if agent_id:
            kwargs["agent_id"] = agent_id
        if metadata:
            kwargs["metadata"] = metadata
        return memory_mod.add_memory(content, **kwargs)

    @mcp.tool
    def search_memories(
        query: str, limit: int = 10, recency_weight: float = 0.0, mode: str = "semantic"
    ) -> dict:
        """Search long-term memory.

        Searches the single shared memory store for the user, across all agents.

        mode: "semantic" (default) ranks by meaning/similarity. Use "keyword" for
        a case-insensitive substring match when you need an exact term the
        semantic search may miss — a name, identifier, URL, or rare token.

        recency_weight (0.0-1.0, semantic mode only) optionally biases results
        toward more recently created or updated memories. Leave it at 0 for pure
        relevance; raise it (e.g. 0.3) when the user asks what is *latest*.
        """
        if mode not in ("semantic", "keyword"):
            raise ValueError(f"mode must be 'semantic' or 'keyword', got {mode!r}")
        if mode == "keyword":
            return memory_mod.keyword_search(query, user_id=default_user, limit=limit)
        results = memory.search(query=query, filters={"user_id": default_user}, top_k=limit)
        return rerank_by_recency(results, recency_weight)

    @mcp.tool
    def list_memories() -> dict:
        """List all stored memories for the user (shared across all agents)."""
        return memory.get_all(filters={"user_id": default_user})

    def _not_found(memory_id: str) -> dict:
        # MCP tools return a structured error instead of raising, so the model
        # sees a usable signal rather than an opaque tool exception.
        return {"error": "not_found", "memory_id": memory_id}

    @mcp.tool
    def get_memory(memory_id: str) -> dict:
        """Fetch a single memory by ID.

        Returns {"error": "not_found", ...} if no memory has that ID.
        """
        result = memory.get(memory_id=memory_id)
        # Explicit None check: a falsy-but-present result (e.g. {}) is a found
        # memory, not a miss.
        return _not_found(memory_id) if result is None else result

    @mcp.tool
    def update_memory(memory_id: str, content: str) -> dict:
        """Replace the content of an existing memory.

        Returns {"error": "not_found", ...} if no memory has that ID.
        """
        if not memory.get(memory_id=memory_id):
            return _not_found(memory_id)
        return memory.update(memory_id=memory_id, data=content)

    @mcp.tool
    def delete_memory(memory_id: str) -> dict:
        """Permanently delete a memory.

        Returns {"error": "not_found", ...} if no memory has that ID.
        """
        if not memory.get(memory_id=memory_id):
            return _not_found(memory_id)
        memory.delete(memory_id=memory_id)
        return {"deleted": True, "memory_id": memory_id}

    return mcp
