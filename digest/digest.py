#!/usr/bin/env python3
"""Memory digest: summarize recently-added memories and post them to a webhook.

Runs as a standalone script (invoked by cron in the digest container). It pulls
recent memories from the memory server's REST API, optionally summarizes them
with Claude, and delivers the result to a Slack or Discord incoming webhook.

Everything is configured via environment variables:

  MEM0_URL                (required) base URL of the memory server
  MEM0_API_KEY            (required) bearer token
  DIGEST_WINDOW_DAYS      look-back window in days (default 1)
  DIGEST_MAX_MEMORIES     cap on memories fetched/considered (default 200)
  DIGEST_WEBHOOK_URL      Slack or Discord incoming webhook; if unset, prints
  DIGEST_WEBHOOK_FORMAT   "slack" or "discord" (auto-detected from the URL)
  DIGEST_TITLE            heading for the message (default "🧠 Memory digest")
  DIGEST_SEND_WHEN_EMPTY  "true" to send even when nothing is new (default false)
  ANTHROPIC_API_KEY       if set, summarize with Claude; otherwise plain list
  MEM0_LLM_MODEL          Claude model for summarization (default haiku)
"""

import os
import sys
from datetime import UTC, datetime, timedelta

import httpx

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and (val is None or str(val).strip() == ""):
        sys.exit(f"error: {name} is required")
    return val


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def memory_text(memory: object) -> str:
    if isinstance(memory, dict):
        for key in ("memory", "content", "text", "data"):
            val = memory.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def memory_timestamp(memory: object) -> datetime | None:
    if isinstance(memory, dict):
        for key in ("updated_at", "created_at"):
            ts = parse_timestamp(memory.get(key))
            if ts is not None:
                return ts
    return None


def filter_recent(memories: list, window_days: float, now: datetime | None = None) -> list:
    """Return memories created/updated within the look-back window.

    A memory with no parseable timestamp is kept (we can't prove it's old); the
    overall set is already capped by DIGEST_MAX_MEMORIES at fetch time.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    out = []
    for m in memories:
        ts = memory_timestamp(m)
        if ts is None or ts >= cutoff:
            out.append(m)
    return out


def _memory_lines(memories: list) -> list[str]:
    return [t for t in (memory_text(m) for m in memories) if t]


def fallback_digest(memories: list) -> str:
    return "\n".join(f"• {line}" for line in _memory_lines(memories))


def summarize_prompt(memories: list, window_days: float) -> str:
    bullets = "\n".join(f"- {line}" for line in _memory_lines(memories))
    span = "day" if window_days == 1 else f"{window_days:g} days"
    return (
        f"The following facts were saved to my personal memory in the last {span}. "
        "Write a short, friendly digest for me. Group related items and call out any "
        "decisions, preferences, tasks, or follow-ups. Use brief bullet points and keep "
        "it under ~200 words. Plain text only, no markdown headers.\n\n"
        f"Memories:\n{bullets}"
    )


def extract_claude_text(data: dict) -> str:
    blocks = data.get("content") or []
    texts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t).strip()


def detect_format(url: str | None, override: str | None = None) -> str:
    if override and override.strip():
        return override.strip().lower()
    if url and "discord" in url:
        return "discord"
    return "slack"


def format_payload(text: str, fmt: str) -> dict:
    # Slack incoming webhooks read "text"; Discord webhooks read "content".
    return {"content": text} if fmt == "discord" else {"text": text}


def fetch_memories(base_url: str, api_key: str, limit: int, *, client=None, timeout=30.0) -> list:
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.get(
            f"{base_url.rstrip('/')}/api/v1/memories",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", []) if isinstance(data, dict) else data
        return results if isinstance(results, list) else []
    finally:
        if owns:
            client.close()


def summarize_with_claude(
    memories: list, *, api_key: str, model: str, window_days: float, client=None, timeout=60.0
) -> str:
    payload = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": summarize_prompt(memories, window_days)}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(ANTHROPIC_URL, json=payload, headers=headers)
        resp.raise_for_status()
        return extract_claude_text(resp.json())
    finally:
        if owns:
            client.close()


def deliver(webhook_url: str, payload: dict, *, client=None, timeout=30.0) -> None:
    owns = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.post(webhook_url, json=payload)
        resp.raise_for_status()
    finally:
        if owns:
            client.close()


def build_digest(
    memories: list, *, window_days: float, anthropic_key: str | None, model: str
) -> str:
    if anthropic_key:
        try:
            text = summarize_with_claude(
                memories, api_key=anthropic_key, model=model, window_days=window_days
            )
            if text:
                return text
        except Exception as exc:  # noqa: BLE001 - fall back to a plain list on any LLM error
            print(f"warning: summarization failed ({exc}); using plain list", file=sys.stderr)
    return fallback_digest(memories)


def main() -> int:
    base_url = _env("MEM0_URL", required=True)
    api_key = _env("MEM0_API_KEY", required=True)
    window_days = float(_env("DIGEST_WINDOW_DAYS", "1"))
    max_memories = int(_env("DIGEST_MAX_MEMORIES", "200"))
    webhook_url = _env("DIGEST_WEBHOOK_URL")
    fmt = detect_format(webhook_url, _env("DIGEST_WEBHOOK_FORMAT"))
    title = _env("DIGEST_TITLE", "🧠 Memory digest")
    anthropic_key = _env("ANTHROPIC_API_KEY")
    model = _env("MEM0_LLM_MODEL", DEFAULT_MODEL)
    send_when_empty = str(_env("DIGEST_SEND_WHEN_EMPTY", "false")).lower() == "true"

    fetched = fetch_memories(base_url, api_key, max_memories)
    recent = filter_recent(fetched, window_days)
    print(
        f"Found {len(recent)} memories in the last {window_days:g} day(s) "
        f"(of {len(fetched)} fetched)."
    )

    if not recent and not send_when_empty:
        print("Nothing new; skipping digest.")
        return 0

    body = build_digest(
        recent, window_days=window_days, anthropic_key=anthropic_key, model=model
    ) or "No memories were captured in this period."
    message = f"{title}\n\n{body}"

    if not webhook_url:
        print("No DIGEST_WEBHOOK_URL set; printing digest instead:\n")
        print(message)
        return 0

    deliver(webhook_url, format_payload(message, fmt))
    print(f"Digest delivered to {fmt} webhook.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
