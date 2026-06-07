import pytest
from fastmcp import Client

from app.mcp_server import build_mcp

EXPECTED_TOOLS = {
    "add_memory",
    "search_memories",
    "list_memories",
    "get_memory",
    "update_memory",
    "delete_memory",
}


@pytest.fixture
def mcp():
    return build_mcp()


async def test_tools_registered(mcp):
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names


async def test_add_memory_tool(mcp, mem):
    mem.add.return_value = {"results": [{"id": "1"}]}
    async with Client(mcp) as client:
        await client.call_tool("add_memory", {"content": "remember this"})
    mem.add.assert_called_once()
    args, kwargs = mem.add.call_args
    assert args[0] == "remember this"
    assert kwargs["user_id"] == "default-user"
    assert "content_fp" in kwargs["metadata"]  # dedup fingerprint stored


async def test_add_memory_tool_deduplicates(mcp, mem):
    from types import SimpleNamespace

    mem.vector_store.list.return_value = ([SimpleNamespace(id="dup-1")], None)
    async with Client(mcp) as client:
        await client.call_tool("add_memory", {"content": "remember this"})
    mem.add.assert_not_called()  # exact repeat is skipped, no LLM extraction


async def test_search_memories_tool(mcp, mem):
    mem.search.return_value = {"results": []}
    async with Client(mcp) as client:
        await client.call_tool("search_memories", {"query": "what", "limit": 7})
    _, kwargs = mem.search.call_args
    # Reads are never scoped by agent_id: the store is shared across agents.
    assert kwargs["filters"] == {"user_id": "default-user"}
    assert kwargs["top_k"] == 7


async def test_read_tools_do_not_expose_agent_id(mcp):
    # Reads must not be scopable by agent_id, or a client's model can partition
    # the shared store and break cross-agent memory.
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("search_memories", "list_memories"):
        props = (tools[name].inputSchema or {}).get("properties", {})
        assert "agent_id" not in props, name


async def test_search_exposes_recency_weight(mcp):
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    props = (tools["search_memories"].inputSchema or {}).get("properties", {})
    assert "recency_weight" in props


async def test_search_with_recency_weight_invokes_mem(mcp, mem):
    mem.search.return_value = {"results": []}
    async with Client(mcp) as client:
        await client.call_tool("search_memories", {"query": "x", "recency_weight": 0.5})
    _, kwargs = mem.search.call_args
    assert kwargs["filters"] == {"user_id": "default-user"}
    assert kwargs["top_k"] == 10


async def test_list_memories_tool(mcp, mem):
    mem.get_all.return_value = {"results": []}
    async with Client(mcp) as client:
        await client.call_tool("list_memories", {})
    _, kwargs = mem.get_all.call_args
    assert kwargs["filters"] == {"user_id": "default-user"}


async def test_delete_memory_tool(mcp, mem):
    async with Client(mcp) as client:
        await client.call_tool("delete_memory", {"memory_id": "xyz"})
    mem.delete.assert_called_once_with(memory_id="xyz")
