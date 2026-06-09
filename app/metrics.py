from prometheus_client import Counter, Histogram

# Labelled by the matched route template (not the raw path) to avoid unbounded
# label cardinality from path parameters like memory_id.
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
)


BULK_DELETED = Counter(
    "memories_bulk_deleted_total",
    "Memories removed via the bulk delete endpoint.",
)

# Brute-force signal: failed auth attempts and rate-limited rejections, by
# auth surface ("rest", "mcp", "oauth_consent", "oauth_token").
AUTH_FAILURES = Counter(
    "auth_failures_total",
    "Failed authentication attempts.",
    ["surface"],
)
RATE_LIMITED = Counter(
    "rate_limited_requests_total",
    "Requests rejected by the auth rate limiter.",
    ["surface"],
)


def observe_request(method: str, path: str, status: int, duration_s: float) -> None:
    REQUEST_COUNT.labels(method=method, path=path, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=method, path=path).observe(duration_s)


def observe_bulk_delete(count: int) -> None:
    BULK_DELETED.inc(count)


def observe_auth_failure(surface: str) -> None:
    AUTH_FAILURES.labels(surface=surface).inc()


def observe_rate_limited(surface: str) -> None:
    RATE_LIMITED.labels(surface=surface).inc()
