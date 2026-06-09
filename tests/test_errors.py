import anthropic
import httpx
import openai
from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import ResponseHandlingException

from app.errors import classify_exception

# ---------------------------------------------------------------------------
# classify_exception unit tests
# ---------------------------------------------------------------------------


def test_classifies_provider_errors_as_502():
    for exc in (openai.OpenAIError("boom"), anthropic.AnthropicError("boom")):
        status, code, _ = classify_exception(exc)
        assert (status, code) == (502, "upstream_provider_error")


def test_classifies_backend_errors_as_503():
    for exc in (
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        ResponseHandlingException("qdrant glitch"),
        ConnectionError("reset"),
        TimeoutError(),
    ):
        status, code, _ = classify_exception(exc)
        assert (status, code) == (503, "backend_unavailable")


def test_provider_wins_over_wrapped_network_error():
    # openai.APIConnectionError is an OpenAIError wrapping an httpx error; it
    # must classify as a provider failure, not vector-store trouble.
    exc = openai.APIConnectionError(request=httpx.Request("GET", "http://x"))
    status, code, _ = classify_exception(exc)
    assert (status, code) == (502, "upstream_provider_error")


def test_unknown_errors_are_500():
    status, code, detail = classify_exception(RuntimeError("secret-host-name"))
    assert (status, code) == (500, "internal_error")
    assert "secret-host-name" not in detail


# ---------------------------------------------------------------------------
# Handler wired into the real app
# ---------------------------------------------------------------------------


def _failing_search(app_instance, mem, auth_header, exc):
    mem.search.side_effect = exc
    client = TestClient(app_instance, raise_server_exceptions=False)
    return client.post(
        "/api/v1/memories/search", json={"query": "x"}, headers=auth_header
    )


def test_qdrant_down_yields_503_json(app_instance, mem, auth_header):
    resp = _failing_search(
        app_instance, mem, auth_header, httpx.ConnectError("connection refused")
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "backend_unavailable"
    assert body["request_id"]


def test_provider_failure_yields_502(app_instance, mem, auth_header):
    resp = _failing_search(
        app_instance, mem, auth_header, anthropic.AnthropicError("bad key")
    )
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_provider_error"


def test_unexpected_error_yields_sanitized_500(app_instance, mem, auth_header):
    resp = _failing_search(
        app_instance, mem, auth_header, RuntimeError("qdrant.internal:6333 exploded")
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_error"
    # No exception text leaks; the request_id is the correlation handle instead.
    assert "qdrant.internal" not in resp.text
    assert body["request_id"]


def test_request_id_header_round_trips_into_error_body(app_instance, mem, auth_header):
    mem.search.side_effect = RuntimeError("boom")
    client = TestClient(app_instance, raise_server_exceptions=False)
    resp = client.post(
        "/api/v1/memories/search",
        json={"query": "x"},
        headers={**auth_header, "X-Request-Id": "trace-me-123"},
    )
    assert resp.json()["request_id"] == "trace-me-123"
