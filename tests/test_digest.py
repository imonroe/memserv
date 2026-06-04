import json
from datetime import UTC, datetime, timedelta

import httpx
import respx

from digest import digest as d

NOW = datetime(2026, 6, 4, tzinfo=UTC)


# --- pure helpers ------------------------------------------------------------


def test_parse_timestamp_handles_zulu_naive_and_bad():
    assert d.parse_timestamp("2026-06-03T00:00:00Z").tzinfo is not None
    assert d.parse_timestamp("2026-06-03T00:00:00").tzinfo is not None  # assumed UTC
    assert d.parse_timestamp("not-a-date") is None
    assert d.parse_timestamp(None) is None


def test_memory_text_prefers_memory_key_then_falls_back():
    assert d.memory_text({"memory": "a", "content": "b"}) == "a"
    assert d.memory_text({"content": "b"}) == "b"
    assert d.memory_text({"text": "c"}) == "c"
    assert d.memory_text({"nope": "x"}) == ""


def test_filter_recent_includes_recent_excludes_old_keeps_missing():
    mems = [
        {"memory": "recent", "created_at": "2026-06-03T00:00:00Z"},
        {"memory": "old", "created_at": "2020-01-01T00:00:00Z"},
        {"memory": "no-ts"},
    ]
    out = d.filter_recent(mems, window_days=2, now=NOW)
    texts = {m["memory"] for m in out}
    assert texts == {"recent", "no-ts"}


def test_filter_recent_uses_updated_at_when_present():
    mems = [
        {"memory": "touched", "created_at": "2019-01-01T00:00:00Z",
         "updated_at": "2026-06-03T12:00:00Z"},
    ]
    assert len(d.filter_recent(mems, window_days=2, now=NOW)) == 1


def test_fallback_digest_bullets_only_nonempty():
    text = d.fallback_digest([{"memory": "one"}, {"nope": "x"}, {"memory": "two"}])
    assert text == "• one\n• two"


def test_summarize_prompt_contains_bullets_and_span():
    p1 = d.summarize_prompt([{"memory": "x"}], window_days=1)
    assert "- x" in p1 and "last day" in p1
    p7 = d.summarize_prompt([{"memory": "x"}], window_days=7)
    assert "last 7 days" in p7


def test_extract_claude_text_joins_text_blocks():
    data = {"content": [{"type": "text", "text": "hello"},
                        {"type": "thinking", "text": "ignore"},
                        {"type": "text", "text": "world"}]}
    assert d.extract_claude_text(data) == "hello\nworld"


def test_detect_format():
    assert d.detect_format("https://hooks.slack.com/x") == "slack"
    assert d.detect_format("https://discord.com/api/webhooks/1/2") == "discord"
    assert d.detect_format("https://discord.com/...", override="slack") == "slack"
    assert d.detect_format(None) == "slack"


def test_format_payload_slack_vs_discord():
    assert d.format_payload("hi", "slack") == {"text": "hi"}
    assert d.format_payload("hi", "discord") == {"content": "hi"}


# --- network helpers (respx) -------------------------------------------------


@respx.mock
def test_fetch_memories_sends_bearer_and_unwraps_results():
    route = respx.get("https://mem0.test/api/v1/memories").mock(
        return_value=httpx.Response(200, json={"results": [{"memory": "a"}]})
    )
    out = d.fetch_memories("https://mem0.test/", "tok", 50)
    assert out == [{"memory": "a"}]
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    assert req.url.params["limit"] == "50"


@respx.mock
def test_deliver_posts_payload():
    route = respx.post("https://hooks.slack.com/x").mock(return_value=httpx.Response(200))
    d.deliver("https://hooks.slack.com/x", {"text": "hi"})
    assert route.called
    assert json.loads(route.calls.last.request.content) == {"text": "hi"}


@respx.mock
def test_summarize_with_claude_sends_headers_and_parses():
    route = respx.post(d.ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "summary"}]})
    )
    out = d.summarize_with_claude(
        [{"memory": "x"}], api_key="sk", model="claude-haiku-4-5-20251001", window_days=1
    )
    assert out == "summary"
    req = route.calls.last.request
    assert req.headers["x-api-key"] == "sk"
    assert req.headers["anthropic-version"] == d.ANTHROPIC_VERSION


@respx.mock
def test_build_digest_uses_claude_when_key_present():
    respx.post(d.ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "AI digest"}]})
    )
    out = d.build_digest([{"memory": "x"}], window_days=1, anthropic_key="sk", model="m")
    assert out == "AI digest"


def test_build_digest_falls_back_without_key():
    out = d.build_digest([{"memory": "x"}, {"memory": "y"}], window_days=1,
                         anthropic_key=None, model="m")
    assert out == "• x\n• y"


@respx.mock
def test_build_digest_falls_back_on_claude_error():
    respx.post(d.ANTHROPIC_URL).mock(return_value=httpx.Response(500))
    out = d.build_digest([{"memory": "x"}], window_days=1, anthropic_key="sk", model="m")
    assert out == "• x"


# --- main wiring -------------------------------------------------------------


def _set_main_env(monkeypatch, **extra):
    monkeypatch.setenv("MEM0_URL", "https://mem0.test")
    monkeypatch.setenv("MEM0_API_KEY", "tok")
    monkeypatch.setenv("DIGEST_WEBHOOK_URL", "https://hooks.slack.com/x")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # force plain-list path
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


@respx.mock
def test_main_delivers_digest(monkeypatch):
    _set_main_env(monkeypatch)
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    respx.get("https://mem0.test/api/v1/memories").mock(
        return_value=httpx.Response(
            200, json={"results": [{"memory": "fact", "created_at": recent}]}
        )
    )
    webhook = respx.post("https://hooks.slack.com/x").mock(return_value=httpx.Response(200))
    assert d.main() == 0
    assert webhook.called
    assert b"fact" in webhook.calls.last.request.content


@respx.mock
def test_main_skips_when_nothing_recent(monkeypatch):
    _set_main_env(monkeypatch, DIGEST_WINDOW_DAYS="1")
    respx.get("https://mem0.test/api/v1/memories").mock(
        return_value=httpx.Response(
            200, json={"results": [{"memory": "old", "created_at": "2000-01-01T00:00:00Z"}]}
        )
    )
    webhook = respx.post("https://hooks.slack.com/x").mock(return_value=httpx.Response(200))
    assert d.main() == 0
    assert not webhook.called
