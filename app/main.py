import os
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.routing import Route

from app.config import get_settings
from app.errors import classify_exception
from app.logging_setup import configure_logging
from app.mcp_server import build_mcp
from app.metrics import observe_request
from app.rest import check_qdrant
from app.rest import router as rest_router

configure_logging()
settings = get_settings()
_log = structlog.get_logger()

mcp = build_mcp()
# stateless_http=True is required to avoid session-not-found errors with >1 worker.
# The endpoint is served at /mcp (not /mcp/): mounting at the root below, plus the
# /mcp/ alias route added here, lets BOTH /mcp and /mcp/ resolve directly without a
# 307 redirect. Strict MCP clients (Claude.ai web / Cowork) POST to the exact
# advertised resource URL and don't follow the redirect, so a redirect breaks them.
mcp_app = mcp.http_app(path="/mcp", stateless_http=True, transport="streamable-http")
_mcp_route = next(
    (r for r in mcp_app.router.routes if getattr(r, "path", None) == "/mcp"), None
)
if _mcp_route is None:
    raise RuntimeError(
        "FastMCP did not register the expected /mcp route; cannot add the /mcp/ alias. "
        "Check the fastmcp version and the http_app(path=...) argument."
    )
mcp_app.router.routes.append(
    Route("/mcp/", _mcp_route.endpoint, methods=list(_mcp_route.methods))
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FastMCP's lifespan MUST run, or the first MCP request raises
    # "Task group is not initialized".
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="mem0 Memory Server", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # The Authorization header is never read here, so tokens are never logged.
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(request_id=request_id)
    # Also stashed on request.state for the exception handler: by the time it
    # runs, this middleware's finally block has already cleared the contextvars.
    request.state.request_id = request_id
    start = time.perf_counter()
    status = 500  # if call_next raises, the request is logged as a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        # Use the matched route template (e.g. /api/v1/memories/{memory_id}) to
        # keep label cardinality bounded. Unmatched (404) requests have no route,
        # so bucket them under a fixed label instead of the arbitrary raw path.
        route = request.scope.get("route")
        metric_path = getattr(route, "path", None)
        if not metric_path:
            # Requests served by the root-mounted MCP app have no route at this
            # outer level. Bucket the two MCP path variants under a single stable
            # label; anything else that fell through is genuinely unmatched.
            metric_path = (
                "/mcp" if request.url.path.rstrip("/") == "/mcp" else "__unmatched__"
            )
        observe_request(request.method, metric_path, status, elapsed)
        _log.info(
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=status,
            ms=round(elapsed * 1000, 1),
        )
        structlog.contextvars.clear_contextvars()


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate unhandled errors into stable, sanitized JSON.

    Backend (Qdrant/network) failures become 503, model-provider failures 502,
    everything else a generic 500. The response never includes exception text —
    it carries the request_id instead, which correlates with the server-side
    log line holding the full traceback.
    """
    status, code, detail = classify_exception(exc)
    request_id = getattr(request.state, "request_id", None)
    _log.error(
        "unhandled_exception",
        request_id=request_id,
        error_code=code,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "error": code, "request_id": request_id},
    )


app.include_router(rest_router, prefix="/api/v1")

if settings.oauth_enabled:
    from app import oauth_store
    from app.oauth import router as oauth_router

    oauth_store.init_db()
    app.include_router(oauth_router)


@app.get("/metrics")
def metrics() -> Response:
    # The Dockerfile runs uvicorn with --workers 2; generate_latest() on the
    # default registry only sees the worker that served this scrape. When
    # PROMETHEUS_MULTIPROC_DIR is set, aggregate across workers instead.
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import CollectorRegistry, multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        data = generate_latest(registry)
    else:
        data = generate_latest()
    return Response(data, media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    reachable = await check_qdrant()
    if not reachable:
        return JSONResponse(status_code=503, content={"ok": False, "qdrant": "unreachable"})
    return JSONResponse(
        content={"ok": True, "version": app.version, "qdrant": "reachable"}
    )


# Mounted at the root LAST so the specific routes above (/api/v1, /oauth,
# /.well-known, /metrics, /healthz) take precedence; the MCP app only owns
# /mcp and /mcp/ and 404s everything else.
app.mount("/", mcp_app)
