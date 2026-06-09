# Developer Guide

This guide is for **contributors** working on mem0-server's code. For deploying and using the
service, see the [User Guide](USER_GUIDE.md). The full product spec lives in [`PRD.md`](PRD.md) and
remains the source of truth for intent.

- [Design in one paragraph](#design-in-one-paragraph)
- [Architecture invariants](#architecture-invariants)
- [Project layout](#project-layout)
- [Request and data flow](#request-and-data-flow)
- [Authentication](#authentication)
- [Local setup](#local-setup)
- [Testing](#testing)
- [Linting and style](#linting-and-style)
- [How to make common changes](#how-to-make-common-changes)
- [Configuration internals](#configuration-internals)
- [Observability internals](#observability-internals)
- [CI and deployment](#ci-and-deployment)

## Design in one paragraph

A single Python process runs **FastAPI** with a **FastMCP** server mounted under `/mcp`. Both the
REST router and the MCP tools call into **one shared `mem0.Memory` instance** (lazily built and
`@lru_cache`d), which talks to an external **Qdrant** for vector storage, an LLM for fact
extraction, and an embedder for vectors. The single-process, dual-protocol design exists
specifically so the LLM/embedder clients aren't duplicated across two services.

## Architecture invariants

These are load-bearing. Breaking one tends to cause silent or hard-to-diagnose failures. (They are
also enumerated in `CLAUDE.md`.)

1. **One `Memory` instance per process.** REST (`app/rest.py`) and MCP (`app/mcp_server.py`) both
   call `memory.get_memory()`, which is `@lru_cache`d in `app/memory.py`. Don't introduce a second
   instance or split the protocols into separate services.
2. **FastMCP's lifespan must be passed to FastAPI's constructor.** In `app/main.py`, the FastAPI
   `lifespan` context manager wraps `mcp_app.lifespan(app)`. Without this, the first MCP request
   raises `Task group is not initialized`.
3. **`stateless_http=True` on `mcp.http_app()`** is required because the container runs uvicorn with
   `--workers 2`. Stateful sessions would produce session-not-found errors across workers.
4. **The MCP app is mounted at the root (`app.mount("/", mcp_app)`), registered LAST**, with the
   FastMCP endpoint built at `path="/mcp"` plus an explicit `/mcp/` alias route. This serves both
   `/mcp` and `/mcp/` directly (no 307 redirect) — strict clients like Claude.ai web POST to the
   exact resource URL and won't follow a redirect. Because the root mount is a catch-all, every
   other route (`/api/v1`, `/oauth`, `/.well-known`, `/metrics`, `/healthz`) MUST be registered
   before it or it will be shadowed.
5. **`MEM0_EMBED_DIMS` must equal the embedder's real output dimension.** A mismatch produces
   *silent* empty searches, not an exception. Changing the embedding model requires dropping and
   recreating the Qdrant collection.
6. **FastMCP is the PrefectHQ `fastmcp` PyPI package** (`from fastmcp import FastMCP`), **not** the
   older `mcp.server.fastmcp` module.
7. **The same `MEM0_API_KEY` protects both protocols** — `require_bearer` for REST and the token
   verifier for MCP. Keep them in sync.

## Project layout

```
app/
  config.py         Settings (pydantic-settings); single source of config truth. Rejects
                    startup on missing required vars; validates provider keys.
  memory.py         mem0 wrapper. _build_config() assembles the mem0 config dict; get_memory()
                    is the @lru_cache'd shared instance. add_memory() wraps mem0's add with a
                    cheap content-fingerprint dedup: it SHA-256s the normalized raw input, stores
                    it in the `content_fp` payload field, and skips the LLM extraction if a memory
                    with that fingerprint already exists (fail-open — a lookup error just proceeds).
                    keyword_search() is the substring-match fallback behind search mode="keyword":
                    it scans the user's memories via vector_store.list() and matches the query as a
                    case-insensitive substring of the `data` payload (fail-open). drop_expired()
                    removes results whose provenance `expires_at` is past. list_paginated()
                    implements offset paging for list reads: mem0's get_all has no offset, so it
                    over-fetches offset+limit+1 (capped by MAX_LIST_OFFSET) and slices, using the
                    extra item as the has_more signal. bulk_delete() backs
                    POST /memories/delete_bulk: dry-run by default, capped at BULK_DELETE_MAX
                    per call (has_more signals the caller to loop), and deletes through
                    Memory.delete per ID — never a raw vector-store filter delete — so mem0's
                    history stays consistent. Deliberately NOT exposed as an MCP tool: a
                    destructive filter-delete is an operator action, not something a model
                    should reach for. The most tweak-prone file.
  mcp_server.py     build_mcp(): the six MCP tools, each thinly wrapping a mem0 op with
                    user_id defaulted to MEM0_DEFAULT_USER_ID. list_memories pages (default 50,
                    max 100 per call) so the whole store is never returned in one response.
  rest.py           REST router under /api/v1 (mounted with prefix in main.py). Pydantic request
                    models, _scope_kwargs() for user/agent/run scoping, _provenance_filters() for
                    the source/confidence/review_status metadata convention, check_qdrant() helper.
  ranking.py        rerank_by_recency(): optional, opt-in post-search re-ranking that blends
                    mem0's similarity score with a recency decay. No-op when recency_weight=0,
                    so default REST/MCP search behavior is unchanged.
  auth.py           require_bearer (REST dependency), CompositeVerifier and StaticTokenVerifier
                    wiring, build_verifier() selecting Phase 1 vs Phase 2.
  oauth.py          Phase 2 OAuth 2.1 + PKCE + DCR endpoints, JWT issuance, JWKS, AS/PR metadata.
  oauth_store.py    SQLite store for OAuth clients, auth codes, refresh tokens (/app/data/oauth.db).
  errors.py         classify_exception(): maps concrete SDK exceptions (qdrant/httpx -> 503,
                    openai/anthropic -> 502, else 500) to sanitized JSON via the app-level
                    exception handler in main.py. New mem0 call sites need no wrapping; if a new
                    backend dependency is added, add its exception types to the tuples here.
  ratelimit.py      Per-IP fixed-window rate limiting of *failed* auth attempts, applied as the
                    rate_limit_middleware over four surfaces: REST (/api/v1), MCP (/mcp), OAuth
                    consent (POST /oauth/authorize) and token (/oauth/token). In-process state,
                    per worker; client_ip() honors X-Forwarded-For when TRUST_FORWARDED_FOR=true.
  metrics.py        Prometheus Counter + Histogram and observe_request().
  logging_setup.py  structlog configuration.
  main.py           Wiring: FastAPI app, request-logging middleware, router include, conditional
                    OAuth mount, MCP mount, /metrics, /healthz, lifespan.

backup/
  backup.sh         Nightly snapshot → S3 → prune script.
  crontab           cron schedule (03:00 UTC).
  Dockerfile        Alpine + crond.
  captain-definition  CapRover build descriptor for the separate backup app.

digest/             Optional companion app (separate, port-less CapRover app): a cron container
                    that summarizes recent memories and posts them to a Slack/Discord webhook.
  digest.py         Pure, unit-tested helpers (filter_recent, build_digest, …) plus main().
  entrypoint.sh     Snapshots env to a quoted file, builds the crontab from $DIGEST_CRON, runs crond.
  Dockerfile        Python 3.12-alpine + dcron. Not part of the main app image.
  requirements.txt  httpx only.
  captain-definition  CapRover build descriptor for the digest app.

capture/            Optional companion app (separate, port-less CapRover app): a long-running
  capture.py        Telegram bot that long-polls for messages and saves them via the REST API,
  Dockerfile        tagged agent_id=capture:telegram. Pure, unit-tested helpers (extract_message,
  requirements.txt  classify, process_update) + run() loop. Only allowlisted chat IDs may save.
  captain-definition  Python 3.12-alpine, httpx only. Not part of the main app image.

tests/              pytest suite, one file per module.
scripts/            smoke.sh (REST) and smoke_mcp.py (MCP) against a live server; import_*.py
                    data-import CLIs (thin wrappers over the importers package).
importers/          Standalone import tooling (not part of the app image): client.py (a retrying
                    REST client) plus pure parsers (chatgpt.py, obsidian.py, readwise.py) and the
                    shared CLI runner (cli.py). Each parser yields MemoryClient.add kwargs.
docs/               PRD.md (spec), USER_GUIDE.md, DEVELOPER_GUIDE.md.
Dockerfile          Main app image; runs uvicorn with --workers 2.
captain-definition  CapRover build descriptor for the main app.
docker-compose.yml  Self-contained stack (Qdrant + app) for non-CapRover hosts.
```

## Request and data flow

```
HTTP request
  → log_requests middleware (assigns request_id, times the request, records metrics)
  → route:
      /api/v1/*   → require_bearer dependency → REST handler → memory.get_memory().<op>()
      /mcp/*      → FastMCP (token verifier) → @mcp.tool function → memory.get_memory().<op>()
      /oauth/*, /.well-known/*  → OAuth endpoints (Phase 2 only)
      /healthz    → check_qdrant() round-trip
      /metrics    → Prometheus exposition
  → memory.get_memory() → mem0.Memory → Qdrant + LLM + embedder
```

Both REST and MCP converge on the same `mem0.Memory` instance. The REST layer adds Pydantic
validation and explicit `user_id`/`agent_id`/`run_id`/`metadata` scoping via `_scope_kwargs()`; the
MCP tools default `user_id` to the single configured user and expose a narrower surface.

## Authentication

`app/auth.py` centralizes auth. `build_verifier()` chooses the strategy at startup based on
`settings.oauth_enabled` (true iff `OAUTH_SIGNING_KEY` is set):

- **Phase 1** — `StaticTokenVerifier` for MCP and `require_bearer` for REST. Both compare the
  presented bearer token against `MEM0_API_KEY` using `secrets.compare_digest` (constant-time).
- **Phase 2** — `CompositeVerifier` accepts **either** the static bearer token **or** an
  OAuth-issued RS256 JWT validated against the public key, with `audience="mem0-server"` and the
  issuer set to `PUBLIC_BASE_URL`. REST still uses the static `require_bearer`. The verifier is
  wrapped in a FastMCP `RemoteAuthProvider` so the mounted MCP app returns a 401 whose
  `WWW-Authenticate` header carries `resource_metadata="…"` (RFC 9728) — without that pointer,
  OAuth MCP clients can't discover the authorization server. Because FastMCP is mounted under
  `/mcp` by the outer FastAPI app and is unaware of that prefix, `resource_base_url` is set
  explicitly to `<PUBLIC_BASE_URL>/mcp` so the advertised resource is correct.

The OAuth flow (`app/oauth.py`) is OAuth 2.1 with PKCE (S256 required) and public clients only — no
client secrets are issued. The `/oauth/authorize` consent step **authenticates the resource owner**:
the POST handler requires the `MEM0_API_KEY` (constant-time compared) before issuing a code. Without
this gate, anyone who reached the public consent screen could mint a token for the single user's
memories just by clicking "Authorize". Endpoints: `/oauth/register` (DCR), `/oauth/authorize` (GET form + POST
consent), `/oauth/token` (authorization_code + refresh_token grants), `/.well-known/jwks.json`, and
the RFC 8414 / RFC 9728 metadata documents. Tokens live 24h; refresh tokens are single-use and
rotated. `oauth_store.py` persists clients/codes/refresh tokens in SQLite, hashing refresh tokens
so a DB leak doesn't expose usable tokens, and using `DELETE … RETURNING` for atomic single-use
consumption.

## Local setup

Requires Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio respx ruff

cp .env.example .env   # fill in values

uvicorn app.main:app --reload --port 8000
```

The tests mock mem0/Qdrant, so you can run the suite without real credentials or a running Qdrant.
Running the actual server (`uvicorn`) does require valid config and reachable backends.

## Testing

```bash
pytest -q                                  # whole suite
pytest tests/test_rest.py                   # one file
pytest tests/test_rest.py::test_add_memory  # one test
```

- `asyncio_mode = "auto"` (in `pyproject.toml`), so `async def test_*` functions run without an
  explicit marker.
- `respx` is used to mock outbound HTTP (e.g. the Qdrant health check).
- `app/memory.py` imports `mem0` lazily inside `get_memory()` specifically so tests can patch it
  without a real mem0/Qdrant install. Mock at that boundary.
- There is one test file per `app/` module (`test_auth.py`, `test_config.py`, `test_logging.py`,
  `test_main.py`, `test_mcp.py`, `test_memory.py`, `test_metrics.py`, `test_oauth.py`,
  `test_rest.py`). Add tests to the matching file.

## Linting and style

```bash
ruff check app/
```

Ruff config (in `pyproject.toml`): line length 100, target py312, rule sets `E, F, I, UP, B`. CI
runs `ruff check app/` and fails on violations, so run it before pushing.

Comment style for this repo: explain **why**, not **what**. Comments that survive in the code call
out non-obvious constraints (the lifespan wiring, `stateless_http`, the embed-dims trap,
constant-time compares). Don't add narration of what well-named code already says.

## How to make common changes

**Add a REST endpoint** — add a handler to `app/rest.py` on `router` (which already carries the
`require_bearer` dependency). Reuse `_scope_kwargs()` for user/agent/run scoping. Add a Pydantic
request model if it takes a body. Add a test to `tests/test_rest.py`.

**Add an MCP tool** — add a `@mcp.tool`-decorated function inside `build_mcp()` in
`app/mcp_server.py`. Default `user_id` to `default_user`. Write a clear docstring — MCP clients show
it to the model as the tool description. Add a test to `tests/test_mcp.py`.

**Add a config option** — add a field to `Settings` in `app/config.py` (with a default if optional),
add it to `.env.example`, and document it in the User Guide's configuration table. If it gates a
provider, extend the `_require_provider_keys` validator.

**Change the mem0 / Qdrant config** — edit `_build_config()` in `app/memory.py`. This is the most
sensitive file; mind the embed-dims invariant and the scheme-aware Qdrant URL (mem0's Qdrant store
honors the scheme only via `url`, not a separate flag).

**Touch OAuth** — `app/oauth.py` for endpoints/flows, `app/oauth_store.py` for persistence. Keep
PKCE S256 mandatory and clients public (no secrets). Update `tests/test_oauth.py`.

## Configuration internals

`Settings` (pydantic-settings) reads from environment and `.env`, with `extra="ignore"`. The
`_require_provider_keys` model validator enforces that `ANTHROPIC_API_KEY` is present when the LLM
provider is Anthropic, and `OPENAI_API_KEY` when the embed provider is OpenAI — failing fast at
startup rather than at first request. `get_settings()` is `@lru_cache`d, so settings are read once
per process. `oauth_enabled` and `allowed_redirect_uris_list` are derived properties.

`app/memory.py`'s `_provider_config()` injects the API key into the mem0 provider config explicitly,
because mem0's provider clients otherwise read keys from `os.environ`, which is not populated when
keys come only from a `.env` file via pydantic-settings.

## Observability internals

The `log_requests` middleware in `app/main.py` assigns a `request_id` (from an inbound
`x-request-id` header or a generated short hex), binds it into structlog context, times the request,
and on completion records Prometheus metrics and emits a structured log line. Metrics are labelled
by the **matched route template** (e.g. `/api/v1/memories/{memory_id}`), not the raw path, to keep
label cardinality bounded; unmatched 404s bucket under `__unmatched__`. Under multiple workers,
`/metrics` aggregates across workers when `PROMETHEUS_MULTIPROC_DIR` is set. The middleware never
reads the `Authorization` header, so tokens are never logged.

`rate_limit_middleware` (`app/ratelimit.py`) is registered *before* `log_requests` so logging wraps
it and 429s still get a log line. It counts failed-auth responses (401s; also 400s on
`/oauth/token`, which is what RFC 6749 returns for guessed codes) per client IP per surface, and
rejects an over-limit IP with 429 + `Retry-After` before the request reaches auth. Two metrics
track it: `auth_failures_total{surface}` and `rate_limited_requests_total{surface}` — a spike in
either is a brute-force signal. State is in-process and per worker by design (single-user service;
no shared Redis). Tests must not leak limiter state: `tests/conftest.py` has an autouse fixture
calling `ratelimit.reset_all()`.

## CI and deployment

- **CI** (`.github/workflows/ci.yml`) runs on pushes to `main` and on all PRs: installs deps, then
  `ruff check app/` and `pytest -q` on Python 3.12.
- **Deploy** has two supported paths (same app image, different infrastructure):
  - **CapRover** — push-to-`main` → CapRover webhook, independent of CI status. The main app builds
    from the root `Dockerfile` / `captain-definition` and runs `uvicorn app.main:app --workers 2`.
    Connects to an external Qdrant.
  - **Docker Compose** — `docker-compose.yml` builds the same `Dockerfile` and brings up the app
    alongside a bundled Qdrant service, overriding `QDRANT_HOST`/`QDRANT_PORT`/`QDRANT_HTTPS` to the
    in-stack service. See the [User Guide](USER_GUIDE.md#deploying-with-docker-compose).
- The **backup app** is a second CapRover app built from `backup/` (separate `captain-definition`).
  See the [User Guide](USER_GUIDE.md#2-deploy-the-backup-app-mem0-backup).
- The main app is stateless in Phase 1; only Phase 2 OAuth uses the `/app/data` persistent volume
  for `oauth.db`.
