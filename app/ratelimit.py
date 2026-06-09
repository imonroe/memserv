"""Per-IP rate limiting of failed authentication attempts.

Every surface that accepts a secret is an online-guessing oracle for
MEM0_API_KEY (or an OAuth code/refresh token): the REST bearer check, the MCP
token verifier, the OAuth consent form, and the OAuth token endpoint. This
module slows brute force to a crawl with a small in-process fixed-window
counter keyed by client IP. Only *failures* count toward the limit; an IP that
hits the limit is locked out of that surface (even with valid credentials)
until the window expires.

Limits are per uvicorn worker (the Dockerfile runs --workers 2), so the
effective limit is roughly workers x the configured value. That is fine for a
single-user service: the point is to turn millions of guesses per day into
dozens, not to enforce an exact quota.
"""

import math
import threading
import time
from functools import lru_cache

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.metrics import observe_auth_failure, observe_rate_limited

_log = structlog.get_logger()

# Cap on tracked IPs per limiter; when reached, expired windows are pruned on
# the next recorded failure so an attacker rotating IPs can't grow the dict
# without bound.
_MAX_TRACKED_KEYS = 4096


class RateLimiter:
    """Fixed-window failure counter keyed by an arbitrary string (client IP).

    A key with `max_failures` failures inside `window_seconds` is limited until
    the window that contains its first failure expires. `max_failures < 1`
    disables limiting entirely (operator opt-out).
    """

    def __init__(self, max_failures: int, window_seconds: float):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        # key -> (window_start_monotonic, failure_count)
        self._windows: dict[str, tuple[float, int]] = {}

    def retry_after(self, key: str, now: float | None = None) -> float | None:
        """Seconds until `key` may retry, or None if it is not limited."""
        if self.max_failures < 1:
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._windows.get(key)
            if entry is None:
                return None
            start, count = entry
            if now - start >= self.window_seconds:
                del self._windows[key]
                return None
            if count >= self.max_failures:
                return self.window_seconds - (now - start)
            return None

    def record_failure(self, key: str, now: float | None = None) -> None:
        if self.max_failures < 1:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._windows) >= _MAX_TRACKED_KEYS:
                self._prune(now)
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self.window_seconds:
                start, count = now, 0
            self._windows[key] = (start, count + 1)

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, (start, _) in self._windows.items()
            if now - start >= self.window_seconds
        ]
        for key in expired:
            del self._windows[key]

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


def client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit keying.

    Behind CapRover's nginx the peer address is the proxy, so the original
    client is in X-Forwarded-For (first hop). TRUST_FORWARDED_FOR=false turns
    that off for deployments exposed directly (e.g. docker-compose without a
    proxy), where the header would be attacker-controlled.
    """
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


# Response statuses that count as an authentication failure, per surface.
# REST/MCP only ever 401 on a bad bearer token; the consent form returns 401 on
# a wrong API key; the token endpoint returns 400 for guessed/expired codes and
# refresh tokens (per RFC 6749), so 400 counts there too.
_FAILURE_STATUSES = {
    "rest": {401},
    "mcp": {401},
    "oauth_consent": {401},
    "oauth_token": {400, 401},
}


def _surface(path: str, method: str) -> str | None:
    p = path.rstrip("/") or "/"
    if p == "/api/v1" or p.startswith("/api/v1/"):
        return "rest"
    if p == "/mcp":
        return "mcp"
    if p == "/oauth/authorize" and method == "POST":
        # The GET form render checks no secret; only the POST submit does.
        return "oauth_consent"
    if p == "/oauth/token":
        return "oauth_token"
    return None


@lru_cache
def _limiter(surface: str) -> RateLimiter:
    s = get_settings()
    if surface == "oauth_consent":
        return RateLimiter(
            s.rate_limit_consent_failures, s.rate_limit_consent_window_seconds
        )
    if surface == "oauth_token":
        return RateLimiter(
            s.rate_limit_token_failures, s.rate_limit_token_window_seconds
        )
    return RateLimiter(s.rate_limit_auth_failures, s.rate_limit_auth_window_seconds)


def reset_all() -> None:
    """Drop all limiter state (and re-read settings). Test hook."""
    _limiter.cache_clear()


async def rate_limit_middleware(request: Request, call_next):
    surface = _surface(request.url.path, request.method)
    if surface is None:
        return await call_next(request)
    ip = client_ip(request)
    limiter = _limiter(surface)
    retry_after = limiter.retry_after(ip)
    if retry_after is not None:
        observe_rate_limited(surface)
        _log.warning("rate_limited", surface=surface, ip=ip)
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Too many failed authentication attempts; try again later."
            },
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )
    response = await call_next(request)
    if response.status_code in _FAILURE_STATUSES[surface]:
        limiter.record_failure(ip)
        observe_auth_failure(surface)
        _log.warning(
            "auth_failure", surface=surface, ip=ip, status=response.status_code
        )
    return response
