import base64
import hashlib
import html
import secrets
import time
from functools import lru_cache

import jwt
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import oauth_store
from app.config import get_settings

router = APIRouter()
_log = structlog.get_logger()

TOKEN_TTL = 24 * 3600
KEY_ID = "mem0-oauth-1"
SCOPES = ["read", "write"]


@lru_cache
def _private_key():
    s = get_settings()
    if not s.oauth_signing_key:
        raise RuntimeError("OAUTH_SIGNING_KEY is not set")
    pem = s.oauth_signing_key.replace("\\n", "\n").encode()
    return serialization.load_pem_private_key(pem, password=None)


def public_key_pem() -> str:
    return (
        _private_key()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def _b64url_uint(val: int) -> str:
    raw = val.to_bytes((val.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwks() -> dict:
    pub: RSAPublicNumbers = _private_key().public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KEY_ID,
                "n": _b64url_uint(pub.n),
                "e": _b64url_uint(pub.e),
            }
        ]
    }


def _verify_pkce(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


def _as_metadata() -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        # Public clients only; PKCE protects the code exchange.
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": SCOPES,
    }


def _protected_resource_metadata() -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {
        # Canonical resource URI per the MCP auth spec: omit the trailing slash.
        # MCP clients (Claude.ai / Cowork) canonicalize to the no-slash form and
        # reject authorization if the advertised resource doesn't match.
        "resource": f"{base}/mcp",
        "authorization_servers": [base],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
    }


# Authorization Server metadata (RFC 8414). The path-scoped variant is what
# MCP clients probe when the MCP endpoint lives under a sub-path (/mcp).
# Trailing-slash variants are served directly (not via 307) because strict
# OAuth clients fetch the exact advertised URL and may not follow redirects.
@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
@router.get("/.well-known/oauth-authorization-server/mcp/")
def metadata() -> dict:
    return _as_metadata()


# Protected Resource metadata (RFC 9728). MCP clients fetch this first to
# discover which authorization server protects the /mcp resource. FastMCP
# advertises this with a trailing slash (.../oauth-protected-resource/mcp/),
# so that exact path must return 200 directly, not a redirect.
@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
@router.get("/.well-known/oauth-protected-resource/mcp/")
def protected_resource() -> dict:
    return _protected_resource_metadata()


