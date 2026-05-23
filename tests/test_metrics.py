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


def test_mcp_requests_get_stable_metric_label(app_instance):
    # The MCP app is mounted at the root, so its requests have no matched route
    # at the middleware level. They must still get a stable "/mcp" label (both
    # slash variants), while genuine fallthrough 404s bucket under __unmatched__.
    with TestClient(app_instance) as client:
        headers = {"Accept": "application/json, text/event-stream"}
        client.post("/mcp", json={}, headers=headers, follow_redirects=False)
        client.post("/mcp/", json={}, headers=headers, follow_redirects=False)
        client.get("/totally-unknown-path")
        body = client.get("/metrics").text
    assert 'path="/mcp"' in body
    assert 'path="__unmatched__"' in body
