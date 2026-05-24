import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.auth import CompositeVerifier, require_bearer

ISSUER = "https://mem0.test"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv_pem, pub_pem


@pytest.fixture(scope="module")
def keypair():
    """Module-scoped keypair so RSA generation isn't repeated per test."""
    return _keypair()


@pytest.fixture(scope="module")
def other_keypair():
    """A second, distinct keypair for the wrong-signature case."""
    return _keypair()


def _make_jwt(priv_pem, *, aud="mem0-server", iss=ISSUER, exp_delta=3600, **extra):
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": "default-user",
        "aud": aud,
        "scope": "read write",
        "client_id": "claude-web",
        "iat": now,
        "exp": now + exp_delta,
        **extra,
    }
    return jwt.encode(claims, priv_pem, algorithm="RS256")


async def test_require_bearer_accepts_valid():
    await require_bearer(authorization="Bearer test-bearer-token")


async def test_require_bearer_rejects_invalid():
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="Bearer wrong")
    assert exc.value.status_code == 401


async def test_require_bearer_rejects_missing_prefix():
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="test-bearer-token")
    assert exc.value.status_code == 401


async def test_require_bearer_rejects_empty_configured_key(monkeypatch):
    import app.auth as auth_mod

    class _S:
        mem0_api_key = ""

    monkeypatch.setattr(auth_mod, "get_settings", lambda: _S())
    # No Authorization header + empty configured key must NOT authenticate.
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="Bearer ")
    assert exc.value.status_code == 500


async def test_composite_accepts_static_token():
    v = CompositeVerifier(static_token="abc")
    token = await v.verify_token("abc")
    assert token is not None
    assert token.client_id == "default-user"
    assert "write" in token.scopes


async def test_composite_rejects_unknown_without_jwt_key():
    v = CompositeVerifier(static_token="abc")
    assert await v.verify_token("nope") is None


async def test_composite_accepts_valid_jwt(keypair):
    priv, pub = keypair
    v = CompositeVerifier(static_token="abc", jwt_public_key=pub, issuer=ISSUER)
    token = await v.verify_token(_make_jwt(priv))
    assert token is not None
    assert token.client_id == "claude-web"
    assert token.scopes == ["read", "write"]


async def test_composite_rejects_expired_jwt(keypair):
    priv, pub = keypair
    v = CompositeVerifier(static_token="abc", jwt_public_key=pub, issuer=ISSUER)
    assert await v.verify_token(_make_jwt(priv, exp_delta=-10)) is None


async def test_composite_rejects_wrong_audience(keypair):
    priv, pub = keypair
    v = CompositeVerifier(static_token="abc", jwt_public_key=pub, issuer=ISSUER)
    assert await v.verify_token(_make_jwt(priv, aud="someone-else")) is None


async def test_composite_rejects_wrong_issuer(keypair):
    priv, pub = keypair
    v = CompositeVerifier(static_token="abc", jwt_public_key=pub, issuer=ISSUER)
    assert await v.verify_token(_make_jwt(priv, iss="https://evil.test")) is None


async def test_composite_rejects_wrong_signature(keypair, other_keypair):
    priv_a, _ = keypair
    _, pub_b = other_keypair
    v = CompositeVerifier(static_token="abc", jwt_public_key=pub_b, issuer=ISSUER)
    # Signed with key A but verified against key B's public key.
    assert await v.verify_token(_make_jwt(priv_a)) is None


async def test_composite_rejects_malformed_jwt(keypair):
    _, pub = keypair
    v = CompositeVerifier(static_token="abc", jwt_public_key=pub, issuer=ISSUER)
    assert await v.verify_token("not.a.jwt") is None
