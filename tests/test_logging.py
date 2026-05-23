import json

from fastapi.testclient import TestClient
from structlog.testing import capture_logs


def test_request_is_logged_without_token(app_instance):
    secret = "super-secret-token-value"
    with capture_logs() as logs:
        TestClient(app_instance).get(
            "/api/v1/memories", headers={"Authorization": f"Bearer {secret}"}
        )

    request_logs = [e for e in logs if e.get("event") == "request"]
    assert request_logs, "expected a 'request' log event"
    entry = request_logs[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/v1/memories"
    assert "request_id" in entry
    assert "ms" in entry

    # The bearer token must never appear anywhere in the structured logs.
    assert secret not in json.dumps(logs)


def test_request_logged_as_500_when_handler_raises(app_instance):
    import structlog
    from fastapi.routing import APIRoute

    async def _boom():
        raise RuntimeError("kaboom")

    # Insert at the front: the app mounts the MCP sub-app at "/" last, which would
    # otherwise shadow a route appended after it.
    app_instance.router.routes.insert(0, APIRoute("/boom-test", _boom, methods=["GET"]))

    client = TestClient(app_instance, raise_server_exceptions=False)
    with capture_logs() as logs:
        client.get("/boom-test")

    entry = next(e for e in logs if e.get("event") == "request" and e["path"] == "/boom-test")
    assert entry["status"] == 500
    # contextvars must be cleared even on failure (no request_id leaks).
    assert "request_id" not in structlog.contextvars.get_contextvars()
