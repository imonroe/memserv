# PRD: Self-Hosted mem0 Memory Server

**Project name:** `mem0-server`
**Owner:** project maintainer
**Status:** Ready for implementation
**Target deployment:** CapRover on Digital Ocean (existing droplet)
**Existing infrastructure:** Qdrant already deployed on CapRover at `qdrant.<domain>` with API key auth and HTTPS.

---

## 1. Overview

Build and deploy a single self-hosted Python service that exposes a shared mem0 memory store to multiple AI clients over both a REST API and the Model Context Protocol (Streamable HTTP transport). The service uses the existing Qdrant instance as its vector backend. A companion service performs nightly Qdrant snapshots and uploads them to S3. The whole stack auto-deploys to CapRover on every push to `main` via a GitHub webhook.

### 1.1 Goals

1. **One memory pool, many agents.** All AI clients write to and read from the same Qdrant collection, scoped by a shared `user_id` and tagged by `agent_id`.
2. **One server, two protocols.** REST for scripts, n8n, Hermes Agent, and ad-hoc curl. Streamable HTTP MCP for Claude Code, Claude Desktop, Claude.ai web, and Cowork.
3. **Push-to-deploy.** Merges to `main` trigger CapRover to pull, build, and redeploy without manual intervention.
4. **Operational durability.** Nightly Qdrant snapshots are uploaded to S3. Droplet loss does not equal memory loss.
5. **Multi-client, multi-machine.** The server is the single source of truth; clients on any of your machines connect to it over HTTPS.

### 1.2 Non-goals

- Multi-tenant isolation. This is a single-user system. `user_id` is hardcoded to one value in client configs.
- Graph memory (Neo4j). Vector-only is sufficient for v1.
- A web UI. Use the Qdrant dashboard for low-level inspection; use Claude or curl for everything else.
- Tracking memory access logs in a separate DB. mem0 already maintains history.

### 1.3 Client compatibility matrix

| Client | Transport | Auth method | Phase |
|---|---|---|---|
| Claude Code (CLI) | Streamable HTTP MCP | Bearer token (header) | 1 |
| Claude Desktop | Streamable HTTP MCP | Bearer token (header, via Advanced Settings) | 1 |
| Hermes Agent | REST or MCP | Bearer token | 1 |
| Direct REST (scripts, n8n, curl) | REST | Bearer token | 1 |
| Claude.ai (web) | Streamable HTTP MCP | OAuth 2.1 + PKCE + DCR | 2 |
| Cowork | Streamable HTTP MCP | OAuth 2.1 + PKCE + DCR | 2 |

**Phase 1** is the MVP. Ship it first; everything in the Phase 1 column works end-to-end. **Phase 2** adds the OAuth layer for Claude.ai web and Cowork. The two phases share the same FastAPI app, same MCP server, same Qdrant collection — Phase 2 just adds OAuth endpoints and a token verifier capable of accepting either bearer or OAuth-issued JWTs.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                        Client machines                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐ │
│  │ Claude Code │  │Claude Desktop│  │Hermes Agent │  │curl / n8n  │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └─────┬──────┘ │
└─────────┼────────────────┼────────────────┼───────────────┼────────┘
          │  Bearer        │  Bearer        │  Bearer       │  Bearer
          └────────────────┴────────────────┴───────────────┘
                                   │ HTTPS
                                   ▼
