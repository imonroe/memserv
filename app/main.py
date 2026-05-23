import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.config import get_settings
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
mcp_app = mcp.http_app(path="/", stateless_http=True, transport="streamable-http")


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
    start = time.perf_counter()
    status = 500  # if call_next raises, the request is logged as a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        # Use the matched route template (e.g. /api/v1/memories/{memory_id})
        # to keep metric label cardinality bounded.
        route = request.scope.get("route")
        metric_path = getattr(route, "path", request.url.path)
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


app.include_router(rest_router, prefix="/api/v1")

if settings.oauth_enabled:
    from app import oauth_store
    from app.oauth import router as oauth_router

    oauth_store.init_db()
    app.include_router(oauth_router)

app.mount("/mcp", mcp_app)


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    reachable = await check_qdrant()
    if not reachable:
        return JSONResponse(status_code=503, content={"ok": False, "qdrant": "unreachable"})
    return JSONResponse(
        content={"ok": True, "version": app.version, "qdrant": "reachable"}
    )
