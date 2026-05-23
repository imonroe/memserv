import base64
import hashlib
import secrets
import time
from functools import lru_cache

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import oauth_store
from app.config import get_settings

router = APIRouter()

TOKEN_TTL = 24 * 3600
KEY_ID = "mem0-oauth-1"


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


@router.get("/.well-known/oauth-authorization-server")
def metadata() -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }


@router.get("/.well-known/jwks.json")
def jwks() -> dict:
    return _jwks()


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    allowed = set(get_settings().allowed_redirect_uris_list)
    if not redirect_uris or any(uri not in allowed for uri in redirect_uris):
        raise HTTPException(status_code=400, detail="redirect_uri not allowed")
    client_id = secrets.token_hex(16)
    client_secret = secrets.token_hex(32)
    oauth_store.save_client(client_id, client_secret, redirect_uris)
    return JSONResponse(
        status_code=201,
        content={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
    )


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
    html = f"""
    <html><body>
      <h2>Authorize mem0</h2>
      <form method="post" action="/oauth/authorize">
        <input type="hidden" name="client_id" value="{client_id}">
        <input type="hidden" name="redirect_uri" value="{redirect_uri}">
        <input type="hidden" name="code_challenge" value="{code_challenge}">
        <input type="hidden" name="state" value="{state}">
        <input type="hidden" name="scope" value="{scope}">
        <button type="submit">Authorize</button>
      </form>
    </body></html>
    """
    return HTMLResponse(html)


@router.post("/oauth/authorize")
def authorize_submit(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    state: str = Form(""),
    scope: str = Form("read write"),
) -> RedirectResponse:
    client = oauth_store.get_client(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status_code=400, detail="invalid client or redirect_uri")
    code = secrets.token_urlsafe(32)
    oauth_store.save_code(code, client_id, redirect_uri, code_challenge)
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(url=location, status_code=302)


@router.post("/oauth/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    code_verifier: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(None),
) -> dict:
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported grant_type")
    stored = oauth_store.consume_code(code)
    if not stored:
        raise HTTPException(status_code=400, detail="invalid or expired code")
    if stored["client_id"] != client_id or stored["redirect_uri"] != redirect_uri:
        raise HTTPException(status_code=400, detail="code/client mismatch")
    if not _verify_pkce(code_verifier, stored["code_challenge"]):
        raise HTTPException(status_code=400, detail="PKCE verification failed")

    s = get_settings()
    now = int(time.time())
    claims = {
        "iss": s.public_base_url.rstrip("/"),
        "sub": "ian",
        "aud": "mem0-server",
        "scope": "read write",
        "client_id": client_id,
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    access_token = jwt.encode(
        claims, _private_key(), algorithm="RS256", headers={"kid": KEY_ID}
    )
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": TOKEN_TTL}
