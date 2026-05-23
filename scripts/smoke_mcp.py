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
        missing = {"add_memory", "search_memories"} - tools
        if missing:
            print(f"FAIL: required MCP tools missing: {sorted(missing)}")
            return 1

        await client.call_tool(
            "add_memory",
            {"content": "smoke-test (mcp): Ian deploys via CapRover", "agent_id": "smoke-mcp"},
        )
        print("   add_memory via MCP: ok")

        res = await client.call_tool(
            "search_memories", {"query": "how does Ian deploy?", "agent_id": "smoke-mcp"}
        )
        print(f"5. search_memories via MCP returned: {res.data}")
        if not _results(res):
            print("FAIL: MCP search_memories returned no results")
            return 1

        if rest_probe:
            # Cross-protocol check: a fact added over REST (agent_id=smoke) must
            # be findable over MCP, proving both protocols share one Memory.
            cross = await client.call_tool(
                "search_memories", {"query": rest_probe, "agent_id": "smoke"}
            )
            print(f"   cross-protocol search (REST-added fact) via MCP: {cross.data}")
            if not _results(cross):
                print("FAIL: cross-protocol search returned no results")
                return 1

    print("MCP smoke complete.")
    return 0


def _results(call_result) -> list:
    data = getattr(call_result, "data", None)
    if isinstance(data, dict):
        return data.get("results") or []
    return data or []


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
