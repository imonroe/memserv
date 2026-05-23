# User Guide

This guide is for **operators and end users** of mem0-server: people who want to deploy it,
connect clients to it, and use it day to day. If you want to work on the code itself, see the
[Developer Guide](DEVELOPER_GUIDE.md).

- [What it is](#what-it-is)
- [How memory works](#how-memory-works)
- [Prerequisites](#prerequisites)
- [Configuration reference](#configuration-reference)
- [Deploying to CapRover](#deploying-to-caprover)
  - [1. Deploy the main app](#1-deploy-the-main-app-mem0-server)
  - [2. Deploy the backup app](#2-deploy-the-backup-app-mem0-backup)
- [Connecting clients](#connecting-clients)
  - [Claude Code](#claude-code)
  - [Claude Desktop](#claude-desktop)
  - [Claude.ai web / Cowork (OAuth)](#claudeai-web--cowork-oauth)
  - [REST / curl / n8n](#rest--curl--n8n)
- [REST API reference](#rest-api-reference)
- [Backups and restore](#backups-and-restore)
- [Health and monitoring](#health-and-monitoring)
- [Troubleshooting](#troubleshooting)

## What it is

mem0-server is a self-hosted [mem0](https://github.com/mem0ai/mem0) memory store that you run as
a single service. It gives AI agents and scripts a shared, persistent long-term memory, reachable
two ways from one process:

- **REST API** under `/api/v1/memories…` — for scripts, n8n, curl, and any HTTP client.
- **Streamable HTTP MCP** under `/mcp/` — for Claude Code, Claude Desktop, Claude.ai web, and Cowork.

Both protocols read and write the **same** memory store, so a fact you save from Claude Code is
searchable from a curl script and vice versa.

It is **single-user by design**: there is exactly one user, set by `MEM0_DEFAULT_USER_ID`. There is
no multi-tenant separation — anyone holding the API token has full access to the one memory store.

## How memory works

When you add a memory, mem0 uses an **LLM** (Anthropic Claude by default) to extract durable facts
from your text, then stores each fact as a vector **embedding** (OpenAI by default) in **Qdrant**.
Searches are semantic: you ask in natural language and get back the most similar stored facts, not
keyword matches.

Memories can optionally be tagged with:

- `agent_id` — which agent/tool wrote it (e.g. `n8n-flow`, `claude-code`), so you can filter later.
- `run_id` — a session or workflow run identifier.
- `metadata` — arbitrary JSON you attach to a memory.

`user_id` always defaults to `MEM0_DEFAULT_USER_ID`; you rarely need to set it.

## Prerequisites

Before deploying you need:

| Requirement | Why |
|---|---|
| A **CapRover** instance | Hosts the app; deploys on push to `main`. |
| A reachable **Qdrant** instance (with API key) | Vector backend that stores the memories. |
| An **Anthropic API key** | Default LLM for fact extraction. |
| An **OpenAI API key** | Default embedding model. |
| An **S3 bucket** + AWS credentials | Only for the nightly backup app. |
| A **domain/subdomain** (e.g. `mem0.your-domain.com`) | Public HTTPS URL for clients and OAuth. |

You can swap the LLM/embedder providers (see [Configuration reference](#configuration-reference)),
but the defaults above are the supported path.

> **Critical:** `MEM0_EMBED_DIMS` must match the embedding model's real output dimension
> (`text-embedding-3-small` = 1536, `text-embedding-3-large` = 3072). A mismatch causes **silent**
> empty search results, not an error. Changing the embedding model later requires dropping and
> recreating the Qdrant collection.

## Configuration reference

All configuration is via environment variables, validated at startup by `app/config.py`. The
service refuses to start if a required variable is missing. Copy `.env.example` to `.env` for local
runs, or set these in the CapRover app's **App Configs** panel for production.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `QDRANT_HOST` | yes | — | Qdrant hostname, e.g. `qdrant.your-domain.com`. |
| `QDRANT_PORT` | no | `443` | Qdrant port. |
| `QDRANT_HTTPS` | no | `true` | Use HTTPS to reach Qdrant. |
| `QDRANT_API_KEY` | yes | — | Qdrant API key. |
| `MEM0_COLLECTION` | no | `ian_memories` | Qdrant collection name. |
| `MEM0_DEFAULT_USER_ID` | yes | — | The single user, e.g. `ian`. |
| `MEM0_LLM_PROVIDER` | no | `anthropic` | LLM provider for fact extraction. |
| `MEM0_LLM_MODEL` | no | `claude-haiku-4-5-20251001` | LLM model. |
| `ANTHROPIC_API_KEY` | if provider=anthropic | — | Required when the LLM provider is Anthropic. |
| `MEM0_EMBED_PROVIDER` | no | `openai` | Embedding provider. |
| `MEM0_EMBED_MODEL` | no | `text-embedding-3-small` | Embedding model. |
| `MEM0_EMBED_DIMS` | no | `1536` | **Must** match the embedder's real dimension. |
| `OPENAI_API_KEY` | if provider=openai | — | Required when the embed provider is OpenAI. |
| `MEM0_API_KEY` | yes | — | Static bearer token protecting REST + MCP. Generate with `openssl rand -hex 32`. |
| `PUBLIC_BASE_URL` | yes | — | Public URL, e.g. `https://mem0.your-domain.com`. Used in OAuth metadata. |
| `OAUTH_SIGNING_KEY` | no | empty | PEM RSA private key. **Setting this enables Phase 2 OAuth.** Leave blank for Phase 1. |
| `OAUTH_ALLOWED_REDIRECT_URIS` | no | claude.ai + cowork callbacks | Comma-separated allowlist for OAuth redirect URIs. |
| `LOG_LEVEL` | no | `INFO` | Log level. |

### Phases

- **Phase 1 (MVP)** — static bearer token only. Leave `OAUTH_SIGNING_KEY` blank. Works with Claude
  Code, Claude Desktop, curl, n8n — anything that can send an `Authorization: Bearer` header.
- **Phase 2 (OAuth)** — set `OAUTH_SIGNING_KEY` to a PEM RSA private key. This turns on OAuth 2.1 +
  PKCE + Dynamic Client Registration endpoints so **Claude.ai web** and **Cowork** can connect.
  The static bearer token keeps working alongside OAuth.

Generate an OAuth signing key with:

```bash
openssl genrsa 2048
```

When pasting a multi-line PEM into a single env var, replace newlines with `\n` — the app converts
`\n` back to real newlines at load time.

## Deploying to CapRover

Deployment is **push-to-`main` → CapRover webhook**. Merging to `main` triggers a rebuild and
redeploy automatically, independent of CI status.

### 1. Deploy the main app (`mem0-server`)

1. In CapRover, create a new app named `mem0-server`. Enable **Has Persistent Data** and map a
   volume to `/app/data` (used by the Phase 2 OAuth SQLite store; harmless in Phase 1).
2. Open **App Configs** and set every required variable from the
   [Configuration reference](#configuration-reference). At minimum: `QDRANT_HOST`,
   `QDRANT_API_KEY`, `MEM0_DEFAULT_USER_ID`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
   `MEM0_API_KEY`, `PUBLIC_BASE_URL`.
3. Set **Container HTTP Port** to `8000`.
4. Under **Deployment → Method 3 (Deploy from GitHub/Bitbucket/GitLab)**, point at this repository
   and the `main` branch. CapRover gives you a webhook URL — add it as a GitHub **push** webhook on
   the repo so merges to `main` auto-deploy.
5. Under **HTTP Settings**, enable **HTTPS** and **Force HTTPS**, and attach your domain
   (e.g. `mem0.your-domain.com`). This domain must match `PUBLIC_BASE_URL`.

The repository root `captain-definition` and `Dockerfile` build the image. The container runs
`uvicorn app.main:app --workers 2` and exposes a `/healthz` healthcheck.

### 2. Deploy the backup app (`mem0-backup`)

The nightly Qdrant→S3 backup is a **separate** CapRover app built from the `backup/` directory in
this same repository.

1. Create a second CapRover app named `mem0-backup`. It needs **no exposed ports**.
2. Set its **Captain Definition Relative Path** to `./backup/captain-definition`.
3. Point its deployment at this repo / `main` (same webhook pattern, or deploy manually).
4. Set its env vars: `QDRANT_URL` (e.g. `https://qdrant.your-domain.com`), `QDRANT_API_KEY`,
   `MEM0_COLLECTION`, `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optionally
   `S3_PREFIX` (default `mem0-backups`), `AWS_DEFAULT_REGION` (default `us-east-1`), and
   `RETENTION_DAYS` (default `14`).

The backup container runs `crond` and executes `backup/backup.sh` nightly at 03:00 UTC. Each run
creates a Qdrant snapshot, downloads it, uploads it to S3, deletes the Qdrant-side snapshot, keeps
the 3 most recent local files, and prunes S3 objects older than `RETENTION_DAYS`.

## Connecting clients

All clients authenticate with the same `MEM0_API_KEY` bearer token (Phase 1), except Claude.ai web
and Cowork, which use OAuth (Phase 2).

### Claude Code

```bash
claude mcp add --scope user --transport http mem0-remote \
  https://mem0.your-domain.com/mcp/ \
  --header "Authorization: Bearer $MEM0_API_KEY"
```

After adding, the six memory tools (add/search/list/get/update/delete) become available in Claude
Code.

### Claude Desktop

Add an entry under the MCP servers section of Claude Desktop's config, pointing at
`https://mem0.your-domain.com/mcp/` with an `Authorization: Bearer <token>` header (Streamable HTTP
transport). Restart Claude Desktop to pick it up.

### Claude.ai web / Cowork (OAuth)

This requires **Phase 2** (`OAUTH_SIGNING_KEY` set). In the client's connector settings:

1. Add a **custom connector** pointing at `https://mem0.your-domain.com/mcp/`.
2. Leave the client ID and secret **blank** — the server supports Dynamic Client Registration, so
   the client registers itself automatically.
3. Complete the consent screen (click **Authorize**) and the redirect back to the client.

The server only allows redirect URIs listed in `OAUTH_ALLOWED_REDIRECT_URIS`, which defaults to the
official claude.ai and Cowork callback URLs.

### REST / curl / n8n

Send the bearer token as an `Authorization` header. See the [REST API reference](#rest-api-reference)
below.

## REST API reference

All endpoints live under `/api/v1` and require `Authorization: Bearer <MEM0_API_KEY>`. Request and
response bodies are JSON. `user_id` defaults to `MEM0_DEFAULT_USER_ID` if omitted.

### Add a memory — `POST /api/v1/memories`

Provide **either** `content` (a string) **or** `messages` (a chat transcript). Optional:
`agent_id`, `run_id`, `metadata`, `user_id`.

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "Ian hosts services on CapRover on DigitalOcean", "agent_id": "n8n-flow"}'
```

With a transcript instead of a plain string:

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "I prefer dark mode"}]}'
```

### Search memories — `POST /api/v1/memories/search`

Semantic search. Optional `agent_id`, `run_id`, `user_id`, and `limit` (1–100, default 10).

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories/search \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "where does Ian host things?"}'
```

### List memories — `GET /api/v1/memories`

Query params: `agent_id`, `run_id`, `user_id`, `limit` (1–100, default 50).

```bash
curl https://mem0.your-domain.com/api/v1/memories?limit=20 \
  -H "Authorization: Bearer $MEM0_API_KEY"
```

### Get one — `GET /api/v1/memories/{memory_id}`

Returns 404 if the memory does not exist.

### Update — `PUT /api/v1/memories/{memory_id}`

Body: `{"content": "new text"}`.

### Delete — `DELETE /api/v1/memories/{memory_id}`

Returns `{"deleted": true, "memory_id": "…"}`.

### History — `GET /api/v1/memories/{memory_id}/history`

Returns the change history for a memory.

A ready-made smoke test against a live server is in [`scripts/smoke.sh`](../scripts/smoke.sh), and
an MCP-level smoke test in [`scripts/smoke_mcp.py`](../scripts/smoke_mcp.py).

## Backups and restore

The `mem0-backup` app handles nightly snapshots automatically (see
[deploy step 2](#2-deploy-the-backup-app-mem0-backup)). To **restore** from a snapshot:

```bash
# 1. Download a snapshot from S3
aws s3 cp s3://<bucket>/mem0-backups/2026-05-20T03-00-00Z.snapshot ./

# 2. Upload it to Qdrant
curl -X POST -H "api-key: $QDRANT_API_KEY" \
  -F "snapshot=@2026-05-20T03-00-00Z.snapshot" \
  "https://qdrant.your-domain.com/collections/ian_memories/snapshots/upload"

# 3. Verify the collection is back
curl -H "api-key: $QDRANT_API_KEY" \
  "https://qdrant.your-domain.com/collections/ian_memories"
```

Run a restore drill periodically so you know the snapshots are usable before you need them.

## Health and monitoring

- **`GET /healthz`** — does a real 2-second-timeout round-trip to Qdrant. Returns
  `{"ok": true, "version": "…", "qdrant": "reachable"}` on success, or HTTP 503 with
  `{"ok": false, "qdrant": "unreachable"}` if Qdrant can't be reached. CapRover uses this for its
  container healthcheck. No auth required.
- **`GET /metrics`** — Prometheus metrics: `http_requests_total` and `http_request_duration_seconds`,
  labelled by method, matched route template, and status. No auth required.

Every request is logged as structured JSON (via `structlog`) with a `request_id`, method, path,
status, and latency. The `Authorization` header is never logged.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Search returns empty, no error | `MEM0_EMBED_DIMS` doesn't match the Qdrant collection's vector size. Recreate the collection with the correct dimension. |
| 401 on REST or MCP | Missing or wrong `Authorization: Bearer` token. Confirm it equals `MEM0_API_KEY`. |
| `Task group is not initialized` on first MCP request | FastMCP lifespan not wired into FastAPI — a code/deploy regression. See `app/main.py`. |
| 503 from `/healthz` | Qdrant is unreachable. Check `QDRANT_HOST`/`QDRANT_PORT`/`QDRANT_HTTPS`/`QDRANT_API_KEY`. |
| Server won't start | A required env var is missing or a provider key is absent. Check the startup logs; `app/config.py` names the missing variable. |
| Claude.ai web can't connect | OAuth not enabled (`OAUTH_SIGNING_KEY` blank), or the client's redirect URI isn't in `OAUTH_ALLOWED_REDIRECT_URIS`. |
| Backup job not running | Check the backup container: `caprover logs mem0-backup`. |
