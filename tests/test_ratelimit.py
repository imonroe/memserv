from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import ratelimit
from app.ratelimit import RateLimiter, client_ip, rate_limit_middleware

# ---------------------------------------------------------------------------
# RateLimiter unit tests
# ---------------------------------------------------------------------------


def test_under_limit_not_limited():
    limiter = RateLimiter(max_failures=3, window_seconds=60)
    limiter.record_failure("ip", now=0.0)
    limiter.record_failure("ip", now=1.0)
    assert limiter.retry_after("ip", now=2.0) is None


def test_limited_at_threshold_with_retry_after():
    limiter = RateLimiter(max_failures=3, window_seconds=60)
    for t in (0.0, 1.0, 2.0):
        limiter.record_failure("ip", now=t)
    retry = limiter.retry_after("ip", now=10.0)
    assert retry == pytest.approx(50.0)


def test_window_expiry_unblocks():
    limiter = RateLimiter(max_failures=2, window_seconds=60)
    limiter.record_failure("ip", now=0.0)
    limiter.record_failure("ip", now=1.0)
    assert limiter.retry_after("ip", now=30.0) is not None
    assert limiter.retry_after("ip", now=60.0) is None


def test_failure_after_expired_window_starts_fresh():
    limiter = RateLimiter(max_failures=2, window_seconds=60)
    limiter.record_failure("ip", now=0.0)
    limiter.record_failure("ip", now=1.0)
    # New failure in a fresh window: count restarts at 1, not 3.
    limiter.record_failure("ip", now=120.0)
    assert limiter.retry_after("ip", now=121.0) is None


def test_keys_are_independent():
    limiter = RateLimiter(max_failures=1, window_seconds=60)
    limiter.record_failure("a", now=0.0)
    assert limiter.retry_after("a", now=1.0) is not None
    assert limiter.retry_after("b", now=1.0) is None


def test_disabled_when_max_failures_below_one():
    limiter = RateLimiter(max_failures=0, window_seconds=60)
    for t in range(100):
        limiter.record_failure("ip", now=float(t))
    assert limiter.retry_after("ip", now=1.0) is None


def test_prune_caps_tracked_keys():
    limiter = RateLimiter(max_failures=5, window_seconds=60)
    for i in range(ratelimit._MAX_TRACKED_KEYS):
        limiter.record_failure(f"ip-{i}", now=0.0)
    # All previous windows are expired by now=100; the next failure prunes them.
    limiter.record_failure("fresh", now=100.0)
    assert len(limiter._windows) == 1


def test_reset_clears_state():
    limiter = RateLimiter(max_failures=1, window_seconds=60)
    limiter.record_failure("ip", now=0.0)
    limiter.reset()
    assert limiter.retry_after("ip", now=1.0) is None


# ---------------------------------------------------------------------------
# client_ip extraction
# ---------------------------------------------------------------------------


def _fake_request(xff: str | None, peer: str | None = "10.0.0.1"):
    request = MagicMock()
    request.headers = {"x-forwarded-for": xff} if xff is not None else {}
    request.client = MagicMock(host=peer) if peer else None
    return request


def test_client_ip_uses_first_forwarded_hop():
    assert client_ip(_fake_request("1.2.3.4, 5.6.7.8")) == "1.2.3.4"


def test_client_ip_falls_back_to_peer():
    assert client_ip(_fake_request(None)) == "10.0.0.1"
    assert client_ip(_fake_request("")) == "10.0.0.1"


def test_client_ip_ignores_header_when_untrusted(monkeypatch):
    class _S:
        trust_forwarded_for = False

    monkeypatch.setattr(ratelimit, "get_settings", lambda: _S())
    assert client_ip(_fake_request("1.2.3.4")) == "10.0.0.1"


def test_client_ip_no_peer():
    assert client_ip(_fake_request(None, peer=None)) == "unknown"


# ---------------------------------------------------------------------------
# Surface classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/api/v1/memories", "POST", "rest"),
        ("/api/v1/memories/abc", "GET", "rest"),
        ("/mcp", "POST", "mcp"),
        ("/mcp/", "POST", "mcp"),
        ("/oauth/authorize", "POST", "oauth_consent"),
        ("/oauth/authorize", "GET", None),  # form render checks no secret
        ("/oauth/token", "POST", "oauth_token"),
        ("/healthz", "GET", None),
        ("/metrics", "GET", None),
        ("/api/v1x", "GET", None),  # prefix must be a path segment
    ],
)
def test_surface_classification(path, method, expected):
    assert ratelimit._surface(path, method) == expected


