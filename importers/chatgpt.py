"""Parse a ChatGPT data export (``conversations.json``) into memory records.

Each conversation becomes one ``messages`` payload, so the server's mem0 layer
runs its normal fact-extraction over the dialogue rather than storing raw turns.
"""

import json
from collections.abc import Iterator

_ROLES = {"user", "assistant"}


def _message_text(message: dict | None) -> tuple[str, str] | None:
    """Return ``(role, text)`` for a renderable user/assistant text message."""
    if not message:
        return None
    role = (message.get("author") or {}).get("role")
    if role not in _ROLES:
        return None
    content = message.get("content") or {}
    if content.get("content_type") != "text":
        return None
    parts = content.get("parts") or []
    text = "\n".join(p for p in parts if isinstance(p, str) and p.strip()).strip()
    if not text:
        return None
    return role, text


def parse_conversations(data, *, source: str = "chatgpt") -> Iterator[dict]:
    """Yield ``MemoryClient.add`` kwargs from a parsed conversations.json structure."""
    conversations = data if isinstance(data, list) else data.get("conversations", [])
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        title = conv.get("title") or "Untitled conversation"
        mapping = conv.get("mapping") or {}
        # Order turns by create_time so the transcript reads in sequence.
        nodes = sorted(
            (n for n in mapping.values() if isinstance(n, dict)),
            key=lambda n: ((n.get("message") or {}).get("create_time") or 0),
        )
        messages = []
        for node in nodes:
            parsed = _message_text(node.get("message"))
            if parsed:
                role, text = parsed
                messages.append({"role": role, "content": text})
        if not messages:
            continue
        yield {
            "messages": messages,
            "agent_id": f"import:{source}",
            "metadata": {"source": source, "title": title},
        }


def load(path: str, *, source: str = "chatgpt") -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    yield from parse_conversations(data, source=source)
