from fastapi.testclient import TestClient


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