┌────────────────────────────────────────────────────────────────────┐
│                  Anthropic cloud (claude.ai, Cowork)                │
│           OAuth 2.1 + PKCE + DCR ──► /oauth/* endpoints             │
└────────────────────────────────┬───────────────────────────────────┘
                                 │ HTTPS
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│              CapRover app: mem0-server (this repo)                  │
│                                                                     │
│  FastAPI                                                            │
│  ├── /healthz                          (unauth)                     │
│  ├── /api/v1/memories                  (REST, bearer)               │
│  ├── /api/v1/memories/search           (REST, bearer)               │
│  ├── /api/v1/memories/{id}             (REST, bearer)               │
│  ├── /api/v1/memories/{id}/history     (REST, bearer)               │
│  ├── /.well-known/oauth-authorization-server  (Phase 2)             │
│  ├── /oauth/register                   (Phase 2, DCR)               │
│  ├── /oauth/authorize                  (Phase 2)                    │
│  ├── /oauth/token                      (Phase 2)                    │
│  └── /mcp/  (mounted FastMCP)          (MCP, bearer or OAuth JWT)   │
│       ├── tool: add_memory                                          │
│       ├── tool: search_memories                                     │
│       ├── tool: list_memories                                       │
│       ├── tool: get_memory                                          │
│       ├── tool: update_memory                                       │
│       └── tool: delete_memory                                       │
└────────────────────────────────┬───────────────────────────────────┘
                                 │ HTTPS + Qdrant API key
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│                  CapRover app: qdrant (existing)                    │
│           Collection: memories  (vectors + payload)                 │
└────────────────────────────────┬───────────────────────────────────┘
                                 │ Snapshot API
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│              CapRover app: mem0-backup (this repo)                  │
│   Cron @ 03:00 UTC daily                                            │
│   ├── POST /collections/memories/snapshots  (create)            │
│   ├── GET  /collections/memories/snapshots/{name}  (download)   │
│   └── Upload to s3://<bucket>/mem0-backups/{date}.snapshot          │
│   Retention: keep last 14 days in S3, last 3 on local volume        │
└────────────────────────────────────────────────────────────────────┘
```

### 2.1 Why one FastAPI app for both REST and MCP

The mem0 Python library does the heavy lifting — fact extraction (calls an LLM), embedding (calls an embedder), and Qdrant I/O — and we want exactly one configured instance of `Memory` per process. Splitting REST and MCP into separate services would double the LLM/embedder client connections, double the cost of any caching layer added later, and complicate auth. Mounting FastMCP's Streamable HTTP app inside FastAPI keeps both protocols sharing the same `Memory` instance, same auth verifier, same logging.

### 2.2 Why a separate backup app instead of a cron inside the main app

The main app is stateless and may be restarted at any time by CapRover (deploys, OOM, etc.). Backups must run on a predictable schedule independent of the API process. The backup app is a small Alpine container with crond running a single shell script. It has no exposed ports.

---

## 3. Repository layout

```
mem0-server/
├── .github/
│   └── workflows/
│       └── ci.yml                    # lint + tests on PR
├── app/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app, FastMCP mount, lifespan
│   ├── config.py                     # Settings class (pydantic-settings)
│   ├── memory.py                     # Mem0 wrapper, init_memory()
│   ├── auth.py                       # Bearer verifier (Phase 1) + OAuth (Phase 2)
│   ├── rest.py                       # FastAPI REST router
│   ├── mcp_server.py                 # FastMCP instance + tool definitions
│   ├── oauth.py                      # Phase 2: OAuth 2.1 endpoints (DCR, authorize, token, JWKS)
│   └── logging_setup.py              # structlog config
├── backup/
│   ├── Dockerfile                    # alpine + crond + curl + aws-cli
│   ├── crontab                       # schedule
│   ├── backup.sh                     # snapshot + upload script
│   └── captain-definition
├── tests/
│   ├── conftest.py
│   ├── test_rest.py
│   ├── test_mcp.py
│   ├── test_memory.py
│   └── test_auth.py
├── .dockerignore
├── .env.example
├── .gitignore
├── captain-definition                # for the main app
├── CLAUDE.md                         # repo-local guide for Claude Code
├── Dockerfile
├── pyproject.toml                    # ruff, pytest, deps
├── README.md
└── requirements.txt
```

---

## 4. Tech stack & dependencies

- **Python 3.12**
- **FastAPI** ≥ 0.115 — HTTP framework
- **FastMCP** ≥ 2.12 — MCP server framework. *Note: this is the PrefectHQ/fastmcp package on PyPI, not the older `mcp.server.fastmcp` module. Import as `from fastmcp import FastMCP`.*
- **uvicorn[standard]** ≥ 0.30 — ASGI server
- **mem0ai** ≥ 0.1.100 — memory layer
- **qdrant-client** ≥ 1.13 — pulled in by mem0ai but pin explicitly
- **openai** ≥ 1.50 — embeddings provider client (used by mem0)
- **anthropic** ≥ 0.39 — LLM provider client (used by mem0 for fact extraction)
- **pydantic** ≥ 2.7 & **pydantic-settings** ≥ 2.4 — config
- **structlog** ≥ 24 — structured logging
- **httpx** ≥ 0.27 — used in tests and OAuth endpoints
- **PyJWT[crypto]** ≥ 2.9 — Phase 2 OAuth token signing
- **pytest**, **pytest-asyncio**, **respx**, **ruff** — dev tools

Pin via `requirements.txt` with `==` for runtime, and use `pyproject.toml` for dev tools and project metadata.

---

## 5. Configuration

All configuration is via environment variables. The `Settings` class in `app/config.py` is the single source of truth. Reject startup if any required value is missing.

### 5.1 Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `QDRANT_HOST` | yes | — | e.g. `qdrant.your-domain.com` |
| `QDRANT_PORT` | no | `443` | |
| `QDRANT_HTTPS` | no | `true` | |
| `QDRANT_API_KEY` | yes | — | Key configured on Qdrant |
| `MEM0_COLLECTION` | no | `memories` | |
| `MEM0_DEFAULT_USER_ID` | yes | — | e.g. `default-user`. Used as fallback when clients don't supply one. |
| `MEM0_LLM_PROVIDER` | no | `anthropic` | mem0 LLM for fact extraction |
| `MEM0_LLM_MODEL` | no | `claude-haiku-4-5-20251001` | |
| `ANTHROPIC_API_KEY` | conditional | — | Required if `MEM0_LLM_PROVIDER=anthropic` |
| `MEM0_EMBED_PROVIDER` | no | `openai` | |
| `MEM0_EMBED_MODEL` | no | `text-embedding-3-small` | |
| `MEM0_EMBED_DIMS` | no | `1536` | Must match the model. 3-small=1536, 3-large=3072 |
| `OPENAI_API_KEY` | conditional | — | Required if `MEM0_EMBED_PROVIDER=openai` |
| `MEM0_API_KEY` | yes | — | Static bearer token for Phase 1 clients. `openssl rand -hex 32` |
| `PUBLIC_BASE_URL` | yes | — | e.g. `https://mem0.your-domain.com`. Used in OAuth metadata. |
| `LOG_LEVEL` | no | `INFO` | |
| `OAUTH_SIGNING_KEY` | Phase 2 | — | PEM-encoded private key for signing JWTs. Generate with `openssl genrsa 2048`. Store as a single-line env var with `\n` escaped, or use a secret file mount. |
| `OAUTH_ALLOWED_REDIRECT_URIS` | Phase 2 | `https://claude.ai/api/mcp/auth_callback,https://cowork.com/api/mcp/auth_callback` | Comma-separated. |

Provide `.env.example` with every variable listed and dummy values so Claude Code (and humans) can see the shape at a glance.

### 5.2 `app/config.py`

```python
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Qdrant
    qdrant_host: str
    qdrant_port: int = 443
    qdrant_https: bool = True
    qdrant_api_key: str

    # mem0 core
    mem0_collection: str = "memories"
    mem0_default_user_id: str

    # LLM (fact extraction)
    mem0_llm_provider: str = "anthropic"
    mem0_llm_model: str = "claude-haiku-4-5-20251001"
    anthropic_api_key: str | None = None

    # Embeddings
    mem0_embed_provider: str = "openai"
    mem0_embed_model: str = "text-embedding-3-small"
    mem0_embed_dims: int = 1536
    openai_api_key: str | None = None

    # Auth
    mem0_api_key: str
    public_base_url: str

    # OAuth (Phase 2)
    oauth_signing_key: str | None = None
    oauth_allowed_redirect_uris: str = "https://claude.ai/api/mcp/auth_callback,https://cowork.com/api/mcp/auth_callback"

    # Misc
    log_level: str = "INFO"

    @property
    def oauth_enabled(self) -> bool:
        return self.oauth_signing_key is not None

    @property
    def allowed_redirect_uris_list(self) -> list[str]:
        return [u.strip() for u in self.oauth_allowed_redirect_uris.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

---

## 6. mem0 wrapper (`app/memory.py`)

Wrap the mem0 `Memory.from_config()` call. Single module-level instance, lazily constructed.

```python
from functools import lru_cache
from mem0 import Memory
from app.config import get_settings


def _build_config(s) -> dict:
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": s.mem0_collection,
                "host": s.qdrant_host,
                "port": s.qdrant_port,
                "https": s.qdrant_https,
                "api_key": s.qdrant_api_key,
                "embedding_model_dims": s.mem0_embed_dims,
            },
        },
        "llm": {
            "provider": s.mem0_llm_provider,
            "config": {"model": s.mem0_llm_model},
        },
        "embedder": {
            "provider": s.mem0_embed_provider,
            "config": {"model": s.mem0_embed_model},
        },
        "version": "v1.1",
    }


@lru_cache
def get_memory() -> Memory:
    s = get_settings()
    return Memory.from_config(_build_config(s))
```

**Important:** On first call, mem0 will create the Qdrant collection with the configured dimension. Confirm in the Qdrant dashboard that the collection vector size matches `MEM0_EMBED_DIMS`. A mismatch is the most common operational footgun and will manifest as silent search failures.

---

## 7. REST API (`app/rest.py`)

All endpoints under `/api/v1`. All require `Authorization: Bearer <MEM0_API_KEY>` except `/healthz`.

### 7.1 Endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/healthz` | — | `{"ok": true, "version": "...", "qdrant": "reachable"}` |
| POST | `/api/v1/memories` | `AddMemoryRequest` | `{"results": [...]}` |
| POST | `/api/v1/memories/search` | `SearchRequest` | `{"results": [...]}` |
| GET | `/api/v1/memories` | query: `user_id?`, `agent_id?`, `limit?=50` | `{"results": [...]}` |
| GET | `/api/v1/memories/{id}` | — | memory object |
| PUT | `/api/v1/memories/{id}` | `{"content": "..."}` | updated memory |
| DELETE | `/api/v1/memories/{id}` | — | `{"deleted": true}` |
| GET | `/api/v1/memories/{id}/history` | — | history array |

### 7.2 Schemas

```python
from typing import Literal, Optional
from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class AddMemoryRequest(BaseModel):
    content: Optional[str] = None
    messages: Optional[list[Message]] = None
    user_id: Optional[str] = None  # falls back to settings.mem0_default_user_id
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    metadata: Optional[dict] = None


class SearchRequest(BaseModel):
    query: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)
```

### 7.3 `/healthz` behavior

`/healthz` does an actual round-trip to Qdrant (`GET /collections`) to verify connectivity. If Qdrant is unreachable, return HTTP 503 with `{"ok": false, "qdrant": "unreachable"}`. Use a 2-second timeout so a slow Qdrant doesn't block CapRover health checks indefinitely.

---

## 8. MCP server (`app/mcp_server.py`)

Use `fastmcp.FastMCP`. Mount via Streamable HTTP transport in stateless mode (required for horizontal scalability and to avoid session-not-found errors).

### 8.1 Tools

Each tool maps thinly to a mem0 operation. The `user_id` always falls back to `settings.mem0_default_user_id`; the LLM should not have to manage that.

```python
from fastmcp import FastMCP
from app.config import get_settings
from app.memory import get_memory

def build_mcp() -> FastMCP:
    s = get_settings()
    mcp = FastMCP("mem0-server")
    memory = get_memory()
    default_user = s.mem0_default_user_id

    @mcp.tool()
    def add_memory(
        content: str,
        agent_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Store a fact or observation in long-term memory.

        Use when the user shares preferences, project context, decisions,
        or anything they may want recalled in future conversations.
        """
        kwargs = {"user_id": default_user}
        if agent_id:
            kwargs["agent_id"] = agent_id
        if metadata:
            kwargs["metadata"] = metadata
        return memory.add(content, **kwargs)

    @mcp.tool()
    def search_memories(
        query: str,
        agent_id: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Search long-term memory by semantic similarity."""
        kwargs = {"user_id": default_user, "limit": limit}
        if agent_id:
            kwargs["agent_id"] = agent_id
        return memory.search(query=query, **kwargs)

    @mcp.tool()
    def list_memories(agent_id: str | None = None) -> dict:
        """List all stored memories for the current user."""
        kwargs = {"user_id": default_user}
        if agent_id:
            kwargs["agent_id"] = agent_id
        return memory.get_all(**kwargs)

    @mcp.tool()
    def get_memory(memory_id: str) -> dict:
        """Fetch a single memory by ID."""
        return memory.get(memory_id=memory_id)

    @mcp.tool()
    def update_memory(memory_id: str, content: str) -> dict:
        """Replace the content of an existing memory."""
        return memory.update(memory_id=memory_id, data=content)

    @mcp.tool()
    def delete_memory(memory_id: str) -> dict:
        """Permanently delete a memory."""
        memory.delete(memory_id=memory_id)
        return {"deleted": True, "memory_id": memory_id}

    return mcp
```

### 8.2 Mounting into FastAPI (`app/main.py`)

The critical detail: FastMCP needs its lifespan passed to FastAPI's constructor, or you'll get a `Task group is not initialized` runtime error on the first MCP request.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.config import get_settings
from app.logging_setup import configure_logging
from app.rest import router as rest_router
from app.mcp_server import build_mcp
from app.auth import bearer_or_oauth_dependency

configure_logging()
settings = get_settings()

mcp = build_mcp()
# stateless_http=True is required; transport must be streamable-http
mcp_app = mcp.http_app(path="/", stateless_http=True, transport="streamable-http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(
    title="mem0 Memory Server",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(rest_router, prefix="/api/v1")

# Mount MCP at /mcp; with path="/" above the final URL is /mcp/
app.mount("/mcp", mcp_app)


@app.get("/healthz")
async def healthz():
    # Real Qdrant check lives in rest.py; this is a quick liveness probe
    return {"ok": True}
```

### 8.3 Authentication on the MCP endpoint

FastMCP supports several auth mechanisms. For Phase 1, use `StaticTokenVerifier` with the same `MEM0_API_KEY` that the REST endpoints check. For Phase 2, swap to a verifier that accepts either the static bearer token *or* a JWT signed by the OAuth signing key.

```python
# app/auth.py — Phase 1
from fastmcp.server.auth import StaticTokenVerifier
from app.config import get_settings

def build_verifier():
    s = get_settings()
    return StaticTokenVerifier(
        tokens={
            s.mem0_api_key: {"client_id": s.mem0_default_user_id, "scopes": ["read", "write"]}
        }
    )
```

Pass `auth=build_verifier()` to `FastMCP(...)` in `build_mcp()`.

For Phase 2 (OAuth), implement a composite verifier:

```python
# Phase 2 additions
from fastmcp.server.auth import TokenVerifier
import jwt

class CompositeVerifier(TokenVerifier):
    def __init__(self, static_token: str, jwt_public_key: str):
        self.static_token = static_token
        self.jwt_public_key = jwt_public_key

    async def verify_token(self, token: str):
        if token == self.static_token:
            return {"client_id": settings.mem0_default_user_id, "scopes": ["read", "write"]}
        try:
            payload = jwt.decode(
                token, self.jwt_public_key, algorithms=["RS256"],
                audience="mem0-server",
            )
            return {"client_id": payload["sub"], "scopes": payload.get("scope", "").split()}
        except jwt.InvalidTokenError:
            return None
```

---

## 9. Authentication (deep dive)

### 9.1 Phase 1 — Static bearer token

The same `MEM0_API_KEY` value protects every REST endpoint and the MCP endpoint. Clients send `Authorization: Bearer <token>` on every request. Single user, no scopes beyond a coarse `read`/`write`.

REST endpoint dependency:

```python
# app/auth.py
from fastapi import Header, HTTPException, status
from app.config import get_settings

async def require_bearer(authorization: str = Header(default="")):
    s = get_settings()
    expected = f"Bearer {s.mem0_api_key}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
```

Apply with `dependencies=[Depends(require_bearer)]` on the REST router.

### 9.2 Phase 2 — OAuth 2.1 + PKCE + Dynamic Client Registration

Required by Claude.ai web and Cowork. The MCP server itself acts as the OAuth Authorization Server (AS). There is no upstream identity provider — this is a single-user setup, so "the user" is implicit. The "consent screen" is a trivial page that auto-redirects on submit.

#### 9.2.1 Endpoints to implement (`app/oauth.py`)

| Path | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | OAuth 2.1 metadata document |
| `GET /.well-known/jwks.json` | Public key for JWT verification |
| `POST /oauth/register` | RFC 7591 Dynamic Client Registration |
| `GET /oauth/authorize` | Renders a minimal HTML consent page |
| `POST /oauth/authorize` | Handles consent submission, redirects with `code` |
| `POST /oauth/token` | Exchanges `code` for `access_token` (JWT) |

#### 9.2.2 Behavior

- **Discovery**: `/.well-known/oauth-authorization-server` returns:
  ```json
  {
    "issuer": "https://mem0.your-domain.com",
    "authorization_endpoint": "https://mem0.your-domain.com/oauth/authorize",
    "token_endpoint": "https://mem0.your-domain.com/oauth/token",
    "registration_endpoint": "https://mem0.your-domain.com/oauth/register",
    "jwks_uri": "https://mem0.your-domain.com/.well-known/jwks.json",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["client_secret_post", "none"]
  }
  ```
- **DCR**: `POST /oauth/register` accepts the standard RFC 7591 body, validates the `redirect_uris` against `OAUTH_ALLOWED_REDIRECT_URIS`, generates a `client_id` (random hex), optionally a `client_secret`, persists them in-memory (or a small SQLite file at `/data/oauth_clients.db` if you want persistence across restarts), and returns the registration response.
- **Authorize**: PKCE `S256` is mandatory. Reject requests without `code_challenge`. The consent page is a single HTML form with one "Authorize" button. On submit, generate an authorization code (random, 5-minute TTL, single-use, bound to the PKCE challenge and `redirect_uri`).
- **Token**: Verify `code_verifier` against the stored `code_challenge`. Issue an RS256 JWT with claims:
  ```json
  {
    "iss": "https://mem0.your-domain.com",
    "sub": "<MEM0_DEFAULT_USER_ID>",
    "aud": "mem0-server",
    "scope": "read write",
    "client_id": "<the registered client_id>",
    "iat": 1700000000,
    "exp": 1700086400
  }
  ```
  Default token lifetime: 24 hours. Refresh tokens optional but recommended (return a `refresh_token` and accept `grant_type=refresh_token`).

#### 9.2.3 Storage

Authorization codes and registered clients are tiny and few. Use a single SQLite file at `/app/data/oauth.db` mounted on a CapRover persistent volume. Schema:

```sql
CREATE TABLE clients (
    client_id TEXT PRIMARY KEY,
    client_secret TEXT,
    redirect_uris TEXT NOT NULL,  -- JSON array
    created_at TEXT NOT NULL
);

CREATE TABLE auth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
```

#### 9.2.4 Key generation

Generate the RSA keypair once and store the private key as the `OAUTH_SIGNING_KEY` env var. Example:

```bash
openssl genrsa -out private.pem 2048
openssl rsa -in private.pem -pubout -out public.pem
# Paste private.pem contents into OAUTH_SIGNING_KEY env var in CapRover.
```

The public key is derived from the private key at startup and exposed via JWKS.

---

## 10. Dockerfile (main app)

```dockerfile
FROM python:3.12-slim

# System deps for any C extensions in mem0/openai/anthropic
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# CapRover persistent volume target for OAuth SQLite (Phase 2)
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
```

### 10.1 `captain-definition`

```json
{
  "schemaVersion": 2,
  "dockerfilePath": "./Dockerfile"
}
```

---

## 11. GitHub → CapRover auto-deploy

Use CapRover's built-in webhook deploy. No GitHub Actions required for the deploy step (CI can still run tests in parallel; see §13).

### 11.1 Steps to wire up

1. In CapRover, create app `mem0-server`. **Has Persistent Data: checked** (for the OAuth SQLite in Phase 2).
2. Under **App Configs → Persistent Directories**, map `/app/data` → `/captain/data/mem0-server-data`.
3. Set **Container HTTP Port** to `8000`.
4. Set all environment variables from §5.1.
5. Under the **Deployment** tab, in section **Method 3: Deploy from Github/Bitbucket/Gitlab**:
   - Repository: `https://github.com/<user>/mem0-server.git`
   - Branch: `main`
   - Username: GitHub username
   - Password: a GitHub fine-grained personal access token with `contents:read` for this repo
   - Click **Save & Update**.
   - CapRover will display a webhook URL of the form `https://captain.<domain>/api/v2/user/apps/webhooks/triggerbuild?namespace=captain&token=<token>`.
6. In the GitHub repo: **Settings → Webhooks → Add webhook**:
   - Payload URL: the webhook URL from step 5
   - Content type: `application/json`
   - Secret: leave empty (CapRover uses the token in the URL)
   - Events: **Just the push event**
   - Active: yes
7. Enable HTTPS + Force HTTPS in CapRover for `mem0-server`.

Test: make a trivial commit on `main` and confirm CapRover builds and deploys within ~2 minutes.

### 11.2 Branch protection (recommended)

In GitHub repo settings:
- Require a pull request before merging to `main`
- Require status checks to pass (the CI workflow in §13)
- Require linear history

This prevents accidentally deploying broken code by direct push.

---

## 12. Backup service

A separate, tiny CapRover app named `mem0-backup`.

### 12.1 `backup/Dockerfile`

```dockerfile
FROM alpine:3.20

RUN apk add --no-cache curl jq aws-cli dcron tzdata bash \
    && cp /usr/share/zoneinfo/UTC /etc/localtime

COPY backup.sh /usr/local/bin/backup.sh
COPY crontab /etc/crontabs/root
RUN chmod +x /usr/local/bin/backup.sh

CMD ["crond", "-f", "-l", "8"]
```

### 12.2 `backup/crontab`

```
# Daily at 03:00 UTC
0 3 * * * /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1
```

### 12.3 `backup/backup.sh`

```bash
#!/bin/bash
set -euo pipefail

: "${QDRANT_URL:?required}"        # e.g. https://qdrant.your-domain.com
: "${QDRANT_API_KEY:?required}"
: "${MEM0_COLLECTION:?required}"
: "${S3_BUCKET:?required}"
: "${S3_PREFIX:=mem0-backups}"
: "${AWS_ACCESS_KEY_ID:?required}"
: "${AWS_SECRET_ACCESS_KEY:?required}"
: "${AWS_DEFAULT_REGION:=us-east-1}"
: "${RETENTION_DAYS:=14}"

TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
LOCAL_DIR=/tmp/snapshots
mkdir -p "$LOCAL_DIR"

echo "[$TS] Creating snapshot of $MEM0_COLLECTION..."
SNAPSHOT_NAME=$(
  curl -fsSL -X POST \
    -H "api-key: $QDRANT_API_KEY" \
    "$QDRANT_URL/collections/$MEM0_COLLECTION/snapshots" \
  | jq -r '.result.name'
)

echo "[$TS] Snapshot created: $SNAPSHOT_NAME"

LOCAL_FILE="$LOCAL_DIR/${TS}_${SNAPSHOT_NAME}"
curl -fsSL \
  -H "api-key: $QDRANT_API_KEY" \
  "$QDRANT_URL/collections/$MEM0_COLLECTION/snapshots/$SNAPSHOT_NAME" \
  -o "$LOCAL_FILE"

SIZE=$(stat -c%s "$LOCAL_FILE")
echo "[$TS] Downloaded $SIZE bytes to $LOCAL_FILE"

echo "[$TS] Uploading to s3://$S3_BUCKET/$S3_PREFIX/"
aws s3 cp "$LOCAL_FILE" "s3://$S3_BUCKET/$S3_PREFIX/${TS}.snapshot"

# Delete the Qdrant-side snapshot to free space on the droplet
curl -fsSL -X DELETE \
  -H "api-key: $QDRANT_API_KEY" \
  "$QDRANT_URL/collections/$MEM0_COLLECTION/snapshots/$SNAPSHOT_NAME" > /dev/null

# Local rotation: keep 3 most recent
ls -1t "$LOCAL_DIR" | tail -n +4 | xargs -I {} rm -f "$LOCAL_DIR/{}"

# S3 rotation: delete objects older than RETENTION_DAYS
CUTOFF=$(date -u -d "$RETENTION_DAYS days ago" +%Y-%m-%d)
aws s3 ls "s3://$S3_BUCKET/$S3_PREFIX/" \
  | awk '{print $4}' \
  | while read -r KEY; do
    KEY_DATE=$(echo "$KEY" | cut -c1-10)
    if [[ "$KEY_DATE" < "$CUTOFF" ]]; then
      echo "[$TS] Pruning s3://$S3_BUCKET/$S3_PREFIX/$KEY"
      aws s3 rm "s3://$S3_BUCKET/$S3_PREFIX/$KEY"
    fi
  done

echo "[$TS] Backup complete."
```

### 12.4 `backup/captain-definition`

```json
{
  "schemaVersion": 2,
  "dockerfilePath": "./Dockerfile"
}
```

### 12.5 Deploy

Deploy the `backup/` directory as a *separate* CapRover app (`mem0-backup`). No exposed HTTP ports. Set environment variables: `QDRANT_URL`, `QDRANT_API_KEY`, `MEM0_COLLECTION`, `S3_BUCKET`, `S3_PREFIX` (optional), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`, `RETENTION_DAYS` (optional).

Since the backup app is in the same repo as the main app, CapRover needs a separate `captain-definition` for it. The simplest way: put `captain-definition` at the root pointing to `Dockerfile` (main app), and configure the `mem0-backup` CapRover app's Deployment tab with the same repo but set a custom path. Alternative: split into two repos. **Recommendation: one repo, two apps, each with its own captain-definition file at its own subdirectory, and configure each CapRover app's `Captain Definition Relative Path` field.** CapRover supports this directly under the Deployment tab.

### 12.6 Restore drill

Document the restore procedure in `README.md`:

```bash
# 1. Download the snapshot from S3
aws s3 cp s3://<bucket>/mem0-backups/2026-05-20T03-00-00Z.snapshot ./

# 2. Upload to Qdrant
curl -X POST \
  -H "api-key: $QDRANT_API_KEY" \
  -F "snapshot=@2026-05-20T03-00-00Z.snapshot" \
  "https://qdrant.your-domain.com/collections/memories/snapshots/upload"

# 3. Verify
curl -H "api-key: $QDRANT_API_KEY" \
  "https://qdrant.your-domain.com/collections/memories"
```

A monthly restore drill (restore to a test collection, verify count) is strongly recommended but not part of v1 automation.

---

## 13. Testing

### 13.1 Unit tests

- `tests/test_memory.py`: mock the mem0 `Memory` class; verify the config dict assembled in `_build_config` matches expected shape.
- `tests/test_rest.py`: use `TestClient`; assert 401 without bearer, 200 with correct bearer, schema validation works.
- `tests/test_mcp.py`: use `fastmcp.Client` against the in-process MCP server; assert each tool is registered and callable.
- `tests/test_auth.py`: bearer pass/fail. Phase 2: JWT pass/fail, expired token, wrong audience, wrong signature.

### 13.2 Integration test (manual)

`scripts/smoke.sh` — a bash script that:

1. Hits `/healthz`
2. POSTs a memory via REST
3. Searches for it via REST
4. Calls `add_memory` via MCP (using `mcp` CLI or `fastmcp.Client`)
5. Searches via MCP, asserts the REST-added memory is found
6. Deletes test memories

### 13.3 GitHub Actions (`.github/workflows/ci.yml`)

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: pip install pytest pytest-asyncio respx ruff
      - run: ruff check app/
      - run: pytest -q
```

The CapRover webhook deploys regardless of CI status (it fires on push). If you want CI-gated deploys, replace the webhook with a GitHub Actions job that calls `caprover deploy` only on green CI. See §11 for the tradeoff — webhook is simpler; Actions is safer.

---

## 14. Client configuration

### 14.1 Claude Code (CLI)

```bash
claude mcp add --scope user --transport http mem0-remote \
  https://mem0.your-domain.com/mcp/ \
  --header "Authorization: Bearer $MEM0_API_KEY"
```

Verify with `claude mcp list`. The server should show as connected with 6 tools.

### 14.2 Claude Desktop

1. Open **Settings → Connectors → Add custom connector**.
2. Name: `mem0`
3. Remote MCP server URL: `https://mem0.your-domain.com/mcp/`
4. Expand **Advanced settings**.
5. In the headers section (if visible in current Desktop build), add:
   - Key: `Authorization`
   - Value: `Bearer <MEM0_API_KEY>`
6. If the headers UI is not present in your Claude Desktop version, complete Phase 2 first and use the OAuth flow instead.

### 14.3 Claude.ai (web) — Phase 2 only

1. **Settings → Connectors → Add custom connector**.
2. Name: `mem0`
3. Remote MCP server URL: `https://mem0.your-domain.com/mcp/`
4. Leave OAuth Client ID and Secret empty — DCR will register automatically.
5. Click **Connect**, complete the consent step on the redirect.

### 14.4 Cowork — Phase 2 only

Same as Claude.ai web — go to Connectors, add the custom URL, complete OAuth.

### 14.5 Hermes Agent

Two options:

**a) REST**: configure Hermes with base URL `https://mem0.your-domain.com/api/v1` and bearer header.

**b) MCP**: configure Hermes' MCP client (Hermes Agent supports Streamable HTTP MCP servers) with the same URL + headers as Claude Code in §14.1.

### 14.6 Direct REST (curl / scripts / n8n)

```bash
export MEM0_URL=https://mem0.your-domain.com
export MEM0_API_KEY=...

# Add
curl -X POST $MEM0_URL/api/v1/memories \
  -H "Authorization: Bearer $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "We deploy with CapRover on DO", "agent_id": "n8n-flow"}'

# Search
curl -X POST $MEM0_URL/api/v1/memories/search \
  -H "Authorization: Bearer $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "where do we host things?"}'
```

---

## 15. README.md (contents to ship in the repo)

The repository's `README.md` should include:

1. One-paragraph project description.
2. Architecture diagram (copy from §2).
3. Local dev quickstart: clone, copy `.env.example` to `.env`, `pip install -r requirements.txt`, `uvicorn app.main:app --reload`.
4. Production deploy: short version of §11 and §12.5.
5. Client setup: condensed §14 with copy-paste commands.
6. Restore drill: §12.6.
7. Troubleshooting table:
   - "Search returns empty" → check embedding dimension matches collection
   - "401 on MCP" → verify Authorization header on Claude Code config
   - "Snapshot job not running" → `caprover logs mem0-backup`

---

## 16. CLAUDE.md (Claude Code context file)

Write a `CLAUDE.md` at the repo root so Claude Code has persistent project context:

```markdown
# mem0-server — Claude Code Notes

This is a self-hosted memory server combining FastAPI (REST) and FastMCP (Streamable HTTP MCP) in one process, backed by an external Qdrant instance.

## Don't change without reading the PRD
- The dual-protocol mount (FastAPI + FastMCP) requires the lifespan dance documented in `app/main.py`. Don't remove it.
- Embedding dimension (`MEM0_EMBED_DIMS`) must match the embedder's actual output dim. Mismatch causes silent search failures.
- `stateless_http=True` is required on `mcp.http_app()` to avoid session errors with multiple workers.
- Don't introduce per-user logic. This is a single-user system; `MEM0_DEFAULT_USER_ID` is the user.

## How to run tests
```bash
pip install -r requirements.txt
pip install pytest pytest-asyncio respx ruff
ruff check app/
pytest -q
```

## How to run locally
```bash
cp .env.example .env  # fill in values
uvicorn app.main:app --reload --port 8000
```

## Key files
- `app/main.py` — wiring
- `app/memory.py` — mem0 config (the most tweak-prone file)
- `app/mcp_server.py` — MCP tool definitions
- `app/auth.py` — bearer (Phase 1), composite verifier (Phase 2)
- `app/oauth.py` — OAuth endpoints (Phase 2)
- `backup/` — separate CapRover app for nightly snapshots
```

---

## 17. Implementation plan (ordered task list for Claude Code)

### Milestone A — Skeleton and Phase 1 (bearer only)

1. Initialize repo: `pyproject.toml`, `requirements.txt`, `.gitignore`, `.dockerignore`, `.env.example`, `README.md` placeholder, `CLAUDE.md`.
2. Implement `app/config.py` with the `Settings` class. Add `tests/test_config.py` that loads from a fixture `.env`.
3. Implement `app/memory.py`. Add `tests/test_memory.py` that mocks `mem0.Memory.from_config` and asserts config dict shape.
4. Implement `app/auth.py` with `require_bearer` and `build_verifier()` (Phase 1).
5. Implement `app/rest.py` with all endpoints from §7.1. Add `tests/test_rest.py` covering auth + each endpoint with mem0 mocked.
6. Implement `app/mcp_server.py` with the six tools from §8.1. Add `tests/test_mcp.py` using an in-process `fastmcp.Client`.
7. Implement `app/main.py` wiring everything per §8.2. Add `/healthz` with the Qdrant connectivity check from §7.3.
8. Write `Dockerfile` and `captain-definition`. Confirm `docker build .` succeeds locally.
9. Write `.github/workflows/ci.yml`.
10. Write `scripts/smoke.sh` for the integration smoke test in §13.2.
11. Update `README.md` with everything from §15 (Phase 1 sections only — defer §14.3 and §14.4 to Milestone C).

### Milestone B — Backup service

12. Implement `backup/backup.sh`, `backup/crontab`, `backup/Dockerfile`, `backup/captain-definition`.
13. Build and test locally: `docker build backup/ -t mem0-backup` and confirm `crond` starts.
14. Run a one-shot test with real env vars against the live Qdrant: `docker run --rm <envs> mem0-backup /usr/local/bin/backup.sh` — verify an S3 object lands.

### Milestone C — Phase 2 OAuth

15. Implement `app/oauth.py` with discovery, JWKS, DCR, authorize, and token endpoints per §9.2.
16. Add SQLite storage as a small module (`app/oauth_store.py`).
17. Replace the MCP verifier with the composite verifier from §8.3.
18. Generate the RSA keypair, paste private key into CapRover env var, push.
19. Add `tests/test_oauth.py` covering: DCR with allowed redirect URI, DCR with disallowed URI rejected, full authorize → token → JWT verification flow, PKCE mismatch rejected, expired code rejected.
20. Update `README.md` with §14.3 and §14.4.

### Milestone D — Operational hardening

21. Add structured request logging via `structlog` with bearer redaction.
22. Add a `/metrics` endpoint (prometheus_client) exposing request counts, latency, memory ops per type. *Optional but recommended.*
23. Document monthly restore drill steps in `README.md`.

---

## 18. Acceptance criteria

The project is "done" for v1 when **all** of the following are true:

- [ ] Push to `main` triggers a CapRover build that completes successfully within 3 minutes.
- [ ] `curl https://mem0.your-domain.com/healthz` returns `{"ok": true}` after deploy.
- [ ] `curl -H "Authorization: Bearer $KEY" -X POST .../api/v1/memories -d '{"content":"test"}'` adds a memory and returns 200.
- [ ] `claude mcp add` with the bearer header succeeds and `claude mcp list` shows the server connected.
- [ ] In Claude Code, asking it to "remember that I prefer X" calls `add_memory`, and a subsequent session can recall it via `search_memories`.
- [ ] In Claude Desktop, the connector connects and the same recall flow works.
- [ ] In Claude.ai web (Phase 2), adding the connector triggers DCR, completes OAuth consent, and the recall flow works.
- [ ] In Cowork (Phase 2), the connector connects via OAuth and the recall flow works.
- [ ] The `mem0-backup` CapRover app runs at 03:00 UTC daily; the next morning, a new object appears in `s3://<bucket>/mem0-backups/`.
- [ ] Restoring from a snapshot to a test collection succeeds (documented drill).
- [ ] All unit tests pass in CI.
- [ ] No secrets are committed to the repo (verified by `git log -p | grep -i 'api_key\|secret\|password'` returning no matches in committed files).

---

## 19. Operational notes (read once, then forget)

- **Embedding dimension is sacred.** If you change embedding models, drop and recreate the Qdrant collection. Old vectors are not portable across dimensions.
- **Fact-extraction cost scales with `add_memory` call volume.** Each call invokes Claude Haiku. For personal use this is pennies/month. For automation that writes hundreds of memories/hour, monitor the Anthropic bill.
- **`stateless_http=True` is non-negotiable** when running uvicorn with `--workers > 1`.
- **CapRover persistent volume on `/app/data`** is only used by Phase 2 (OAuth SQLite). Phase 1 is fully stateless.
- **The bearer token in `MEM0_API_KEY` is high-trust** — anyone with it can read or write any memory. Rotate it if you suspect leakage; rotation just means setting a new value in CapRover env vars and updating client configs.
- **Snapshot files are not encrypted at rest in S3 by default.** Enable bucket-level SSE-S3 or SSE-KMS in the AWS console; the script doesn't need changes.
- **mem0 sometimes deduplicates memories** during `add()` — if you add the same fact twice, the second call may return an empty `results` array. This is correct behavior, not a bug.

---

## Appendix A: `.env.example`

```bash
# Qdrant
QDRANT_HOST=qdrant.your-domain.com
QDRANT_PORT=443
QDRANT_HTTPS=true
QDRANT_API_KEY=replace-me

# mem0 core
MEM0_COLLECTION=memories
MEM0_DEFAULT_USER_ID=default-user

# LLM for fact extraction
MEM0_LLM_PROVIDER=anthropic
MEM0_LLM_MODEL=claude-haiku-4-5-20251001
ANTHROPIC_API_KEY=replace-me

# Embeddings
MEM0_EMBED_PROVIDER=openai
MEM0_EMBED_MODEL=text-embedding-3-small
MEM0_EMBED_DIMS=1536
OPENAI_API_KEY=replace-me

# Auth
MEM0_API_KEY=replace-with-openssl-rand-hex-32
PUBLIC_BASE_URL=https://mem0.your-domain.com

# OAuth (Phase 2, leave blank for Phase 1)
OAUTH_SIGNING_KEY=
OAUTH_ALLOWED_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,https://cowork.com/api/mcp/auth_callback

# Misc
LOG_LEVEL=INFO
```

## Appendix B: `requirements.txt`

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
fastmcp==2.12.5
mem0ai==0.1.100
qdrant-client==1.13.0
openai==1.50.0
anthropic==0.39.0
pydantic==2.7.4
pydantic-settings==2.4.0
structlog==24.4.0
httpx==0.27.2
PyJWT[crypto]==2.9.0
```

(Adjust to the latest patch versions at implementation time; the constraints above are the minimum tested.)

## Appendix C: Useful CapRover CLI snippets

```bash
# Tail logs
caprover logs mem0-server -f

# Force redeploy (no code change)
caprover deploy

# Check env vars (paste to confirm what's set)
caprover api --path /user/apps/appDefinitions/mem0-server

# Test backup app one-shot
caprover api --path /user/apps/appData/mem0-backup --method POST \
  --data '{"captainExtra": {"runOnce": true}}'
```

---

**End of PRD.**
