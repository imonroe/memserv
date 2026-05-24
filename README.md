# mem0-server

A self-hosted [mem0](https://github.com/mem0ai/mem0) memory server that exposes one shared
memory store over **two protocols from a single process**:

- **REST** (`/api/v1/memories…`) for scripts, n8n, curl, and custom agents.
- **Streamable HTTP MCP** (`/mcp/`) for Claude Code, Claude Desktop, Claude.ai web, and Cowork.

It uses **Qdrant** as the vector backend and runs two ways: a **Docker Compose** stack that bundles
Qdrant and the app together, or on **CapRover** (auto-deploys on push to `main`) against an existing
external Qdrant, with a companion `mem0-backup` app that snapshots Qdrant to S3 nightly.
This is a **single-user** system: `MEM0_DEFAULT_USER_ID` is the only user.

**Documentation:**
- [User Guide](docs/USER_GUIDE.md) — deploy to CapRover, configure, connect clients, REST API, backups, troubleshooting.
- [Developer Guide](docs/DEVELOPER_GUIDE.md) — architecture, module map, auth, testing, how to make common changes.
- [`docs/PRD.md`](docs/PRD.md) — the full specification.

The sections below are a quick reference; the guides above go deeper.

## Architecture

```
Clients (Claude Code / Desktop / agents / curl)  ──Bearer──┐
Anthropic cloud (Claude.ai / Cowork)  ──OAuth 2.1 + PKCE──┐ │
                                                          ▼ ▼
                              CapRover app: mem0-server (this repo)
                                FastAPI + mounted FastMCP (one Memory instance)
                                          │ Qdrant API key
                                          ▼
                              CapRover app: qdrant (existing)
                                          │ snapshot API
                                          ▼
                              CapRover app: mem0-backup → S3 (nightly @ 03:00 UTC)
```

Phase 1 (MVP) uses a static bearer token. Phase 2 adds OAuth 2.1 + PKCE + Dynamic Client
Registration endpoints (enabled by setting `OAUTH_SIGNING_KEY`).

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio respx ruff

cp .env.example .env   # fill in values

ruff check app/
pytest -q

uvicorn app.main:app --reload --port 8000
```

Run a single test: `pytest tests/test_rest.py::test_add_memory`.

## Configuration

All config is via environment variables, validated by `app/config.py`. See `.env.example` for
the full list. Key ones:

| Variable | Notes |
|---|---|
| `QDRANT_HOST`, `QDRANT_API_KEY` | external Qdrant instance |
| `MEM0_DEFAULT_USER_ID` | the single user (e.g. `default-user`) |
| `MEM0_EMBED_DIMS` | **must** match the embedder's output dim (3-small=1536) |
| `MEM0_API_KEY` | static bearer token; `openssl rand -hex 32` |
| `PUBLIC_BASE_URL` | public URL, used in OAuth metadata |
| `OAUTH_SIGNING_KEY` | PEM RSA private key; setting it enables Phase 2 OAuth |

## Deploy with Docker Compose

The simplest way to self-host if you don't already run CapRover. The bundled
`docker-compose.yml` brings up **Qdrant and the app together** — no external
Qdrant required.

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY, OPENAI_API_KEY, MEM0_API_KEY, QDRANT_API_KEY
docker compose up -d
```

The compose file points the app at the in-stack Qdrant automatically (you don't
need to touch `QDRANT_HOST`/`QDRANT_PORT`/`QDRANT_HTTPS`). The server comes up at
`http://localhost:8000` — REST under `/api/v1`, MCP at `/mcp`. For Phase 2 OAuth,
set `OAUTH_SIGNING_KEY` and a public `PUBLIC_BASE_URL` in `.env` and put the stack
behind an HTTPS reverse proxy. See the
[User Guide](docs/USER_GUIDE.md#deploying-with-docker-compose) for details.

## Production deploy (CapRover)

1. Create the `mem0-server` app. Enable **Has Persistent Data** and map `/app/data`
   (used by the Phase 2 OAuth SQLite store).
2. Set Container HTTP Port to `8000` and configure all env vars from `.env.example`.
3. Under **Deployment → Method 3 (Deploy from GitHub)**, point at this repo / `main`.
   CapRover gives you a webhook URL; add it as a GitHub push webhook so merges auto-deploy.
4. Enable HTTPS + Force HTTPS.

Deploy the backup as a **separate** `mem0-backup` CapRover app from the `backup/` directory
(set its *Captain Definition Relative Path* to `./backup/captain-definition`). No exposed
ports; set `QDRANT_URL`, `QDRANT_API_KEY`, `MEM0_COLLECTION`, `S3_BUCKET`, AWS credentials,
and optional `S3_PREFIX` / `RETENTION_DAYS`.

## Client setup

**Claude Code:**

```bash
claude mcp add --scope user --transport http mem0-remote \
  https://mem0.your-domain.com/mcp/ \
  --header "Authorization: Bearer $MEM0_API_KEY"
```

**Direct REST:**

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "We deploy services with CapRover", "agent_id": "n8n-flow"}'

curl -X POST https://mem0.your-domain.com/api/v1/memories/search \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "how do we deploy services?"}'
```

**Claude.ai web / Cowork (Phase 2):** add a custom connector pointing at
`https://mem0.your-domain.com/mcp/`, leave client ID/secret blank (DCR registers
automatically), and complete the consent redirect.

**Make agents actually use it:** connecting a client only makes the memory tools available — add a
short instruction block to your `CLAUDE.md` / ChatGPT custom instructions / `AGENTS.md` so the
agent recalls and saves memory every session. Copy-paste snippets are in the
[User Guide](docs/USER_GUIDE.md#prompting-agents-to-use-memory).

A smoke test against a live server is in `scripts/smoke.sh`.

## Restore drill

```bash
# 1. Download a snapshot from S3
aws s3 cp s3://<bucket>/mem0-backups/2026-05-20T03-00-00Z.snapshot ./

# 2. Upload to Qdrant
curl -X POST -H "api-key: $QDRANT_API_KEY" \
  -F "snapshot=@2026-05-20T03-00-00Z.snapshot" \
  "https://qdrant.your-domain.com/collections/memories/snapshots/upload"

# 3. Verify
curl -H "api-key: $QDRANT_API_KEY" \
  "https://qdrant.your-domain.com/collections/memories"
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Search returns empty | `MEM0_EMBED_DIMS` doesn't match the Qdrant collection's vector size |
| 401 on MCP | missing/incorrect `Authorization: Bearer` header in client config |
| `Task group is not initialized` | FastMCP lifespan not wired into FastAPI (see `app/main.py`) |
| Snapshot job not running | `caprover logs mem0-backup` |
