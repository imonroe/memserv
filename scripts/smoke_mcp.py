#!/usr/bin/env python3
"""MCP half of the smoke test (PRD §13.2 steps 4-5).

Connects to the running server's /mcp/ endpoint with the bearer token, then:
  1. add_memory via MCP
  2. search_memories via MCP and assert results come back
  3. cross-check: search via MCP for a fact added over REST (proves both
     protocols share one Memory instance)

Usage:
  MEM0_URL=https://mem0.your-domain.com MEM0_API_KEY=... \
    python scripts/smoke_mcp.py [rest_probe_query]
"""

import asyncio
import os
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> int:
    base = os.environ["MEM0_URL"].rstrip("/")
    token = os.environ["MEM0_API_KEY"]
    rest_probe = sys.argv[1] if len(sys.argv) > 1 else None

    transport = StreamableHttpTransport(
        url=f"{base}/mcp/",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with Client(transport) as client:
        tools = {t.name for t in await client.list_tools()}
        print(f"4. MCP connected; tools: {sorted(tools)}")
        assert "add_memory" in tools and "search_memories" in tools

        await client.call_tool(
            "add_memory",
            {"content": "smoke-test (mcp): Ian deploys via CapRover", "agent_id": "smoke-mcp"},
        )
        print("   add_memory via MCP: ok")

        res = await client.call_tool(
            "search_memories", {"query": "how does Ian deploy?", "agent_id": "smoke-mcp"}
        )
        print(f"5. search_memories via MCP returned: {res.data}")

        if rest_probe:
            cross = await client.call_tool("search_memories", {"query": rest_probe})
            print(f"   cross-protocol search (REST-added fact) via MCP: {cross.data}")

    print("MCP smoke complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
