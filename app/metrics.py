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


def observe_request(method: str, path: str, status: int, duration_s: float) -> None:
    REQUEST_COUNT.labels(method=method, path=path, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=method, path=path).observe(duration_s)
