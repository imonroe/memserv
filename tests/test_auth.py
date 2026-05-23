import pytest
from fastapi import HTTPException

from app.auth import CompositeVerifier, require_bearer


async def test_require_bearer_accepts_valid():
    await require_bearer(authorization="Bearer test-bearer-token")


async def test_require_bearer_rejects_invalid():
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="Bearer wrong")
    assert exc.value.status_code == 401


async def test_composite_accepts_static_token():
    v = CompositeVerifier(static_token="abc")
    token = await v.verify_token("abc")
    assert token is not None
    assert token.client_id == "ian"
    assert "write" in token.scopes


async def test_composite_rejects_unknown_without_jwt_key():
    v = CompositeVerifier(static_token="abc")
    assert await v.verify_token("nope") is None