# ---------------------------------------------------------------------------
# Middleware behavior (stub app exercising every surface)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_client():
    app = FastAPI()
    app.middleware("http")(rate_limit_middleware)

    @app.get("/api/v1/memories")
    def rest_endpoint(fail: bool = False):
        if fail:
            raise HTTPException(status_code=401)
        return {"ok": True}

    @app.post("/oauth/token")
    def token_endpoint(fail: bool = False):
        if fail:
            raise HTTPException(status_code=400)
        return {"ok": True}

    @app.post("/oauth/authorize")
    def consent_endpoint(fail: bool = False):
        if fail:
            raise HTTPException(status_code=401)
        return {"ok": True}

    @app.get("/healthz")
    def health_endpoint():
        raise HTTPException(status_code=401)

    return TestClient(app)


def _hammer(client, n, path, ip, method="GET"):
    for _ in range(n):
        resp = client.request(
            method, path, params={"fail": "true"}, headers={"X-Forwarded-For": ip}
        )
    return resp


def test_rest_failures_trigger_429(stub_client):
    resp = _hammer(stub_client, 10, "/api/v1/memories", "9.9.9.1")
    assert resp.status_code == 401
    resp = stub_client.get(
        "/api/v1/memories", headers={"X-Forwarded-For": "9.9.9.1"}
    )
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_successes_never_count(stub_client):
    for _ in range(30):
        resp = stub_client.get(
            "/api/v1/memories", headers={"X-Forwarded-For": "9.9.9.2"}
        )
        assert resp.status_code == 200


def test_other_ips_unaffected(stub_client):
    _hammer(stub_client, 10, "/api/v1/memories", "9.9.9.3")
    resp = stub_client.get(
        "/api/v1/memories", headers={"X-Forwarded-For": "9.9.9.4"}
    )
    assert resp.status_code == 200


def test_unclassified_paths_never_limited(stub_client):
    for _ in range(30):
        resp = stub_client.get("/healthz", headers={"X-Forwarded-For": "9.9.9.5"})
        assert resp.status_code == 401  # the stub 401s, but never 429


def test_oauth_token_400_counts_as_failure(stub_client):
    resp = _hammer(stub_client, 10, "/oauth/token", "9.9.9.6", method="POST")
    assert resp.status_code == 400
    resp = stub_client.post("/oauth/token", headers={"X-Forwarded-For": "9.9.9.6"})
    assert resp.status_code == 429


def test_oauth_consent_stricter_limit(stub_client):
    resp = _hammer(stub_client, 5, "/oauth/authorize", "9.9.9.7", method="POST")
    assert resp.status_code == 401
    resp = stub_client.post(
        "/oauth/authorize", headers={"X-Forwarded-For": "9.9.9.7"}
    )
    assert resp.status_code == 429


def test_surfaces_limited_independently(stub_client):
    # Locking out the REST surface must not lock out the token endpoint.
    _hammer(stub_client, 10, "/api/v1/memories", "9.9.9.8")
    resp = stub_client.post("/oauth/token", headers={"X-Forwarded-For": "9.9.9.8"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Wired into the real app
# ---------------------------------------------------------------------------


def test_main_app_limits_bad_bearer_tokens(app_instance, mem):
    client = TestClient(app_instance)
    headers = {
        "Authorization": "Bearer wrong-token",
        "X-Forwarded-For": "8.8.8.1",
    }
    for _ in range(10):
        resp = client.get("/api/v1/memories", headers=headers)
        assert resp.status_code == 401
    resp = client.get("/api/v1/memories", headers=headers)
    assert resp.status_code == 429
    # Even a valid token is locked out from that IP until the window expires.
    resp = client.get(
        "/api/v1/memories",
        headers={
            "Authorization": "Bearer test-bearer-token",
            "X-Forwarded-For": "8.8.8.1",
        },
    )
    assert resp.status_code == 429


def test_main_app_healthz_unaffected_by_lockout(app_instance, mem, monkeypatch):
    import app.main as main_mod

    async def _ok():
        return True

    monkeypatch.setattr(main_mod, "check_qdrant", _ok)
    client = TestClient(app_instance)
    headers = {
        "Authorization": "Bearer wrong-token",
        "X-Forwarded-For": "8.8.8.2",
    }
    for _ in range(11):
        client.get("/api/v1/memories", headers=headers)
    resp = client.get("/healthz", headers={"X-Forwarded-For": "8.8.8.2"})
    assert resp.status_code == 200
