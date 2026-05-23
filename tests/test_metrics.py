from fastapi.testclient import TestClient


def test_metrics_endpoint_reports_requests(app_instance):
    client = TestClient(app_instance)
    # Hit a parameterized route (no auth -> 401, but the route still matches).
    # Using a concrete id proves the metric label uses the route template, not
    # the raw path containing the id.
    client.get("/api/v1/memories/abc123")

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    # The matched route template is used as the path label, not the raw id.
    assert 'path="/api/v1/memories/{memory_id}"' in body
    assert "abc123" not in body
