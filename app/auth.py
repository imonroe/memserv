from fastapi import Header, HTTPException, status
from fastmcp.server.auth import StaticTokenVerifier, TokenVerifier
from mcp.server.auth.provider import AccessToken

from app.config import get_settings


async def require_bearer(authorization: str = Header(default="")) -> None:
    """FastAPI dependency: enforce the static bearer token on REST endpoints."""
    s = get_settings()
    expected = f"Bearer {s.mem0_api_key}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class CompositeVerifier(TokenVerifier):
    """Accepts the static bearer token OR an OAuth-issued RS256 JWT (Phase 2)."""

    def __init__(
        self,
        static_token: str,
        jwt_public_key: str | None = None,
        issuer: str | None = None,
    ):
        super().__init__()
        self.static_token = static_token
        self.jwt_public_key = jwt_public_key
        self.issuer = issuer

    async def verify_token(self, token: str) -> AccessToken | None:
        if token == self.static_token:
            return AccessToken(
                token=token, client_id="ian", scopes=["read", "write"]
            )
        if not self.jwt_public_key:
            return None
        try:
            import jwt

            payload = jwt.decode(
                token,
                self.jwt_public_key,
                algorithms=["RS256"],
                audience="mem0-server",
                issuer=self.issuer,
            )
        except Exception:
            return None
        return AccessToken(
            token=token,
            client_id=payload.get("client_id", payload.get("sub", "unknown")),
            scopes=payload.get("scope", "").split(),
        )


def build_verifier() -> TokenVerifier:
    s = get_settings()
    if s.oauth_enabled:
        from app.oauth import public_key_pem

        return CompositeVerifier(
            static_token=s.mem0_api_key,
            jwt_public_key=public_key_pem(),
            issuer=s.public_base_url,
        )
    return StaticTokenVerifier(
        tokens={s.mem0_api_key: {"client_id": "ian", "scopes": ["read", "write"]}}
    )
