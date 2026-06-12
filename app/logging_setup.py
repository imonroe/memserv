import logging

import structlog

from app.config import get_settings

_REDACT_KEYS = {"authorization", "api_key", "mem0_api_key", "client_secret", "code_verifier"}


def _redact(_logger, _method, event_dict):
    for key in list(event_dict.keys()):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***"
    return event_dict


def _secret_values() -> tuple[str, ...]:
    s = get_settings()
    return tuple(
        v
        for v in (
            s.mem0_api_key,
            s.qdrant_api_key,
            s.anthropic_api_key,
            s.openai_api_key,
            s.oauth_signing_key,
        )
        if v
    )


def _scrub_exception(_logger, _method, event_dict):
    # Key-based redaction can't see inside the formatted traceback string that
    # format_exc_info produces, so scrub it against the known secret *values*
    # in case an exception message echoes one (e.g. a provider error).
    text = event_dict.get("exception")
    if isinstance(text, str):
        for secret in _secret_values():
            text = text.replace(secret, "***")
        event_dict["exception"] = text
    return event_dict


def configure_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _redact,
            structlog.processors.TimeStamper(fmt="iso"),
            # Render exc_info (tuple, exception instance, or True) into a
            # formatted "exception" traceback string — JSONRenderer alone would
            # serialize the raw tuple, losing the stack trace.
            structlog.processors.format_exc_info,
            _scrub_exception,
            structlog.processors.JSONRenderer(),
        ],
    )
