# User Guide

This guide is for **operators and end users** of mem0-server: people who want to deploy it,
connect clients to it, and use it day to day. If you want to work on the code itself, see the
[Developer Guide](DEVELOPER_GUIDE.md).

- [What it is](#what-it-is)
- [How memory works](#how-memory-works)
- [Prerequisites](#prerequisites)
- [Configuration reference](#configuration-reference)
  - [Running fully local with Ollama](#running-fully-local-with-ollama)
- [Choosing a deployment method](#choosing-a-deployment-method)
- [Deploying with Docker Compose](#deploying-with-docker-compose)
- [Putting it behind a reverse proxy (HTTPS)](#putting-it-behind-a-reverse-proxy-https)
  - [Nginx Proxy Manager](#nginx-proxy-manager)
  - [Caddy](#caddy)
  - [Verifying the proxy](#verifying-the-proxy)
- [Deploying to CapRover](#deploying-to-caprover)
  - [1. Deploy the main app](#1-deploy-the-main-app-mem0-server)
  - [2. Deploy the backup app](#2-deploy-the-backup-app-mem0-backup)
  - [3. Deploy the digest app (optional)](#3-deploy-the-digest-app-optional-mem0-digest)
  - [4. Deploy the capture bot (optional)](#4-deploy-the-capture-bot-optional-mem0-capture)
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
- [Automating memory with Claude Code hooks](#automating-memory-with-claude-code-hooks)
  - [Prerequisites](#hooks-prerequisites)
  - [Recall: inject memories on every prompt](#recall-inject-memories-on-every-prompt)
  - [Capture: save the conversation at session end](#capture-save-the-conversation-at-session-end)
  - [Wiring the hooks up](#wiring-the-hooks-up)
  - [Hooks vs. prompting](#hooks-vs-prompting)
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

**Re-adding the same content is free.** Before that LLM extraction runs, the server fingerprints the
content (normalized: lowercased and whitespace-collapsed, so differences in case or spacing still
match); if it matches something already stored, the add is skipped (no LLM call) and the response is
`{"results": [], "deduplicated": true, "memory_id": "…"}`. This makes re-runs of imports and
webhook/n8n retries cheap and idempotent. Pass `"dedup": false` on a REST add to force
re-extraction. (This is distinct from mem0's *semantic* dedup, which still applies when
similar-but-not-identical content reaches the LLM.)

Memories can optionally be tagged with:

- `agent_id` — a provenance tag for which agent/tool wrote it (e.g. `n8n-flow`, `claude-code`).
  Over MCP it is **write-only**: the `search`/`list` tools always span the whole store, so every
  connected agent (Claude Code, Codex, Claude.ai web, …) shares one memory. The REST API can still
  filter reads by `agent_id` for scripts that explicitly want a slice.
- `run_id` — a session or workflow run identifier.
- `metadata` — arbitrary JSON you attach to a memory.

`user_id` always defaults to `MEM0_DEFAULT_USER_ID`; you rarely need to set it.

### Provenance and review metadata (convention)

For "governed" agent memory — distinguishing trusted from untrusted, fresh from
stale — there's a small **convention** of reserved `metadata` keys. They're just
ordinary metadata (nothing enforces them), but the REST read endpoints can filter
on them, and agents can reason about them:

| Key | Recommended values | Meaning |
|---|---|---|
| `source` | free-form, e.g. `user`, `agent`, `import:chatgpt`, `capture:telegram`, `tool:n8n` | Where the memory came from. The import scripts and capture bot already set this. |
| `confidence` | `high`, `medium`, `low`, `unknown` | How much to trust the content. |
| `review_status` | `unreviewed`, `approved`, `rejected`, `stale` | Whether a human/agent has vetted it. |
| `reviewed_by` / `reviewed_at` | free-form / ISO 8601 | Who vetted it and when (optional). |
| `expires_at` | ISO 8601 timestamp | When the fact should stop being trusted. |

`confidence` and `review_status` are **independent** — a memory can be
high-confidence but unreviewed, or approved but deliberately low-confidence.
`expires_at` addresses the common agent-memory failure mode where *old memory
stays trusted after the world changed*: set it on facts that age out, then pass
`exclude_expired=true` on reads (below) to drop them. This is a flat convention
layered on `metadata`; the existing top-level `agent_id` remains the writer tag.

Set them on any write, e.g.:

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"content": "Q3 OKRs are finalized", "agent_id": "claude-code",
       "metadata": {"source": "user", "confidence": "high",
                    "review_status": "approved", "expires_at": "2026-10-01T00:00:00Z"}}'
```

Filter reads by them with the query params on `GET /api/v1/memories` (and the same
fields on search) — see the [REST API reference](#rest-api-reference). Per the
shared-store design, the **MCP** read tools never filter by these (they span the
whole store); metadata filtering is a REST-only affordance for scripts.

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
| `MEM0_LLM_PROVIDER` | no | `anthropic` | LLM provider for fact extraction. `anthropic`, `openai`, or `ollama` (local — see [Running fully local with Ollama](#running-fully-local-with-ollama)). |
| `MEM0_LLM_MODEL` | no | `claude-haiku-4-5-20251001` | LLM model. For Ollama, e.g. `llama3.1:8b`. |
| `ANTHROPIC_API_KEY` | if using Anthropic | — | Required when Anthropic is the LLM (or embed) provider. |
| `MEM0_EMBED_PROVIDER` | no | `openai` | Embedding provider. `openai` or `ollama` (local). |
| `MEM0_EMBED_MODEL` | no | `text-embedding-3-small` | Embedding model. For Ollama, e.g. `nomic-embed-text`. |
| `MEM0_EMBED_DIMS` | no | `1536` | **Must** match the embedder's real dimension (Ollama: `nomic-embed-text`=768, `mxbai-embed-large`=1024). |
| `OPENAI_API_KEY` | if using OpenAI | — | Required when OpenAI is the embed or LLM provider. |
| `OLLAMA_BASE_URL` | no | `http://localhost:11434` | Ollama server URL; used when either provider is `ollama`. No API key needed. |
| `MEM0_API_KEY` | yes | — | Static bearer token protecting REST + MCP. Generate with `openssl rand -hex 32`. |
| `PUBLIC_BASE_URL` | yes | — | Public URL, e.g. `https://mem0.your-domain.com`. Used in OAuth metadata. |
| `OAUTH_SIGNING_KEY` | no | empty | PEM RSA private key. **Setting this enables Phase 2 OAuth.** Leave blank for Phase 1. |
| `OAUTH_ALLOWED_REDIRECT_URIS` | no | claude.ai + cowork + chatgpt callbacks | Comma-separated allowlist for OAuth redirect URIs. An entry ending in `*` is a **path-prefix** match locked to an exact scheme + host — it must be a full `scheme://host/path/` prefix (e.g. `https://chatgpt.com/connector/oauth/*`). Host-only or bare wildcards (`https://chatgpt.com*`, `https://*`, `*`) are **ignored**, so a misconfigured entry can't match lookalike hosts like `chatgpt.com.evil.com`. |
| `TRUST_FORWARDED_FOR` | no | `true` | Use the first `X-Forwarded-For` hop as the client IP for rate limiting. Correct behind CapRover's nginx; set to `false` if the app is exposed directly (no reverse proxy), where the header would be attacker-controlled. |
| `RATE_LIMIT_AUTH_FAILURES` | no | `10` | Failed bearer-token attempts (REST + MCP, per surface) allowed per IP per window before 429s. `0` disables. |
| `RATE_LIMIT_AUTH_WINDOW_SECONDS` | no | `60` | Window for the above. |
| `RATE_LIMIT_CONSENT_FAILURES` | no | `5` | Failed OAuth consent (wrong API key) attempts per IP per window. `0` disables. |
| `RATE_LIMIT_CONSENT_WINDOW_SECONDS` | no | `300` | Window for the above. |
| `RATE_LIMIT_TOKEN_FAILURES` | no | `10` | Failed `/oauth/token` exchanges per IP per window. `0` disables. |
| `RATE_LIMIT_TOKEN_WINDOW_SECONDS` | no | `60` | Window for the above. |
| `LOG_LEVEL` | no | `INFO` | Log level. |

#### Rate limiting

Failed authentication attempts are rate-limited per client IP to slow down brute-force guessing of
`MEM0_API_KEY` (and OAuth codes). Only **failures** count — normal authenticated traffic is never
throttled — but once an IP crosses the limit, *all* its requests to that surface (even with the
correct token) get **HTTP 429** with a `Retry-After` header until the window expires. The four
surfaces (REST `/api/v1/...`, MCP `/mcp`, OAuth consent, OAuth token) are limited independently.
`/healthz` and `/metrics` are never limited. Limits are per uvicorn worker (the default image runs
2 workers), so the effective ceiling is about twice the configured value. If you lock yourself out
during testing, wait out the window or restart the app.

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

### Running fully local with Ollama

By default memserv extracts facts with Anthropic and embeds with OpenAI — both are cloud APIs that
cost money per call and see your memory content. You can instead point the **LLM**, the
**embedder**, or **both** at a local [Ollama](https://ollama.com/) server for **zero per-call cost**
and **no content leaving the host** — a natural fit for a self-hosted single-user store. It's
**opt-in**; the default stays `anthropic` + `openai`.

Set the provider(s) to `ollama` and give the models and server URL:

```bash
# Fully local — nothing leaves the host, no API keys needed.
OLLAMA_BASE_URL=http://localhost:11434
MEM0_LLM_PROVIDER=ollama
MEM0_LLM_MODEL=llama3.1:8b
MEM0_EMBED_PROVIDER=ollama
MEM0_EMBED_MODEL=nomic-embed-text
MEM0_EMBED_DIMS=768              # nomic-embed-text is 768-dim — see the warning below
```

Pull the models on the Ollama host first (`ollama pull llama3.1:8b`,
`ollama pull nomic-embed-text`). When a provider is `ollama` its API key (`ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`) is **not required** — memserv only enforces a provider's key when that provider is
actually selected. You can also mix and match: a local Ollama LLM with cloud OpenAI embeddings (keep
`OPENAI_API_KEY`), or cloud Anthropic extraction with local embeddings, whatever balances cost,
privacy, and quality for you.

> **Critical — `MEM0_EMBED_DIMS` must match the Ollama embed model's real output dimension.**
> `nomic-embed-text` is **768**, `mxbai-embed-large` is **1024** (the OpenAI defaults are 1536/3072).
> A mismatch causes **silent** empty search results, not an error. The dimension is baked into the
> Qdrant collection at creation, so **switching embed models (or moving between OpenAI and Ollama)
> means dropping and recreating the collection** — you can't change it in place. Start on Ollama from
> an empty store, or re-import after recreating the collection. This is the same
> [invariant](#prerequisites) that applies to any embed-model change.

**Performance note.** Local models run on your hardware. Fact extraction (`add`) makes an LLM call,
so on a CPU-only box a busy automation pipeline may feel slow; a GPU helps a lot. Embedding models
are lighter. If cost/privacy matter more than latency, local is a good trade; if you need snappy
high-volume writes and don't mind the bill, the cloud defaults are faster.

**Bundled Ollama with Docker Compose.** The `docker-compose.yml` ships a commented-out `ollama`
service. Uncomment it (and the `ollama_data` volume), set `OLLAMA_BASE_URL=http://ollama:11434` (the
Compose **service name**, not `localhost`) and the provider vars in `.env`, then `docker compose up
-d` and pull the models into the container:

```bash
docker compose exec ollama ollama pull llama3.1:8b
docker compose exec ollama ollama pull nomic-embed-text
```

See [Deploying with Docker Compose](#deploying-with-docker-compose). On CapRover, run Ollama as its
own app (or on a reachable host) and set `OLLAMA_BASE_URL` to its URL.

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
OAuth require HTTPS, so for anything beyond local use put the app behind a reverse proxy that
terminates TLS, and set `PUBLIC_BASE_URL` in `.env` to the public HTTPS URL
(e.g. `https://mem0.your-domain.com`). For Phase 2 OAuth, also set `OAUTH_SIGNING_KEY`
(see [Phases](#phases)). See
[Putting it behind a reverse proxy (HTTPS)](#putting-it-behind-a-reverse-proxy-https) for
copy-paste setups with **Nginx Proxy Manager** and **Caddy**.

**Backups.** The nightly S3 backup app is part of the CapRover setup. With Docker Compose you can
take Qdrant snapshots yourself against the bundled instance — see
[Backups and restore](#backups-and-restore) for the snapshot/restore API; the `qdrant_data` volume
also holds the on-disk data.

**Updating.** Pull the latest code and rebuild:

```bash
git pull
docker compose up -d --build
```

## Putting it behind a reverse proxy (HTTPS)

If you run the [Docker Compose](#deploying-with-docker-compose) stack on a homelab box or a VPS and
want to reach it from the internet, put a reverse proxy in front of it. The proxy gets you three
things the app deliberately does **not** do itself:

- **TLS termination** — a real Let's Encrypt certificate, so the public URL is `https://`. MCP
  clients (Claude Code/Desktop/web, ChatGPT) and OAuth (Phase 2) refuse plain HTTP.
- **A stable public hostname** that you point `PUBLIC_BASE_URL` at.
- **A single front door** you can add access logging, fail2ban, or IP allowlisting to.

The app listens on plain HTTP port `8000` and expects the proxy to handle HTTPS. Whatever proxy you
pick, three things matter for this app specifically:

1. **Set `PUBLIC_BASE_URL` to the public HTTPS URL** in `.env` and restart the stack
   (`docker compose up -d`). The OAuth metadata and the MCP `resource` URI are derived from it; a
   mismatch breaks strict OAuth clients. Do **not** add a trailing slash.
2. **Don't buffer responses and use a generous read timeout.** The MCP endpoint (`/mcp`) uses
   Streamable HTTP and can hold a response open while the server streams. Proxies that buffer the
   whole body or time out at ~30–60s will make MCP appear to hang. Disable response buffering and
   allow long-lived reads (e.g. 1 hour).
3. **Forward the original host and scheme** (`Host`, `X-Forwarded-Proto: https`) so generated URLs
   stay on your public origin.

> **Don't publish Qdrant.** Only proxy the app (port `8000`). The bundled Qdrant has no published
> ports in `docker-compose.yml` — leave it that way. Nothing outside the Compose network should be
> able to reach Qdrant directly.

The two examples below assume DNS for `mem0.your-domain.com` points at the host running the proxy,
and that the proxy can reach the app at `http://<app-host>:8000` (on the same Docker network, the
service name `mem0-server:8000`; on the host, `127.0.0.1:8000`).

### Nginx Proxy Manager

[Nginx Proxy Manager](https://nginxproxymanager.com/) (NPM) is a popular web-UI wrapper around nginx
+ Let's Encrypt that many homelabbers already run.

If you want NPM and mem0-server on the same Docker network, add the proxy to the Compose project (or
attach both to a shared external network) so NPM can reach the app by service name. A minimal NPM
service you can drop into `docker-compose.yml`:

```yaml
  nginx-proxy-manager:
    image: jc21/nginx-proxy-manager:latest
    restart: unless-stopped
    ports:
      - "80:80"     # HTTP (Let's Encrypt challenges + redirect)
      - "443:443"   # HTTPS
      - "81:81"     # NPM admin UI
    volumes:
      - npm_data:/data
      - npm_letsencrypt:/etc/letsencrypt
```

(and add `npm_data:` / `npm_letsencrypt:` under the top-level `volumes:` block.)

Then in the NPM admin UI (`http://<host>:81`, default login `admin@example.com` / `changeme` —
change it immediately):

1. **Hosts → Proxy Hosts → Add Proxy Host.**
2. **Details tab:**
   - **Domain Names:** `mem0.your-domain.com`
   - **Scheme:** `http`
   - **Forward Hostname / IP:** `mem0-server` (the Compose service name) — or the host IP /
     `127.0.0.1` if NPM runs elsewhere.
   - **Forward Port:** `8000`
   - Enable **Block Common Exploits**. Leave **Websockets Support** on (harmless).
3. **SSL tab:**
   - **SSL Certificate:** *Request a new SSL Certificate*.
   - Enable **Force SSL** and **HTTP/2 Support**.
   - Agree to the Let's Encrypt terms and save. NPM provisions the cert automatically (port 80 must
     be reachable from the internet for the challenge).
4. **Advanced tab** — paste this so MCP streaming isn't buffered or cut off early:

   ```nginx
   proxy_buffering off;
   proxy_request_buffering off;
   proxy_read_timeout 3600s;
   proxy_send_timeout 3600s;
   ```

5. Save, then set `PUBLIC_BASE_URL=https://mem0.your-domain.com` in `.env` and
   `docker compose up -d`.

### Caddy

[Caddy](https://caddyserver.com/) terminates TLS and renews Let's Encrypt certificates
automatically with almost no configuration — often the simplest option on a VPS. A whole reverse
proxy is two lines.

If you run Caddy as a container alongside the app, add it to `docker-compose.yml`:

```yaml
  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
```

(and add `caddy_data:` / `caddy_config:` under the top-level `volumes:` block.)

Create a `Caddyfile` next to `docker-compose.yml`:

```caddy
mem0.your-domain.com {
    reverse_proxy mem0-server:8000 {
        # Stream MCP responses instead of buffering them, and allow long-lived reads.
        flush_interval -1
        transport http {
            read_timeout 1h
        }
    }
}
```

Use the app's host/IP and `:8000` instead of `mem0-server:8000` if Caddy runs outside the Compose
network (e.g. directly on the host, `127.0.0.1:8000`). Caddy obtains and renews the certificate on
first request — just make sure ports 80 and 443 are reachable from the internet and DNS points at
the host.

Then set `PUBLIC_BASE_URL=https://mem0.your-domain.com` in `.env` and `docker compose up -d`
(plus `docker compose restart caddy` if you edited the `Caddyfile`).

### Verifying the proxy

From anywhere on the internet:

```bash
# 1. TLS + health round-trip to Qdrant through the proxy.
curl https://mem0.your-domain.com/healthz
# -> {"ok": true, ...}

# 2. The MCP endpoint answers on the canonical (no-trailing-slash) path.
#    A 401/406 here is fine — it means the route is reachable and auth is enforced.
curl -i https://mem0.your-domain.com/mcp
```

If `/healthz` works but an MCP client hangs or times out, the proxy is almost always buffering the
response or timing out too early — re-check the streaming/timeout settings above. If OAuth clients
reject the connection, confirm `PUBLIC_BASE_URL` exactly matches the public origin with no trailing
slash, and see [Troubleshooting](#troubleshooting).

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

### 3. Deploy the digest app (optional, `mem0-digest`)

The optional **digest** app posts a periodic summary of recently added memories to a Slack or
Discord channel — a lightweight way to resurface what you've been capturing. Like the backup app
it's a **separate**, port-less CapRover app built from the `digest/` directory, running `crond`.

1. Create a CapRover app named `mem0-digest`. It needs **no exposed ports**.
2. Set its **Captain Definition Relative Path** to `./digest/captain-definition`.
3. Point its deployment at this repo / `main` (same webhook pattern, or deploy manually).
4. Set its env vars:

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MEM0_URL` | yes | — | Base URL of the memory server, e.g. `https://mem0.your-domain.com`. |
| `MEM0_API_KEY` | yes | — | The same bearer token the server uses. |
| `DIGEST_WEBHOOK_URL` | recommended | — | Slack or Discord **incoming webhook** URL. If unset, the digest is written to the container log instead of being sent. |
| `DIGEST_WEBHOOK_FORMAT` | no | auto | `slack` or `discord`; auto-detected from the URL. Set explicitly if detection is wrong. |
| `DIGEST_CRON` | no | `0 8 * * *` | Cron schedule in UTC. Default is daily at 08:00; use e.g. `0 8 * * 1` for Mondays only. |
| `DIGEST_WINDOW_DAYS` | no | `1` | Look-back window. Match it to the schedule (e.g. `7` for a weekly digest). |
| `ANTHROPIC_API_KEY` | no | — | If set, the digest is summarized by Claude; otherwise it's a plain bulleted list. |
| `MEM0_LLM_MODEL` | no | `claude-haiku-4-5-20251001` | Model used for summarization. |
| `DIGEST_MAX_MEMORIES` | no | `100` | Cap on memories fetched per run. The server's list endpoint allows at most 100, so larger values are clamped to 100. |
| `DIGEST_TITLE` | no | `🧠 Memory digest` | Heading on the posted message. |
| `DIGEST_SEND_WHEN_EMPTY` | no | `false` | `true` to post even when nothing new was found. |
| `DIGEST_RUN_ON_START` | no | `false` | `true` to run once immediately on container start — handy to verify config. |

Each run fetches recent memories over the REST API, optionally summarizes them with Claude, and
posts the result to your webhook. To verify right after deploy, set `DIGEST_RUN_ON_START=true` and
check `caprover logs mem0-digest`.

> **Cost note:** with `ANTHROPIC_API_KEY` set, each run makes **one** Claude call to summarize.
> Without it, no LLM is used and you get a plain bulleted list.

Non-CapRover hosts can run the same image directly:

```bash
docker build -t mem0-digest ./digest
docker run -d --name mem0-digest \
  -e MEM0_URL=https://mem0.your-domain.com -e MEM0_API_KEY=... \
  -e DIGEST_WEBHOOK_URL=https://hooks.slack.com/services/... \
  -e DIGEST_CRON="0 8 * * *" -e DIGEST_WINDOW_DAYS=1 \
  mem0-digest
```

### 4. Deploy the capture bot (optional, `mem0-capture`)

The optional **capture bot** lets you save a thought into memory by sending a Telegram message —
frictionless capture from your phone. It's a **separate**, port-less CapRover app built from the
`capture/` directory. It long-polls the Telegram Bot API (no inbound webhook or public port needed)
and stores each message via `POST /api/v1/memories`, tagged `agent_id=capture:telegram`.

Because the memory store is single-user and high-trust, the bot **only saves messages from an
allowlist of Telegram chat IDs** — anyone else is refused.

1. Create a Telegram bot: message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the
   **bot token** it gives you.
2. Create a CapRover app named `mem0-capture`. It needs **no exposed ports**.
3. Set its **Captain Definition Relative Path** to `./capture/captain-definition`.
4. Set its env vars (see the table below), **leaving `TELEGRAM_ALLOWED_CHAT_IDS` blank for now**, and
   deploy.
5. Message your bot anything. It replies with your **chat id** (it stores nothing yet — "discovery
   mode"). Put that id in `TELEGRAM_ALLOWED_CHAT_IDS` and redeploy.
6. Message it again — it now replies "Saved ✓" and the note is in your memory.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MEM0_URL` | yes | — | Base URL of the memory server. |
| `MEM0_API_KEY` | yes | — | The same bearer token the server uses. |
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from @BotFather. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | recommended | — | Comma-separated chat IDs allowed to save. **Blank = discovery mode** (replies with your chat id, stores nothing). |
| `CAPTURE_AGENT_ID` | no | `capture:telegram` | Provenance tag stored as `agent_id`. |
| `TELEGRAM_POLL_TIMEOUT` | no | `30` | Long-poll seconds per Telegram request. |

Send plain text to save it as-is, or use `/note <text>`. `/start` and `/help` show usage. Other chat
platforms (Slack slash commands, Discord bots) can be added the same way — parse the inbound message
and call the same `POST /api/v1/memories` endpoint.

> **Security:** keep `TELEGRAM_ALLOWED_CHAT_IDS` set in production. Anyone who finds your bot can
> message it, but only allowlisted chats can write to your memory; everyone else is refused.

Non-CapRover hosts can run the same image directly:

```bash
docker build -t mem0-capture ./capture
docker run -d --name mem0-capture \
  -e MEM0_URL=https://mem0.your-domain.com -e MEM0_API_KEY=... \
  -e TELEGRAM_BOT_TOKEN=123456:ABC... -e TELEGRAM_ALLOWED_CHAT_IDS=123456789 \
  mem0-capture
```

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

### Companion prompt packs

Beyond the baseline rules above, [`docs/prompts/`](./prompts/README.md) collects reusable,
copy-paste prompt packs for specific recurring tasks — [auto-capturing a session
summary](./prompts/auto-capture.md), [research synthesis](./prompts/research-synthesis.md), and
[meeting synthesis](./prompts/meeting-synthesis.md). They're documentation only (no server changes)
and drive the same six tools.

## Automating memory with Claude Code hooks

[Prompting agents to use memory](#prompting-agents-to-use-memory) makes the memory tools
*available* and *asks* Claude to use them — but the model still has to remember to. [Claude
Code hooks](https://code.claude.com/docs/en/hooks-guide) close that gap: they run shell
commands at fixed points in the request/response lifecycle, so memory recall and capture
happen **deterministically**, every time, without relying on the model to choose to call a
tool.

Two hooks give you the loop the model can't be trusted to run on its own:

- **`UserPromptSubmit`** fires right after you submit a prompt, before Claude sees it. A hook
  here searches memserv for memories relevant to what you just asked and injects them into
  Claude's context — "recall first," enforced.
- **`SessionEnd`** fires when the conversation ends. A hook here ships the transcript to
  memserv's REST `add` endpoint, whose LLM extracts the durable facts — "save what mattered,"
  enforced.

Both hooks talk to memserv over the **REST API** with the bearer token, completely independent
of the MCP connector. They work whether or not you've run `claude mcp add`; if you have, the
hook-injected recall and the six MCP tools happily coexist (the hook front-loads context, the
tools let Claude read/write on demand mid-task).

<a id="hooks-prerequisites"></a>
### Prerequisites

- `curl` and [`jq`](https://jqlang.github.io/jq/) on your `PATH` (the scripts parse JSON with
  `jq`).
- Two environment variables exported in the shell that launches `claude`, so the hooks inherit
  them:

  ```bash
  export MEM0_URL=https://mem0.your-domain.com     # base URL, no /api or /mcp suffix
  export MEM0_API_KEY=...                            # the same bearer token the server uses
  ```

  Put these in your shell profile (`~/.bashrc`, `~/.zshrc`) so every session has them. The
  token lives in your environment and, for capture, the conversation is sent to *your* memory
  server — fine for a self-hosted single-user store, but keep the usual "don't put secrets in
  memory" discipline (see the recall/capture notes below).

Save both scripts under `.claude/hooks/` (per-project) or `~/.claude/hooks/` (global), and make
them executable with `chmod +x`.

<a id="recall-inject-memories-on-every-prompt"></a>
### Recall: inject memories on every prompt

`.claude/hooks/mem0-recall.sh` — reads the prompt off stdin, semantically searches memserv, and
prints the top matches back as `additionalContext`. Anything a `UserPromptSubmit` hook writes to
stdout (or returns via `additionalContext`) is added to Claude's context for that turn. It
**never blocks the prompt**: on any error, or if memserv is unreachable, it exits quietly and
Claude proceeds as normal.

```bash
#!/usr/bin/env bash
# mem0-recall.sh — Claude Code UserPromptSubmit hook.
# Search memserv for memories relevant to the prompt and inject them into
# Claude's context before it answers. Never blocks the prompt.
set -euo pipefail

INPUT=$(cat)
PROMPT=$(printf '%s' "$INPUT" | jq -r '.prompt // empty')

# Nothing to search on, or config missing → stay silent.
[ -z "$PROMPT" ] && exit 0
if [ -z "${MEM0_URL:-}" ] || [ -z "${MEM0_API_KEY:-}" ]; then exit 0; fi

# Semantic search; --max-time keeps us well under the 30s UserPromptSubmit budget.
RESPONSE=$(curl -sS --max-time 10 \
  -X POST "$MEM0_URL/api/v1/memories/search" \
  -H "Authorization: Bearer $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg q "$PROMPT" '{query: $q, limit: 5}')") || exit 0

# Format the hits as a bullet list; emit nothing if there are none.
CONTEXT=$(printf '%s' "$RESPONSE" | jq -r '
  (.results // []) | map(select(.memory))
  | if length == 0 then empty
    else "Relevant long-term memories from mem0 (use if helpful, ignore if not):\n"
         + (map("- " + .memory) | join("\n"))
    end' 2>/dev/null) || exit 0

[ -z "$CONTEXT" ] && exit 0

jq -n --arg ctx "$CONTEXT" \
  '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $ctx}}'
```

The injected block is advisory ("use if helpful, ignore if not") so a stray semantic match
doesn't derail the answer. Tune `limit` (how many memories to pull) to taste. To bias toward
recent facts over the closest topical match, add `recency_weight` to the search body (see
[Search memories](#search-memories--post-apiv1memoriessearch)); to recall by an exact term
instead of meaning, use `"mode": "keyword"`.

<a id="capture-save-the-conversation-at-session-end"></a>
### Capture: save the conversation at session end

`.claude/hooks/mem0-capture.sh` — a `SessionEnd` hook that flattens the transcript into a
`messages` array and POSTs it to memserv. **memserv's own LLM does the fact extraction**, so the
hook stays dumb: it doesn't decide what's worth remembering, it just hands over the conversation
and lets the server distill it. Re-sending overlapping content across sessions is cheap —
memserv fingerprints content and skips exact re-adds before the LLM runs (see
[How memory works](#how-memory-works)).

```bash
#!/usr/bin/env bash
# mem0-capture.sh — Claude Code SessionEnd (or Stop) hook.
# Send the conversation to memserv, whose LLM extracts the durable facts.
set -euo pipefail

INPUT=$(cat)
TRANSCRIPT=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty')

if [ -z "$TRANSCRIPT" ] || [ ! -f "$TRANSCRIPT" ]; then exit 0; fi
if [ -z "${MEM0_URL:-}" ] || [ -z "${MEM0_API_KEY:-}" ]; then exit 0; fi

# Flatten the transcript JSONL into a mem0 messages array: user/assistant turns
# and their text only (content may be a string or an array of blocks), capped at
# the last 40 turns to bound payload and cost.
MESSAGES=$(jq -rs '
  [ .[]
    | select(.message.role == "user" or .message.role == "assistant")
    | { role: .message.role,
        content: ( .message.content
                   | if type == "string" then .
                     else ([ .[]? | .text // empty ] | join("\n"))
                     end ) }
    | select(.content != "")
  ] | .[-40:]
' "$TRANSCRIPT" 2>/dev/null) || exit 0

# Empty conversation → nothing to save.
[ "$(printf '%s' "$MESSAGES" | jq 'length')" -gt 0 ] || exit 0

# agent_id is a write-only provenance tag; metadata.source follows the convention.
curl -sS --max-time 30 \
  -X POST "$MEM0_URL/api/v1/memories" \
  -H "Authorization: Bearer $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --argjson msgs "$MESSAGES" \
        '{messages: $msgs, agent_id: "claude-code:hook", metadata: {source: "agent"}}')" \
  >/dev/null 2>&1 || true

exit 0
```

Notes and tradeoffs:

- **`SessionEnd` fires on a graceful end** — `/clear`, `logout`, or exiting the prompt — but not
  if the terminal is force-killed. If you want capture after *every* turn instead of once per
  conversation, register the same script on the **`Stop`** event (fires each time Claude finishes
  responding). That's more thorough but runs fact-extraction far more often.
- **Cost.** Each capture that contains new content triggers one LLM fact-extraction call on
  memserv. The dedup fingerprint makes re-runs over unchanged content free, but a fresh
  conversation is genuinely new work. The 40-turn cap bounds the payload; lower it for tighter
  cost control.
- **`agent_id: "claude-code:hook"`** tags these writes for provenance. It's write-only — it does
  **not** scope future searches (every client shares one store) — but it lets you later find or
  bulk-delete hook-captured memories via the REST filters.
- **Secrets.** The hook ships the whole (tailed) conversation to memserv, and memserv's LLM may
  distill anything in it into a memory. Don't paste credentials into a session you're
  auto-capturing.

<a id="wiring-the-hooks-up"></a>
### Wiring the hooks up

Register both in a Claude Code [settings file](https://code.claude.com/docs/en/hooks-guide) —
`.claude/settings.json` in the project root (shareable, commit it) or `~/.claude/settings.json`
(applies everywhere). Use `$CLAUDE_PROJECT_DIR` for project-scoped scripts, or an absolute path
like `~/.claude/hooks/...` for global ones:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/mem0-recall.sh" }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/mem0-capture.sh" }
        ]
      }
    ]
  }
}
```

Verify with `/hooks` inside Claude Code (it lists registered hooks per event). If a hook doesn't
fire, confirm the scripts are executable, that `MEM0_URL`/`MEM0_API_KEY` are exported in the
launching shell, and that `jq`/`curl` are installed; then run `claude --debug-file /tmp/claude.log`
and check the log for the hook's exit code, stdout, and stderr. You can sanity-check a script
outside Claude Code by piping it a sample payload:

```bash
echo '{"prompt": "where do we deploy?"}' | .claude/hooks/mem0-recall.sh
```

<a id="hooks-vs-prompting"></a>
### Hooks vs. prompting

Hooks and the [prompt-based approach](#prompting-agents-to-use-memory) solve the same problem —
"actually use memory" — from opposite ends, and combine well:

| | Hooks (this section) | Prompting / MCP tools |
|---|---|---|
| **Runs** | Deterministically, every time | When the model decides to |
| **Recall** | Injects top-K matches for *every* prompt | Model searches when it judges it useful |
| **Capture** | Ships the raw conversation; memserv's LLM distills it | Model chooses *what* to save, as clean single-facts |
| **Setup** | `curl`/`jq` scripts + `settings.json` | A paragraph in `CLAUDE.md` |

A good middle ground: use the **recall hook** (deterministic front-loading beats hoping the model
searches) alongside **prompt-based capture** via the
[auto-capture prompt pack](./prompts/auto-capture.md) (the model writes cleaner, more selective
single-fact memories than a raw transcript dump). Or run all four for belt-and-suspenders. They
read and write the same shared store, so nothing conflicts.

## REST API reference

All endpoints live under `/api/v1` and require `Authorization: Bearer <MEM0_API_KEY>`. Request and
response bodies are JSON. `user_id` defaults to `MEM0_DEFAULT_USER_ID` if omitted. Response
schemas are published in the interactive docs at `/docs` (OpenAPI).

### Error responses

Failures return a stable JSON shape:

```json
{"detail": "human-readable summary", "error": "machine_code", "request_id": "abc123def456"}
```

| Status | `error` | Meaning |
|---|---|---|
| `401` | — | Missing/invalid bearer token (plain `detail` only). |
| `404` | — | Memory ID does not exist (`GET`/`PUT`/`DELETE` by ID). |
| `422` | — | Request validation failed (FastAPI's standard shape). |
| `502` | `upstream_provider_error` | The LLM or embedding provider failed — check provider keys/status. |
| `503` | `backend_unavailable` | Qdrant is unreachable or erroring — same condition `/healthz` reports. |
| `500` | `internal_error` | Unexpected failure. The body never contains internals; quote the `request_id` (also settable via an `X-Request-Id` request header) when digging through server logs. |

### Add a memory — `POST /api/v1/memories`

Provide **either** `content` (a string) **or** `messages` (a chat transcript). Optional:
`agent_id`, `run_id`, `metadata`, `user_id`, and `dedup` (default `true`).

By default, submitting content that matches something already stored — compared on a normalized
fingerprint (case-insensitive, whitespace-collapsed), not raw bytes — is skipped before the LLM runs
and returns `{"results": [], "deduplicated": true, "memory_id": "…"}` (see
[How memory works](#how-memory-works)). Set `"dedup": false` to force re-extraction.

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

**Recency boost (optional).** By default results are ordered purely by semantic
similarity. When you care more about what's *latest* than what's the closest
topical match, add `recency_weight` (0.0–1.0): `0` keeps the default order, `1`
orders almost entirely by how recently each memory was created or updated. The
half-life of the decay (default 30 days) is tunable via `recency_half_life_days`.

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories/search \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "current deploy target", "recency_weight": 0.4}'
```

When `recency_weight > 0`, each returned result carries a `rerank_score` showing
the blended similarity-plus-recency value it was sorted by. The MCP
`search_memories` tool accepts the same `recency_weight` argument.

**Keyword search (optional).** Semantic search ranks by *meaning*, which can miss
an exact term — a name, identifier, URL, or rare token. Pass `"mode": "keyword"`
to instead do a **case-insensitive substring match** over memory text, returning
the most recent matches first:

```bash
curl -X POST https://mem0.your-domain.com/api/v1/memories/search \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "Philips Hue", "mode": "keyword"}'
```

The default is `"mode": "semantic"`. The MCP `search_memories` tool accepts the
same `mode` argument. Keyword mode spans the whole user store; it's a
literal-match fallback, not a replacement for semantic retrieval.
(`recency_weight` applies to semantic mode only.)

Under the hood, keyword matching is pushed down to Qdrant via a full-text payload
index that the server creates automatically on first use (no setup or maintenance
needed). Whole-word queries are answered from the index; queries that only match
*inside* a word (e.g. `hil` matching `Philips`) transparently fall back to a scan
of up to a few thousand memories, so results are the same as before — exact,
case-insensitive substring matches, most recent first. If your Qdrant version
can't create the index, everything still works via the scan path.

### List memories — `GET /api/v1/memories`

Query params: `agent_id`, `run_id`, `user_id`, `limit` (1–100, default 50), `offset`
(0–10000, default 0), plus the provenance/review filters `source`, `confidence`,
`review_status` (exact match), and `exclude_expired` (drop memories whose
`expires_at` is in the past). See
[Provenance and review metadata](#provenance-and-review-metadata-convention).

The response carries a `pagination` object — `{"limit": …, "offset": …, "has_more": …}` —
so the full store can be enumerated by advancing `offset` by `limit` while
`has_more` is `true`. Ordering is stable (by internal ID) but **not** chronological.
With `exclude_expired=true`, expired items are dropped *after* the page is cut, so a
page may contain fewer than `limit` items while `has_more` is still `true`.

```bash
curl https://mem0.your-domain.com/api/v1/memories?limit=20 \
  -H "Authorization: Bearer $MEM0_API_KEY"

# Next page:
curl "https://mem0.your-domain.com/api/v1/memories?limit=20&offset=20" \
  -H "Authorization: Bearer $MEM0_API_KEY"

# Only approved, non-expired memories imported from ChatGPT:
curl "https://mem0.your-domain.com/api/v1/memories?source=import:chatgpt&review_status=approved&exclude_expired=true" \
  -H "Authorization: Bearer $MEM0_API_KEY"
```

The same `source` / `confidence` / `review_status` / `exclude_expired` fields are
accepted on `POST /api/v1/memories/search` (in both `semantic` and `keyword` modes).

### Get one — `GET /api/v1/memories/{memory_id}`

Returns 404 if the memory does not exist.

### Update — `PUT /api/v1/memories/{memory_id}`

Body: `{"content": "new text"}`. Returns 404 if the memory does not exist.

### Delete — `DELETE /api/v1/memories/{memory_id}`

Returns `{"deleted": true, "memory_id": "…"}`, or 404 if the memory does not exist.

### Bulk delete — `POST /api/v1/memories/delete_bulk`

Deletes every memory matching exact-match filters: `agent_id`, `run_id`, `source`,
`confidence`, `review_status` (plus optional `user_id`). **At least one filter
besides `user_id` is required** — wiping the whole store through this endpoint is
deliberately impossible.

It is a **dry run by default**: with `"confirm": false` (or omitted) nothing is
deleted; the response reports the match count and a sample of up to 10 items so
you can verify the blast radius first. Re-post the same body with
`"confirm": true` to actually delete. `matched` and `deleted` are **per call**,
capped at 1000; if `has_more` is `true`, more memories match than this call
covered — repeat the call until it's `false`. If a deletion fails partway, the
response carries `"error": "delete_failed_partway"` with the partial `deleted`
count; deletes are idempotent, so just re-post. Deletions go through mem0 (not a
raw vector-store filter delete), so each memory's history stays consistent.

Typical use — undo a bad import run:

```bash
# 1. Dry run: how much would this delete?
curl -X POST https://mem0.your-domain.com/api/v1/memories/delete_bulk \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"source": "import:chatgpt"}'
# -> {"matched": 412, "deleted": 0, "dry_run": true, "has_more": false, "sample": [...]}

# 2. Looks right - confirm:
curl -X POST https://mem0.your-domain.com/api/v1/memories/delete_bulk \
  -H "Authorization: Bearer $MEM0_API_KEY" -H "Content-Type: application/json" \
  -d '{"source": "import:chatgpt", "confirm": true}'
# -> {"matched": 412, "deleted": 412, "dry_run": false, "has_more": false, ...}
```

There is intentionally **no MCP equivalent** — a destructive filter-delete is an
operator/script action, not something a connected agent should be able to reach for.

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

**Cost note.** Every *new* imported memory goes through the normal `add` path, which
invokes the fact-extraction LLM (see the
[Configuration reference](#configuration-reference)). A large ChatGPT or Obsidian import can mean
thousands of LLM calls — use `--dry-run` and `--limit` first to gauge volume.
**Re-running an import is cheap and idempotent:** content already stored — matched on a normalized
fingerprint (case-insensitive, whitespace-collapsed) — is skipped *before* the LLM runs (see
[How memory works](#how-memory-works)), so a second pass over the same export adds nothing and costs
nothing.

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
| MCP hangs or times out through a reverse proxy (but `/healthz` works) | The proxy is buffering or cutting off the streamed `/mcp` response. Disable response buffering and raise the read timeout — see [Putting it behind a reverse proxy (HTTPS)](#putting-it-behind-a-reverse-proxy-https). |
| Server won't start | A required env var is missing or a provider key is absent. Check the startup logs; `app/config.py` names the missing variable. |
| Claude.ai web / Cowork can't connect | OAuth not enabled (`OAUTH_SIGNING_KEY` blank), or the client's redirect URI isn't in `OAUTH_ALLOWED_REDIRECT_URIS`. |
| "Couldn't reach the MCP server" on Claude.ai web / Cowork (but Claude Code/Desktop work) | OAuth discovery failure. Confirm `OAUTH_SIGNING_KEY` is set and `PUBLIC_BASE_URL` exactly matches the public HTTPS URL; the server must advertise the protected-resource metadata in the `/mcp/` 401 `WWW-Authenticate` header. |
| Connector fails right after consent; logs show `POST /oauth/register → 400` | The client's callback isn't in `OAUTH_ALLOWED_REDIRECT_URIS`. The server logs a `dcr_redirect_uri_rejected` warning with the exact `requested` URI and the active `allowed` list — add the requested URI to `OAUTH_ALLOWED_REDIRECT_URIS` and redeploy. Claude.ai web/desktop/mobile/Cowork use `https://claude.ai/api/mcp/auth_callback`. |
| Backup job not running | Check the backup container: `caprover logs mem0-backup`. |
| Digest not arriving | Check `caprover logs mem0-digest`. Common causes: `DIGEST_WEBHOOK_URL` unset (digest is only logged), nothing within `DIGEST_WINDOW_DAYS`, or a wrong webhook URL. Set `DIGEST_RUN_ON_START=true` to trigger a run immediately. |
| Capture bot replies "not authorized" | Your Telegram chat id isn't in `TELEGRAM_ALLOWED_CHAT_IDS`. Clear that var to enter discovery mode (the bot replies with your id), add the id, and redeploy. |
| Capture bot saves nothing / only echoes your chat id | It's in discovery mode because `TELEGRAM_ALLOWED_CHAT_IDS` is blank. Set it to your chat id and redeploy. |
