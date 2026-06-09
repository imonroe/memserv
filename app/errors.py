"""Map backend/provider failures to stable, sanitized HTTP error responses.

mem0 calls Qdrant, an LLM provider, and an embedder synchronously inside
request handlers; without this module any of those failing surfaces as an
opaque 500 whose body may leak backend details (hosts, model names, key
prefixes). classify_exception() sorts the concrete SDK exception types into a
small taxonomy with fixed, content-free messages:

- vector store / network failures  -> 503 backend_unavailable
- LLM / embedder provider failures -> 502 upstream_provider_error
- anything else                    -> 500 internal_error

The full exception is always logged server-side (with the request_id) and
never echoed to the client. New mem0 call sites need no wrapping — the
classifier runs from the app-level exception handler in app/main.py.
"""

import anthropic
import httpx
import openai
from qdrant_client.http.exceptions import (
    ResponseHandlingException,
    UnexpectedResponse,
)

# Provider SDK errors are checked first: openai/anthropic connection errors
# wrap httpx exceptions, so the network-level check below would otherwise
# misfile them as vector-store trouble.
_PROVIDER_ERRORS = (openai.OpenAIError, anthropic.AnthropicError)

# Qdrant client failures and raw transport errors. ConnectionError/TimeoutError
# cover the stdlib variants some client paths raise.
_BACKEND_ERRORS = (
    ResponseHandlingException,
    UnexpectedResponse,
    httpx.TransportError,
    httpx.TimeoutException,
    ConnectionError,
    TimeoutError,
)


def classify_exception(exc: BaseException) -> tuple[int, str, str]:
    """Return (status_code, error_code, client-safe detail) for an exception."""
    if isinstance(exc, _PROVIDER_ERRORS):
        return (
            502,
            "upstream_provider_error",
            "An upstream model provider (LLM or embedder) failed; "
            "check provider keys and status.",
        )
    if isinstance(exc, _BACKEND_ERRORS):
        return (
            503,
            "backend_unavailable",
            "The vector store is unreachable or returned an error; try again later.",
        )
    return (500, "internal_error", "Internal server error.")
