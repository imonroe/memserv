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
    assert kwargs["user_id"] == "ian"


async def test_delete_memory_tool(mcp, mem):
    async with Client(mcp) as client:
        await client.call_tool("delete_memory", {"memory_id": "xyz"})
    mem.delete.assert_called_once_with(memory_id="xyz")
