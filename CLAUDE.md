# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

This repository is **implemented** (Phase 1 + Phase 2). The full app lives in `app/`, with a test suite in `tests/`, a root `Dockerfile`/`captain-definition`, a `backup/` app, and CI in `.github/workflows/ci.yml`. **`docs/PRD.md` remains the source of truth for intent and design**; read it before changing behavior. Section 17 of the PRD is the milestone-based task list it was built from.

Documentation:
- `docs/USER_GUIDE.md` — operators/end users: deploy to CapRover, configure, connect clients, REST API, backups, troubleshooting.
- `docs/DEVELOPER_GUIDE.md` — contributors: architecture, module map, request flow, auth, testing, how to make common changes.
- `docs/PRD.md` — the spec.

When code and the guides drift, update the guides in the same change.

## What this project is

A single self-hosted Python service that exposes one shared [mem0](https://github.com/mem0ai/mem0) memory store over **two protocols from one process**:
- **REST** (`/api/v1/memories...`) for scripts, n8n, curl.
- **Streamable HTTP MCP** (`/mcp/`) for Claude Code, Claude Desktop, Claude.ai web, Cowork.

It uses an existing external **Qdrant** instance as the vector backend, deploys to **CapRover** on push to `main`, and a companion `mem0-backup` app takes nightly Qdrant snapshots to S3. It is **single-user**: `MEM0_DEFAULT_USER_ID` is the only user; do not add multi-tenant logic.

Phase 1 (MVP) = static bearer-token auth. Phase 2 adds OAuth 2.1 + PKCE + DCR endpoints for Claude.ai web / Cowork, sharing the same FastAPI app and Qdrant collection.

## Architecture invariants (don't break these)

- **One `Memory` instance per process.** REST and MCP both share a single `mem0.Memory` (lazily built, `@lru_cache`) so the LLM/embedder clients aren't duplicated. This is the whole reason for the single-process, dual-protocol design — don't split them.
- **FastMCP lifespan must be passed to FastAPI's constructor.** FastMCP is mounted into FastAPI via `mcp.http_app(...)`; its lifespan has to be wired into the FastAPI `lifespan` or the first MCP request fails with `Task group is not initialized`. See PRD §8.2.
- **`stateless_http=True`** is required on `mcp.http_app()` — non-negotiable when running uvicorn with `--workers > 1`, or you get session-not-found errors.
- **MCP is mounted at the root (`app.mount("/", mcp_app)`) and must be registered LAST.** The endpoint is built at `path="/mcp"` with an extra `/mcp/` alias route so both `/mcp` and `/mcp/` serve directly (no 307) — strict OAuth clients POST to the exact resource URL and won't follow a redirect. The root mount is a catch-all, so every other route must be registered before it.
- **The OAuth protected-resource `resource` is the canonical URI without a trailing slash** (`<base>/mcp`). MCP clients canonicalize away the trailing slash and reject auth on a mismatch.
- **`MEM0_EMBED_DIMS` must match the embedder's real output dimension** (3-small=1536, 3-large=3072). A mismatch causes *silent* search failures, not errors. Changing embedding models requires dropping and recreating the Qdrant collection.
- **FastMCP = the PrefectHQ `fastmcp` PyPI package**, imported `from fastmcp import FastMCP`. It is NOT the older `mcp.server.fastmcp` module.
- **Same `MEM0_API_KEY` protects both** the REST endpoints (`require_bearer` dependency) and the MCP endpoint (`StaticTokenVerifier` in Phase 1).
- **The Phase 2 OAuth `/oauth/authorize` consent step authenticates the resource owner** by requiring `MEM0_API_KEY` (constant-time compare) before issuing a code. Don't remove this — the OAuth endpoints are public, so without it anyone reaching the consent screen could mint a token for the single user's memories.

## Planned layout (per PRD §3)

- `app/config.py` — `Settings` (pydantic-settings); single source of config truth, reject startup on missing required vars.
- `app/memory.py` — mem0 wrapper / `_build_config`; **the most tweak-prone file**.
- `app/mcp_server.py` — the six MCP tools (add/search/list/get/update/delete), each thinly wrapping a mem0 op with `user_id` defaulted.
- `app/rest.py` — REST router under `/api/v1`.
- `app/auth.py` — `require_bearer` + `build_verifier()` (Phase 1); composite bearer-or-JWT verifier (Phase 2).
- `app/oauth.py` / `app/oauth_store.py` — Phase 2 OAuth AS endpoints + SQLite store at `/app/data/oauth.db` (CapRover persistent volume).
- `app/main.py` — wiring (FastAPI + FastMCP mount + lifespan + `/healthz`).
- `backup/` — separate CapRover app: Alpine + crond running `backup.sh` nightly. No exposed ports.

## Commands (per PRD §13/§16)

```bash
# Install
pip install -r requirements.txt
pip install pytest pytest-asyncio respx ruff

# Lint + test
ruff check app/
pytest -q
pytest tests/test_rest.py::test_name   # single test

# Run locally
cp .env.example .env   # fill in values
uvicorn app.main:app --reload --port 8000
```

Dependencies are pinned in `requirements.txt` (per PRD Appendix B); CI is in `.github/workflows/ci.yml` (per PRD §13.3).

## Deployment notes

- Deploy is **push-to-`main` → CapRover webhook**, independent of CI status. The main app is stateless (Phase 1); only Phase 2 OAuth uses the `/app/data` persistent volume.
- `/healthz` does a real 2s-timeout round-trip to Qdrant and returns 503 if unreachable — keep the timeout so CapRover health checks don't hang.
- The `mem0-backup` app lives in this same repo as a second CapRover app (separate `captain-definition` / relative path). It snapshots Qdrant, uploads to S3, prunes by retention.
