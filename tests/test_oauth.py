import base64
import hashlib
import os

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

ALLOWED_URI = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def oauth_client(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    os.environ["OAUTH_SIGNING_KEY"] = pem
    os.environ["OAUTH_DB_PATH"] = str(tmp_path / "oauth.db")

    from app.config import get_settings

    get_settings.cache_clear()

    import app.oauth as oauth_mod

    oauth_mod._private_key.cache_clear()
    from app import oauth_store

    oauth_store.init_db()

    app = FastAPI()
    app.include_router(oauth_mod.router)
    yield TestClient(app)

    del os.environ["OAUTH_SIGNING_KEY"]
    get_settings.cache_clear()
    oauth_mod._private_key.cache_clear()


def _pkce():
    verifier = "verifier-" + "a" * 60
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def test_metadata_and_jwks(oauth_client):
    meta = oauth_client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == "https://mem0.test"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["token_endpoint_auth_methods_supported"] == ["none"]
    jwks = oauth_client.get("/.well-known/jwks.json").json()
    assert jwks["keys"][0]["kty"] == "RSA"


def test_protected_resource_metadata(oauth_client):
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        meta = oauth_client.get(path).json()
        assert meta["resource"] == "https://mem0.test/mcp/"
        assert meta["authorization_servers"] == ["https://mem0.test"]
        assert meta["bearer_methods_supported"] == ["header"]
        assert meta["scopes_supported"] == ["read", "write"]


def test_path_scoped_as_metadata(oauth_client):
    for path in (
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-authorization-server/mcp",
    ):
        meta = oauth_client.get(path).json()
        assert meta["issuer"] == "https://mem0.test"
        assert meta["token_endpoint"] == "https://mem0.test/oauth/token"
        assert meta["scopes_supported"] == ["read", "write"]


def test_expired_codes_are_garbage_collected(oauth_client):
    from app import oauth_store

    # Fresh tmp DB: exactly one expired row, so the helper deletes exactly 1.
    oauth_store.save_code("expired", "c1", ALLOWED_URI, "chal", ttl=-10)
    assert oauth_store.delete_expired_codes() == 1

    # save_code opportunistically purges expired rows: after saving an expired
    # code then a live one, no expired rows remain to delete (distinguishes
    # "purged on save" from "still present but expired").
    oauth_store.save_code("expired2", "c1", ALLOWED_URI, "chal", ttl=-10)
    oauth_store.save_code("live", "c1", ALLOWED_URI, "chal", ttl=300)
    assert oauth_store.delete_expired_codes() == 0
    assert oauth_store.consume_code("live") is not None


def test_dcr_rejects_disallowed_uri(oauth_client):
    resp = oauth_client.post("/oauth/register", json={"redirect_uris": ["https://evil.com/cb"]})
    assert resp.status_code == 400


def test_dcr_registers_public_client(oauth_client):
    resp = oauth_client.post("/oauth/register", json={"redirect_uris": [ALLOWED_URI]})
    assert resp.status_code == 201
    body = resp.json()
    assert "client_secret" not in body
    assert body["token_endpoint_auth_method"] == "none"


def _register(oauth_client):
    resp = oauth_client.post("/oauth/register", json={"redirect_uris": [ALLOWED_URI]})
    assert resp.status_code == 201
    return resp.json()["client_id"]


def test_full_authorize_token_flow(oauth_client):
    client_id = _register(oauth_client)
    verifier, challenge = _pkce()

    resp = oauth_client.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": ALLOWED_URI,
            "code_challenge": challenge,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    code = location.split("code=")[1].split("&")[0]

    resp = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ALLOWED_URI,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert resp.status_code == 200
    access_token = resp.json()["access_token"]

    jwks = oauth_client.get("/.well-known/jwks.json").json()
    pub_pem = _pub_from_jwks(jwks)
    payload = jwt.decode(access_token, pub_pem, algorithms=["RS256"], audience="mem0-server")
    assert payload["sub"] == "ian"
    assert payload["scope"] == "read write"


def test_authorize_form_escapes_state(oauth_client):
    client_id = _register(oauth_client)
    _, challenge = _pkce()
    payload = '"><script>alert(1)</script>'
    resp = oauth_client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": ALLOWED_URI,
            "code_challenge": challenge,
            "state": payload,
        },
    )
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    # Attribute-breaking payloads don't need <script>: ensure the leading quote
    # that would break out of the value="..." attribute is escaped too.
    assert '="><script>' not in resp.text
    assert "&quot;&gt;&lt;script&gt;" in resp.text


def test_pkce_mismatch_rejected(oauth_client):
    client_id = _register(oauth_client)
    _, challenge = _pkce()
    resp = oauth_client.post(
        "/oauth/authorize",
        data={"client_id": client_id, "redirect_uri": ALLOWED_URI, "code_challenge": challenge},
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=")[1].split("&")[0]
    resp = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ALLOWED_URI,
            "code_verifier": "wrong-verifier-value-that-does-not-match",
            "client_id": client_id,
        },
    )
    assert resp.status_code == 400


def test_expired_code_rejected_at_token_endpoint(oauth_client):
    from app import oauth_store

    client_id = _register(oauth_client)
    verifier, challenge = _pkce()
    # Insert a code that's already expired, bypassing the authorize step.
    oauth_store.save_code("expired-code", client_id, ALLOWED_URI, challenge, ttl=-10)
    resp = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": "expired-code",
            "redirect_uri": ALLOWED_URI,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert resp.status_code == 400


