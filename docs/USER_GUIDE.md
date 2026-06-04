# User Guide

This guide is for **operators and end users** of mem0-server: people who want to deploy it,
connect clients to it, and use it day to day. If you want to work on the code itself, see the
[Developer Guide](DEVELOPER_GUIDE.md).

- [What it is](#what-it-is)
- [How memory works](#how-memory-works)
- [Prerequisites](#prerequisites)
- [Configuration reference](#configuration-reference)
- [Choosing a deployment method](#choosing-a-deployment-method)
- [Deploying with Docker Compose](#deploying-with-docker-compose)
- [Deploying to CapRover](#deploying-to-caprover)
  - [1. Deploy the main app](#1-deploy-the-main-app-mem0-server)
  - [2. Deploy the backup app](#2-deploy-the-backup-app-mem0-backup)
- [Connecting clients](#connecting-clients)
  - [Claude Code](#claude-code)
  - [Claude Desktop](#claude-desktop)
  - [Claude.ai web / Cowork (OAuth)](#claudeai-web--cowork-oauth)
  - [ChatGPT (OAuth, Developer Mode)](#chatgpt-oauth-developer-mode)
  - [REST / curl / n8n](#rest--curl--n8n)
- [Prompting agents to use memory](#prompting-agents-to-use-memory)
  - [Claude (CLAUDE.md)](#claude-claudemd)
  - [ChatGPT (custom instructions)](#chatgpt-custom-instructions)
  - [Other agents (AGENTS.md and similar)](#other-agents-agentsmd-and-similar)
- [REST API reference](#rest-api-reference)
- [Backups and restore](#backups-and-restore)
- [Health and monitoring](#health-and-monitoring)
- [Troubleshooting](#troubleshooting)

## What it is

mem0-server is a self-hosted [mem0](https://github.com/mem0ai/mem0) memory store that you run as
a single service. It gives AI agents and scripts a shared, persistent long-term memory, reachable
two ways from one process:

- **REST API** under `/api/v1/memories…` — for scripts, n8n, curl, and any HTTP client.
- **Streamable HTTP MCP** under `/mcp` — for Claude Code, Claude Desktop, Claude.ai web, and Cowork.

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

- `agent_id` — a provenance tag for which agent/tool wrote it (e.g. `n8n-flow`, `claude-code`).
  Over MCP it is **write-only**: the `search`/`list` tools always span the whole store, so every
  connected agent (Claude Code, Codex, Claude.ai web, …) shares one memory. The REST API can still
  filter reads by `agent_id` for scripts that explicitly want a slice.
- `run_id` — a session or workflow run identifier.
- `metadata` — arbitrary JSON you attach to a memory.

`user_id` always defaults to `MEM0_DEFAULT_USER_ID`; you rarely need to set it.

## Prerequisites

Before deploying you need:

| Requirement | Why |
|---|---|
| **Docker** + Docker Compose, **or** a **CapRover** instance | Runs the app. See [Choosing a deployment method](#choosing-a-deployment-method). |
| A reachable **Qdrant** instance (with API key) | Vector backend that stores the memories. The Docker Compose method provides this for you. |
| An **Anthropic API key** | Default LLM for fact extraction. |
| An **OpenAI API key** | Default embedding model. |
| An **S3 bucket** + AWS credentials | Only for the nightly backup app (CapRover). |
| A **domain/subdomain** (e.g. `mem0.your-domain.com`) | Public HTTPS URL for clients and OAuth. Optional for a local Docker Compose run. |

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
| `MEM0_COLLECTION` | no | `memories` | Qdrant collection name. |
| `MEM0_DEFAULT_USER_ID` | yes | — | The single user, e.g. `default-user`. |
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
| `OAUTH_ALLOWED_REDIRECT_URIS` | no | claude.ai + cowork + chatgpt callbacks | Comma-separated allowlist for OAuth redirect URIs. An entry ending in `*` is a **path-prefix** match locked to an exact scheme + host — it must be a full `scheme://host/path/` prefix (e.g. `https://chatgpt.com/connector/oauth/*`). Host-only or bare wildcards (`https://chatgpt.com*`, `https://*`, `*`) are **ignored**, so a misconfigured entry can't match lookalike hosts like `chatgpt.com.evil.com`. |
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

## Choosing a deployment method

There are two supported ways to run mem0-server:

- **Docker Compose** — the simplest path if you don't already run CapRover. One `docker compose up`
  brings up **both Qdrant and the app** on a single host, with persistent volumes for each. You
  manage your own HTTPS (typically via a reverse proxy) and your own backups. Best for a single VM,
  a homelab, or local use.
- **CapRover** — best if you already operate a CapRover instance and want push-to-`main`
  auto-deploy plus the companion nightly S3 backup app. This method connects to an **existing,
  external** Qdrant.

The application is identical in both cases; only the surrounding infrastructure differs. The
sections below cover each.

## Deploying with Docker Compose

The repository ships a `docker-compose.yml` that runs Qdrant and the app together. You do **not**
need an external Qdrant for this method.

1. Copy the example environment file and fill in the secrets:

   ```bash
   cp .env.example .env
   ```

   At minimum set: `MEM0_API_KEY` (generate with `openssl rand -hex 32`), `QDRANT_API_KEY` (any
   strong secret — the bundled Qdrant is configured to require it), `ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`, and `MEM0_DEFAULT_USER_ID`.

   You can leave `QDRANT_HOST`, `QDRANT_PORT`, and `QDRANT_HTTPS` at their `.env.example` values —
   the compose file overrides them to point at the in-stack Qdrant service (`qdrant:6333`, no TLS
   on the internal network).

2. Bring up the stack:

   ```bash
   docker compose up -d
   ```

   This builds the app image from the root `Dockerfile`, starts Qdrant with a persistent
   `qdrant_data` volume, and starts the app on `http://localhost:8000`. The app's `/healthz`
   endpoint round-trips to Qdrant; once it returns `{"ok": true, ...}` the stack is ready.

3. Verify:

   ```bash
   curl http://localhost:8000/healthz
   ```

**HTTPS and public access.** The compose stack serves plain HTTP on port 8000. MCP clients and
OAuth require HTTPS, so for anything beyond local use put the app behind a reverse proxy
(Caddy, nginx, Traefik) that terminates TLS, and set `PUBLIC_BASE_URL` in `.env` to the public
HTTPS URL (e.g. `https://mem0.your-domain.com`). For Phase 2 OAuth, also set `OAUTH_SIGNING_KEY`
(see [Phases](#phases)).

**Backups.** The nightly S3 backup app is part of the CapRover setup. With Docker Compose you can
take Qdrant snapshots yourself against the bundled instance — see
[Backups and restore](#backups-and-restore) for the snapshot/restore API; the `qdrant_data` volume
also holds the on-disk data.

**Updating.** Pull the latest code and rebuild:

```bash
git pull
docker compose up -d --build
```

## Deploying to CapRover

This method connects to an **existing, external** Qdrant (it does not start one for you).
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
  https://mem0.your-domain.com/mcp \
  --header "Authorization: Bearer $MEM0_API_KEY"
```

After adding, the six memory tools (add/search/list/get/update/delete) become available in Claude
Code.

### Claude Desktop

Add an entry under the MCP servers section of Claude Desktop's config, pointing at
`https://mem0.your-domain.com/mcp` with an `Authorization: Bearer <token>` header (Streamable HTTP
transport). Restart Claude Desktop to pick it up. Both `/mcp` and `/mcp/` work; `/mcp` is the
canonical form.

### Claude.ai web / Cowork (OAuth)

This requires **Phase 2** (`OAUTH_SIGNING_KEY` set). In the client's connector settings:

1. Add a **custom connector** pointing at `https://mem0.your-domain.com/mcp`.
2. Leave the client ID and secret **blank** — the server supports Dynamic Client Registration, so
   the client registers itself automatically.
3. On the consent screen, **enter your `MEM0_API_KEY`** in the API key field and click
   **Authorize**, then let the redirect complete.

**Why the API key prompt matters (security):** this server is single-user and the consent step
authenticates *you* as the owner. Because the OAuth endpoints are public, anyone who knows the URL
could otherwise reach the consent screen; requiring `MEM0_API_KEY` at authorization ensures only the
holder of that key can mint an access token to your memories. Treat `MEM0_API_KEY` as the master
credential — anyone with it has full access via either the bearer header or the OAuth flow.

The server also only allows redirect URIs listed in `OAUTH_ALLOWED_REDIRECT_URIS`, which defaults to
the official claude.ai, Cowork, and ChatGPT callbacks.

### ChatGPT (OAuth, Developer Mode)

Also **Phase 2**. In ChatGPT, enable **Developer Mode**, add a custom connector pointing at
`https://mem0.your-domain.com/mcp`, and choose OAuth. On the consent screen enter your
`MEM0_API_KEY` and authorize.

ChatGPT's OAuth callback is a **per-connector** URL of the form
`https://chatgpt.com/connector/oauth/<connector-id>` — the `<connector-id>` is unique to each
connector you create. The default allowlist already covers these via the prefix entry
`https://chatgpt.com/connector/oauth/*`, so you don't need to add the exact URL. If you've
customized `OAUTH_ALLOWED_REDIRECT_URIS`, include that wildcard entry.

A trailing `*` is a **path-prefix** match, not a free-form glob: it is locked to the exact
scheme and host of the entry and only extends the path, so write the full
`scheme://host/path/` prefix (keep the trailing `/`). An entry without a concrete host *and*
path — `https://chatgpt.com*`, `https://*`, or a bare `*` — is ignored rather than honored, so
a typo can't accidentally allow a lookalike host such as `chatgpt.com.evil.com`.

### REST / curl / n8n

Send the bearer token as an `Authorization` header. See the [REST API reference](#rest-api-reference)
below.

## Prompting agents to use memory

Connecting a client only makes the memory tools *available* — it does not make the agent *use*
them. Models won't reliably search or save memory on their own; you have to tell them to. The most
durable way is to put a short instruction block in whatever file the agent reads at the start of
every session (`CLAUDE.md`, ChatGPT custom instructions, `AGENTS.md`, a system prompt, etc.).

A good memory instruction covers four behaviors:

1. **Recall first** — search memory at the start of a task, before answering, so past context is used.
2. **Save durable facts** — persist preferences, decisions, project conventions, and recurring
   context as they come up (not transient chatter).
3. **Update, don't duplicate** — when something changes, update the existing memory instead of
   adding a near-duplicate.
4. **Don't store secrets** — never save passwords, API keys, or sensitive personal data.

The server exposes six tools: `search_memories`, `add_memory`, `list_memories`, `get_memory`,
`update_memory`, `delete_memory`. Adjust the tool/connector names below to match how your client
surfaces them (for example, Claude Code namespaces them like `mcp__mem0-remote__search_memories`).

### Claude (CLAUDE.md)

For **Claude Code**, add this to the project's `CLAUDE.md` (or your user-level
`~/.claude/CLAUDE.md` to apply it everywhere). For **Claude Desktop**, paste the same text into a
Project's custom instructions.

```markdown
## Long-term memory (mem0)

You have a persistent memory store available through the mem0 MCP server. Use it in every session:

- **At the start of a task**, call `search_memories` with a query about the topic to recall any
  relevant preferences, decisions, or context before you respond.
- **When the user shares** a durable preference, decision, project convention, or fact they'll
  likely want recalled later, call `add_memory` to save it. Keep each memory a single clear fact.
- **When something changes**, find the existing memory (`search_memories` / `list_memories`) and
  `update_memory` it instead of adding a duplicate.
- Do **not** store secrets, credentials, or sensitive personal data.
- You don't need to announce routine memory operations; just use them naturally.
```

### ChatGPT (custom instructions)

In ChatGPT, open **Settings → Personalization → Custom instructions** (or a Project's
instructions) and add the following to the "How would you like ChatGPT to respond?" box. This
assumes you've connected the mem0 connector in Developer Mode (see
[ChatGPT (OAuth, Developer Mode)](#chatgpt-oauth-developer-mode)).

```text
I have a personal long-term memory store connected via the mem0 MCP connector. Use it every session:
- Before answering a substantive question, use the connector's search_memories tool to recall any
  relevant saved preferences, decisions, or context.
- When I share a durable preference, decision, or fact worth remembering, use add_memory to save it
  as a single clear statement.
- If something changes, update the existing memory rather than creating a duplicate.
- Never store passwords, API keys, or sensitive personal data.
```

### Other agents (AGENTS.md and similar)

Many coding agents and frameworks read an `AGENTS.md` (or an equivalent system-prompt/rules file)
at session start. Drop in a tool-agnostic version:

```markdown
## Memory

A shared long-term memory store is available via the mem0 MCP server. Behavior:

1. Recall: at the start of a task, search memory for context relevant to the request before acting.
2. Persist: save durable facts, preferences, decisions, and conventions as they arise.
3. Reconcile: update an existing memory when it changes; avoid near-duplicates.
4. Safety: never store secrets, credentials, or sensitive personal data.

Tools: search_memories, add_memory, list_memories, get_memory, update_memory, delete_memory.
```

If your agent has no instruction file but does take a system prompt, the same four numbered rules
work verbatim there.

## REST API reference

All endpoints live under `/api/v1` and require `Authorization: Bearer <MEM0_API_KEY>`. Request and
response bodies are JSON. `user_id` defaults to `MEM0_DEFAULT_USER_ID` if omitted.

### Add a memory — `POST /api/v1/memories`

Provide **either** `content` (a string) **or** `messages` (a chat transcript). Optional:
`agent_id`, `run_id`, `metadata`, `user_id`.

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "We host services on CapRover on DigitalOcean", "agent_id": "n8n-flow"}'
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
  -d '{"query": "where do we host things?"}'
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

## Importing existing data

A new memory store starts empty. To seed it from data you already have, the repo
ships standalone importer scripts under [`scripts/`](../scripts) that read common
export formats and POST them to the REST API. They're plain REST clients — run
them from a checkout against any reachable server.

| Source | Script | What it sends |
|---|---|---|
| ChatGPT export (`conversations.json`) | `scripts/import_chatgpt.py` | One `messages` payload per conversation |
| Obsidian vault (folder of `.md`) | `scripts/import_obsidian.py` | One memory per note (frontmatter stripped) |
| Readwise highlights (CSV export) | `scripts/import_readwise.py` | One memory per highlight (+ its note) |

All three take the same options: a `path` to the export, `--base-url`/`--api-key`
(default to `$MEM0_URL`/`$MEM0_API_KEY`), `--source` (provenance tag), `--limit`
(stop after N — good for a trial), and `--dry-run` (parse and report without
sending). Each imported memory is tagged `agent_id=import:<source>` and carries a
`source` (plus `title`/`path`/`book`/`author` where available) in its metadata, so
you can later tell imported memories apart from ones written during a session.

```bash
# 1. Preview without sending anything
python scripts/import_chatgpt.py ~/Downloads/conversations.json --dry-run

# 2. Trial run: import only the first 5
export MEM0_URL=https://mem0.your-domain.com
export MEM0_API_KEY=...
python scripts/import_obsidian.py ~/my-vault --limit 5

# 3. Full import
python scripts/import_readwise.py ~/Downloads/readwise.csv
```

**Cost note.** Every imported memory goes through the normal `add` path, which
invokes the fact-extraction LLM (see the
[Configuration reference](#configuration-reference)). A large ChatGPT or Obsidian import can mean
thousands of LLM calls — use `--dry-run` and `--limit` first to gauge volume.
mem0 also deduplicates semantically on add, so re-importing the same content
often results in no new memories.

> Requirements: Python 3.12 and the project's dependencies installed
> (`pip install -r requirements.txt`); the scripts add the repo root to
> `sys.path`, so no packaging step is needed.

## Backups and restore

The `mem0-backup` app handles nightly snapshots automatically (see
[deploy step 2](#2-deploy-the-backup-app-mem0-backup)). To **restore** from a snapshot:

```bash
# 1. Download a snapshot from S3
aws s3 cp s3://<bucket>/mem0-backups/2026-05-20T03-00-00Z.snapshot ./

# 2. Upload it to Qdrant
curl -X POST -H "api-key: $QDRANT_API_KEY" \
  -F "snapshot=@2026-05-20T03-00-00Z.snapshot" \
  "https://qdrant.your-domain.com/collections/memories/snapshots/upload"

# 3. Verify the collection is back
curl -H "api-key: $QDRANT_API_KEY" \
  "https://qdrant.your-domain.com/collections/memories"
```

Run a restore drill periodically so you know the snapshots are usable before you need them.

## Health and monitoring

- **`GET /healthz`** — does a real 2-second-timeout round-trip to Qdrant. Returns
  `{"ok": true, "version": "…", "qdrant": "reachable"}` on success, or HTTP 503 with
  `{"ok": false, "qdrant": "unreachable"}` if Qdrant can't be reached. CapRover uses this for its
  container healthcheck. No auth required.
- **`GET /metrics`** — Prometheus metrics: `http_requests_total` (labelled by method, matched route
  template, and status) and `http_request_duration_seconds` (labelled by method and matched route
  template). No auth required.

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
| Claude.ai web / Cowork can't connect | OAuth not enabled (`OAUTH_SIGNING_KEY` blank), or the client's redirect URI isn't in `OAUTH_ALLOWED_REDIRECT_URIS`. |
| "Couldn't reach the MCP server" on Claude.ai web / Cowork (but Claude Code/Desktop work) | OAuth discovery failure. Confirm `OAUTH_SIGNING_KEY` is set and `PUBLIC_BASE_URL` exactly matches the public HTTPS URL; the server must advertise the protected-resource metadata in the `/mcp/` 401 `WWW-Authenticate` header. |
| Connector fails right after consent; logs show `POST /oauth/register → 400` | The client's callback isn't in `OAUTH_ALLOWED_REDIRECT_URIS`. The server logs a `dcr_redirect_uri_rejected` warning with the exact `requested` URI and the active `allowed` list — add the requested URI to `OAUTH_ALLOWED_REDIRECT_URIS` and redeploy. Claude.ai web/desktop/mobile/Cowork use `https://claude.ai/api/mcp/auth_callback`. |
| Backup job not running | Check the backup container: `caprover logs mem0-backup`. |
