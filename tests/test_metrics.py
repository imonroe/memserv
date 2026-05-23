from fastapi.testclient import TestClient


def test_metrics_endpoint_reports_requests(app_instance):
    client = TestClient(app_instance)
    # A 401 still flows through the middleware and matches the route, so the
    # metric is recorded without needing to mock the mem0 store.
    client.get("/api/v1/memories")

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    # The matched route template is used as the path label, not the raw path.
    assert 'path="/api/v1/memories"' in body