def test_code_is_single_use(oauth_client):
    client_id = _register(oauth_client)
    verifier, challenge = _pkce()
    resp = oauth_client.post(
        "/oauth/authorize",
        data={"client_id": client_id, "redirect_uri": ALLOWED_URI, "code_challenge": challenge},
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=")[1].split("&")[0]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ALLOWED_URI,
        "code_verifier": verifier,
        "client_id": client_id,
    }
    assert oauth_client.post("/oauth/token", data=data).status_code == 200
    assert oauth_client.post("/oauth/token", data=data).status_code == 400


def _obtain_tokens(oauth_client) -> dict:
    client_id = _register(oauth_client)
    verifier, challenge = _pkce()
    resp = oauth_client.post(
        "/oauth/authorize",
        data={"client_id": client_id, "redirect_uri": ALLOWED_URI, "code_challenge": challenge},
        follow_redirects=False,
    )
    code = resp.headers["location"].split("code=")[1].split("&")[0]
    resp = oauth_client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": ALLOWED_URI,
            "code_verifier": verifier,
            "client_id": client_id,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_metadata_advertises_refresh_grant(oauth_client):
    meta = oauth_client.get("/.well-known/oauth-authorization-server").json()
    assert "refresh_token" in meta["grant_types_supported"]


def test_authorization_code_returns_refresh_token(oauth_client):
    tokens = _obtain_tokens(oauth_client)
    assert tokens["refresh_token"]
    assert tokens["token_type"] == "Bearer"


def test_refresh_token_flow_rotates(oauth_client):
    tokens = _obtain_tokens(oauth_client)
    old_refresh = tokens["refresh_token"]

    resp = oauth_client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert resp.status_code == 200
    new_tokens = resp.json()
    assert new_tokens["access_token"]
    # Rotation: a new refresh token is issued and the old one is invalidated.
    assert new_tokens["refresh_token"] != old_refresh
    reused = oauth_client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": old_refresh},
    )
    assert reused.status_code == 400


def test_refresh_token_invalid_rejected(oauth_client):
    resp = oauth_client.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": "nope"},
    )
    assert resp.status_code == 400


def test_refresh_token_expiry_and_cleanup(oauth_client):
    from app import oauth_store

    # Expired token is rejected on consume.
    oauth_store.save_refresh_token("expired-rt", "c1", ttl=-10)
    assert oauth_store.consume_refresh_token("expired-rt") is None

    # Opportunistic cleanup: saving purges expired rows (exactly one here).
    oauth_store.save_refresh_token("expired-rt2", "c1", ttl=-10)
    assert oauth_store.delete_expired_refresh_tokens() == 1

    # A live token round-trips and is not stored in plaintext.
    oauth_store.save_refresh_token("live-rt", "c1", ttl=300)
    assert oauth_store.consume_refresh_token("live-rt") == {"client_id": "c1"}


def test_refresh_token_not_stored_in_plaintext(oauth_client):
    import sqlite3

    from app import oauth_store

    oauth_store.save_refresh_token("secret-rt", "c1", ttl=300)
    conn = sqlite3.connect(oauth_store.DB_PATH)
    try:
        tokens = [r[0] for r in conn.execute("SELECT token FROM refresh_tokens")]
    finally:
        conn.close()
    assert "secret-rt" not in tokens
    assert all(len(t) == 64 for t in tokens)  # sha256 hex digests


def test_mcp_401_advertises_resource_metadata(tmp_path, monkeypatch):
    """An unauthenticated MCP request must return a 401 whose WWW-Authenticate
    header points at the protected resource metadata (RFC 9728). Without this,
    OAuth MCP clients (Claude.ai web / Cowork) can't discover the auth server."""
    import re

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    # monkeypatch restores/removes these on teardown even if already set in the shell.
    monkeypatch.setenv("OAUTH_SIGNING_KEY", pem)
    monkeypatch.setenv("OAUTH_DB_PATH", str(tmp_path / "oauth.db"))

    from app.config import get_settings

    get_settings.cache_clear()
    import app.oauth as oauth_mod

    oauth_mod._private_key.cache_clear()

    try:
        from contextlib import asynccontextmanager

        from app.mcp_server import build_mcp

        mcp_app = build_mcp().http_app(
            path="/", stateless_http=True, transport="streamable-http"
        )

        @asynccontextmanager
        async def lifespan(app):
            async with mcp_app.lifespan(app):
                yield

        app = FastAPI(lifespan=lifespan)
        app.mount("/mcp", mcp_app)

        with TestClient(app) as client:
            resp = client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert resp.status_code == 401
        www_auth = resp.headers.get("www-authenticate", "")
        match = re.search(r'resource_metadata="([^"]+)"', www_auth)
        assert match, f"no resource_metadata in WWW-Authenticate: {www_auth!r}"
        # Must be the /mcp-scoped protected-resource metadata document, not just
        # any URL containing "/mcp" — a wrong path here still breaks discovery.
        assert match.group(1).rstrip("/").endswith(
            "/.well-known/oauth-protected-resource/mcp"
        ), match.group(1)
    finally:
        # monkeypatch handles env restoration; lru_caches must be cleared manually
        # so other tests don't observe OAuth-enabled settings.
        get_settings.cache_clear()
        oauth_mod._private_key.cache_clear()


def _pub_from_jwks(jwks: dict) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    k = jwks["keys"][0]

    def _int(b64):
        return int.from_bytes(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)), "big")

    pub = RSAPublicNumbers(_int(k["e"]), _int(k["n"])).public_key()
    return pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