@router.get("/.well-known/jwks.json")
def jwks() -> dict:
    return _jwks()


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    allowed = set(get_settings().allowed_redirect_uris_list)
    rejected = [uri for uri in redirect_uris if uri not in allowed]
    if not redirect_uris or rejected:
        # Log the exact requested URIs and the active allowlist so a client whose
        # callback isn't allowed (the common cause of failed Claude.ai / Cowork
        # connections) can be diagnosed and added to OAUTH_ALLOWED_REDIRECT_URIS.
        _log.warning(
            "dcr_redirect_uri_rejected",
            requested=redirect_uris,
            rejected=rejected,
            allowed=sorted(allowed),
        )
        raise HTTPException(status_code=400, detail="redirect_uri not allowed")
    # Register as a public client: PKCE (S256) protects the code exchange, so
    # no client_secret is issued or verified.
    client_id = secrets.token_hex(16)
    oauth_store.save_client(client_id, None, redirect_uris)
    return JSONResponse(
        status_code=201,
        content={
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
    )


def _consent_page(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str,
    error: str = "",
) -> str:
    e_client_id = html.escape(client_id, quote=True)
    e_redirect_uri = html.escape(redirect_uri, quote=True)
    e_code_challenge = html.escape(code_challenge, quote=True)
    e_state = html.escape(state, quote=True)
    e_scope = html.escape(scope, quote=True)
    error_html = f'<p style="color:red">{html.escape(error, quote=True)}</p>' if error else ""
    return f"""
    <html><body>
      <h2>Authorize mem0</h2>
      {error_html}
      <p>Enter your mem0 API key to grant this client access to your memories.</p>
      <form method="post" action="/oauth/authorize">
        <input type="hidden" name="client_id" value="{e_client_id}">
        <input type="hidden" name="redirect_uri" value="{e_redirect_uri}">
        <input type="hidden" name="code_challenge" value="{e_code_challenge}">
        <input type="hidden" name="state" value="{e_state}">
        <input type="hidden" name="scope" value="{e_scope}">
        <label>API key: <input type="password" name="password" autofocus></label>
        <button type="submit">Authorize</button>
      </form>
    </body></html>
    """


def _owner_authenticated(password: str) -> bool:
    # Single-user: the resource owner proves ownership at the consent step with
    # the same MEM0_API_KEY that protects the API. Constant-time compare; an empty
    # configured key must never authenticate.
    key = get_settings().mem0_api_key
    return bool(key) and secrets.compare_digest(password, key)


@router.get("/oauth/authorize")
def authorize_form(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    response_type: str = "code",
    state: str = "",
    scope: str = "read write",
) -> HTMLResponse:
    if response_type != "code":
        raise HTTPException(status_code=400, detail="unsupported response_type")
    if code_challenge_method != "S256" or not code_challenge:
        raise HTTPException(status_code=400, detail="PKCE S256 required")
    client = oauth_store.get_client(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status_code=400, detail="invalid client or redirect_uri")
    return HTMLResponse(
        _consent_page(client_id, redirect_uri, code_challenge, state, scope)
    )


@router.post("/oauth/authorize")
def authorize_submit(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    state: str = Form(""),
    scope: str = Form("read write"),
    password: str = Form(""),
) -> Response:
    client = oauth_store.get_client(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status_code=400, detail="invalid client or redirect_uri")
    # Authenticate the resource owner before issuing a code. Without this, anyone
    # who reaches the consent screen could obtain a token for the single user's
    # memories just by clicking "Authorize".
    if not _owner_authenticated(password):
        _log.warning("oauth_consent_rejected", client_id=client_id)
        return HTMLResponse(
            _consent_page(
                client_id, redirect_uri, code_challenge, state, scope,
                error="Invalid API key.",
            ),
            status_code=401,
        )
    code = secrets.token_urlsafe(32)
    oauth_store.save_code(code, client_id, redirect_uri, code_challenge)
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(url=location, status_code=302)


def _issue_tokens(client_id: str) -> dict:
    s = get_settings()
    now = int(time.time())
    claims = {
        "iss": s.public_base_url.rstrip("/"),
        "sub": "ian",
        "aud": "mem0-server",
        "scope": " ".join(SCOPES),
        "client_id": client_id,
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    access_token = jwt.encode(
        claims, _private_key(), algorithm="RS256", headers={"kid": KEY_ID}
    )
    refresh_token = secrets.token_urlsafe(32)
    oauth_store.save_refresh_token(refresh_token, client_id)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL,
        "refresh_token": refresh_token,
    }


@router.post("/oauth/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    code_verifier: str = Form(None),
    client_id: str = Form(None),
    refresh_token: str = Form(None),
) -> dict:
    if grant_type == "authorization_code":
        if not (code and redirect_uri and code_verifier and client_id):
            raise HTTPException(status_code=400, detail="missing authorization_code params")
        stored = oauth_store.consume_code(code)
        if not stored:
            raise HTTPException(status_code=400, detail="invalid or expired code")
        if stored["client_id"] != client_id or stored["redirect_uri"] != redirect_uri:
            raise HTTPException(status_code=400, detail="code/client mismatch")
        if not _verify_pkce(code_verifier, stored["code_challenge"]):
            raise HTTPException(status_code=400, detail="PKCE verification failed")
        return _issue_tokens(client_id)

    if grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(status_code=400, detail="missing refresh_token")
        # Single-use: consume rotates it, so a new refresh_token is returned.
        rec = oauth_store.consume_refresh_token(refresh_token)
        if not rec:
            raise HTTPException(status_code=400, detail="invalid or expired refresh_token")
        return _issue_tokens(rec["client_id"])

    raise HTTPException(status_code=400, detail="unsupported grant_type")
