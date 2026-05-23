from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.logging_setup import configure_logging
from app.mcp_server import build_mcp
from app.rest import check_qdrant
from app.rest import router as rest_router

configure_logging()
settings = get_settings()

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

app.include_router(rest_router, prefix="/api/v1")

if settings.oauth_enabled:
    from app import oauth_store
    from app.oauth import router as oauth_router

    oauth_store.init_db()
    app.include_router(oauth_router)

app.mount("/mcp", mcp_app)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    reachable = await check_qdrant()
    if not reachable:
        return JSONResponse(status_code=503, content={"ok": False, "qdrant": "unreachable"})
    return JSONResponse(
        content={"ok": True, "version": app.version, "qdrant": "reachable"}
    )
