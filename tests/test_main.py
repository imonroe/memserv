from fastapi.testclient import TestClient


def test_mcp_endpoint_served_at_both_slash_variants(app_instance, auth_header):
    # Claude.ai web / Cowork POST to the exact resource URL and do not follow
    # redirects, so both /mcp and /mcp/ must resolve directly (no 307).
    headers = {**auth_header, "Accept": "application/json, text/event-stream"}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    with TestClient(app_instance) as client:
        for path in ("/mcp", "/mcp/"):
            resp = client.post(path, json=body, headers=headers, follow_redirects=False)
            assert resp.status_code == 200, (path, resp.status_code)


def test_oauth_routes_not_mounted_when_disabled(app_instance):
    # The test env sets no OAUTH_SIGNING_KEY, so OAuth is disabled and its
    # routes must not be exposed. A regression here would leak unconfigured
    # OAuth endpoints.
    client = TestClient(app_instance)
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/jwks.json",
        "/oauth/register",
        "/oauth/authorize",
        "/oauth/token",
    ):
        assert client.get(path).status_code == 404, path
