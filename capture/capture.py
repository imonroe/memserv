#!/usr/bin/env python3
"""Telegram capture bot: save messages you send it into the memory server.

A long-running worker (no inbound webhook needed — it long-polls the Telegram
Bot API). Any text you send the bot is stored via POST /api/v1/memories, tagged
with a capture provenance agent_id. Because the memory store is single-user and
high-trust, the bot only accepts messages from an allowlist of Telegram chat IDs.

Configured entirely via environment variables:

  MEM0_URL                   (required) base URL of the memory server
  MEM0_API_KEY               (required) bearer token
  TELEGRAM_BOT_TOKEN         (required) token from @BotFather
  TELEGRAM_ALLOWED_CHAT_IDS  comma-separated chat IDs allowed to save. If unset,
                             the bot runs in discovery mode: it replies with your
                             chat ID and stores nothing (so you can authorize it).
  CAPTURE_AGENT_ID           provenance tag (default "capture:telegram")
  TELEGRAM_POLL_TIMEOUT      long-poll seconds per getUpdates call (default 30)

Other chat platforms (Slack slash commands, Discord bots) can be added the same
way: parse the inbound message, then call post_memory().
"""

import os
import sys
import time
from collections import namedtuple

import httpx

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_AGENT_ID = "capture:telegram"
NOTE_COMMANDS = {"note", "save", "remember", "capture"}
HELP_COMMANDS = {"start", "help"}
HELP_TEXT = (
    "Send me any text and I'll save it to your memory. "
    "You can also use /note <text>. I only accept messages from authorized chats."
)

Config = namedtuple("Config", "base_url api_key token allowed_ids agent_id")


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and (val is None or str(val).strip() == ""):
        sys.exit(f"error: {name} is required")
    return val


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"error: {name} must be an integer (got {raw!r})")
    if value < minimum:
        sys.exit(f"error: {name} must be >= {minimum} (got {value})")
    return value


def parse_allowed_chat_ids(raw: str | None) -> set[int]:
    ids: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            print(f"warning: ignoring non-integer chat id {part!r}", file=sys.stderr)
    return ids


def extract_message(update: dict) -> tuple[int, int | None, str] | None:
    """Return (chat_id, user_id, text) for a text message update, else None."""
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return None
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text")
    if not isinstance(chat_id, int) or not isinstance(text, str) or not text.strip():
        return None
    user_id = (msg.get("from") or {}).get("id")
    return chat_id, user_id, text.strip()


def classify(text: str) -> tuple[str, str]:
    """Map message text to (kind, body) where kind is help|note|empty."""
    if not text.startswith("/"):
        return "note", text
    first, _, rest = text.partition(" ")
    command = first[1:].split("@", 1)[0].lower()  # tolerate /note@MyBot
    if command in HELP_COMMANDS:
        return "help", ""
    if command in NOTE_COMMANDS:
        body = rest.strip()
        return ("note", body) if body else ("empty", "")
    return "help", ""  # unknown command -> show help


def get_updates(token: str, offset: int | None, poll_timeout: int, *, client: httpx.Client) -> list:
    params = {"timeout": poll_timeout}
    if offset is not None:
        params["offset"] = offset
    resp = client.get(
        f"{TELEGRAM_API}/bot{token}/getUpdates",
        params=params,
        timeout=poll_timeout + 10,
    )
    resp.raise_for_status()
    data = resp.json()
    # Telegram signals errors as HTTP 200 with {"ok": false, ...}; surface those
    # (and any non-dict body) as errors so the run loop logs and backs off rather
    # than silently looking idle on, say, an invalid token.
    if not isinstance(data, dict) or not data.get("ok"):
        detail = data.get("description") if isinstance(data, dict) else data
        raise RuntimeError(f"Telegram getUpdates failed: {detail!r}")
    result = data.get("result")
    return result if isinstance(result, list) else []


def send_message(token: str, chat_id: int, text: str, *, client: httpx.Client) -> None:
    resp = client.post(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
    )
    resp.raise_for_status()


def post_memory(
    base_url: str, api_key: str, text: str, agent_id: str, *, client: httpx.Client
) -> dict:
    resp = client.post(
        f"{base_url.rstrip('/')}/api/v1/memories",
        json={"content": text, "agent_id": agent_id, "metadata": {"source": agent_id}},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    resp.raise_for_status()
    return resp.json()


def process_update(update: dict, cfg: Config, *, client: httpx.Client) -> None:
    parsed = extract_message(update)
    if parsed is None:
        return
    chat_id, _user_id, text = parsed
    kind, body = classify(text)

    if kind == "help":
        send_message(cfg.token, chat_id, HELP_TEXT, client=client)
        return

    # Discovery mode: no allowlist configured yet. Never store; just help the
    # operator find their chat ID so they can authorize themselves.
    if not cfg.allowed_ids:
        send_message(
            cfg.token,
            chat_id,
            f"This capture bot isn't configured yet. Your chat id is {chat_id}. "
            "Add it to TELEGRAM_ALLOWED_CHAT_IDS to enable saving.",
            client=client,
        )
        return

    if chat_id not in cfg.allowed_ids:
        send_message(
            cfg.token, chat_id, "Sorry, you're not authorized to use this bot.", client=client
        )
        return

    if kind == "empty":
        send_message(
            cfg.token, chat_id, "Send some text to save, e.g. /note buy milk", client=client
        )
        return

    try:
        post_memory(cfg.base_url, cfg.api_key, body, cfg.agent_id, client=client)
    except Exception as exc:  # noqa: BLE001 - report failure to the user, keep the bot alive
        # Full detail to logs; a stable, non-revealing message to the user.
        print(f"error saving memory: {exc}", file=sys.stderr)
        send_message(cfg.token, chat_id, "⚠️ Couldn't save that — please try again.", client=client)
        return

    send_message(cfg.token, chat_id, "Saved ✓", client=client)


def load_config() -> Config:
    return Config(
        base_url=_env("MEM0_URL", required=True),
        api_key=_env("MEM0_API_KEY", required=True),
        token=_env("TELEGRAM_BOT_TOKEN", required=True),
        allowed_ids=parse_allowed_chat_ids(_env("TELEGRAM_ALLOWED_CHAT_IDS")),
        agent_id=_env("CAPTURE_AGENT_ID", DEFAULT_AGENT_ID),
    )


def run() -> None:
    cfg = load_config()
    poll_timeout = _int_env("TELEGRAM_POLL_TIMEOUT", 30)
    if not cfg.allowed_ids:
        print(
            "warning: TELEGRAM_ALLOWED_CHAT_IDS is unset — running in discovery mode "
            "(nothing will be saved). Message the bot to learn your chat id.",
            file=sys.stderr,
        )
    offset: int | None = None
    print("capture bot started; polling Telegram for messages.")
    with httpx.Client() as client:
        while True:
            try:
                updates = get_updates(cfg.token, offset, poll_timeout, client=client)
            except Exception as exc:  # noqa: BLE001 - transient network/API error; back off and retry
                print(f"error polling Telegram: {exc}", file=sys.stderr)
                time.sleep(5)
                continue
            for update in updates:
                if isinstance(update.get("update_id"), int):
                    offset = update["update_id"] + 1
                try:
                    process_update(update, cfg, client=client)
                except Exception as exc:  # noqa: BLE001 - never let one bad update kill the loop
                    print(f"error processing update: {exc}", file=sys.stderr)


if __name__ == "__main__":
    run()
