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
