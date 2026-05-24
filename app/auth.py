import secrets

from fastapi import Header, HTTPException, status
from fastmcp.server.auth import (
    AuthProvider,
    RemoteAuthProvider,
    StaticTokenVerifier,
    TokenVerifier,
)
from mcp.server.auth.provider import AccessToken

from app.config import get_settings

_BEARER_PREFIX = "Bearer "


async def require_bearer(authorization: str = Header(default="")) -> None:
    """FastAPI dependency: enforce the static bearer token on REST endpoints."""
    s = get_settings()
    if not s.mem0_api_key:
        # An empty configured key would otherwise authenticate empty tokens.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server auth misconfigured: MEM0_API_KEY is empty",
        )
    if not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len(_BEARER_PREFIX) :]
    # Constant-time compare to avoid leaking the token via response timing.
    if not secrets.compare_digest(token, s.mem0_api_key):
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
        static_client_id: str = "default-user",
    ):
        super().__init__()
        self.static_token = static_token
        self.jwt_public_key = jwt_public_key
        self.issuer = issuer
        self.static_client_id = static_client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.static_token and secrets.compare_digest(token, self.static_token):
            return AccessToken(
                token=token, client_id=self.static_client_id, scopes=["read", "write"]
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


def build_verifier() -> AuthProvider:
    s = get_settings()
    if s.oauth_enabled:
        from app.oauth import SCOPES, public_key_pem

        base = s.public_base_url.rstrip("/")
        verifier = CompositeVerifier(
            static_token=s.mem0_api_key,
            jwt_public_key=public_key_pem(),
            issuer=base,
            static_client_id=s.mem0_default_user_id,
        )
        # Wrap the verifier so the mounted MCP app advertises the protected
        # resource metadata URL in the 401 WWW-Authenticate header (RFC 9728).
        # Without this, OAuth MCP clients (Claude.ai web / Cowork) can't discover
        # the authorization server and fail with "Couldn't reach the MCP server".
        # resource_base_url must be set explicitly: FastMCP is mounted under /mcp
        # by the outer FastAPI app and doesn't know that prefix on its own, so the
        # advertised resource would otherwise be the bare base URL.
        return RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[base],
            base_url=base,
            resource_base_url=f"{base}/mcp",
            scopes_supported=SCOPES,
        )
    return StaticTokenVerifier(
        tokens={
            s.mem0_api_key: {
                "client_id": s.mem0_default_user_id,
                "scopes": ["read", "write"],
            }
        }
    )
