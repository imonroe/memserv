#!/bin/bash
# Integration smoke test against a running mem0-server.
# Usage: MEM0_URL=https://mem0.your-domain.com MEM0_API_KEY=... ./scripts/smoke.sh
set -euo pipefail

: "${MEM0_URL:?set MEM0_URL}"
: "${MEM0_API_KEY:?set MEM0_API_KEY}"

AUTH="Authorization: Bearer $MEM0_API_KEY"
JSON="Content-Type: application/json"

echo "1. /healthz"
curl -fsS "$MEM0_URL/healthz" && echo

echo "2. Add a memory via REST"
curl -fsS -X POST "$MEM0_URL/api/v1/memories" \
  -H "$AUTH" -H "$JSON" \
  -d '{"content": "smoke-test: the team hosts services on CapRover", "agent_id": "smoke"}' && echo

echo "3. Search for it via REST"
curl -fsS -X POST "$MEM0_URL/api/v1/memories/search" \
  -H "$AUTH" -H "$JSON" \
  -d '{"query": "where are services hosted?", "agent_id": "smoke"}' && echo

echo "4. List memories"
curl -fsS "$MEM0_URL/api/v1/memories?agent_id=smoke" -H "$AUTH" && echo

echo "5-6. MCP add + search (cross-checks REST-added fact via MCP)"
python3 "$(dirname "$0")/smoke_mcp.py" "where are services hosted?"

echo "Smoke test complete. Review output above; clean up test memories via DELETE if desired."
