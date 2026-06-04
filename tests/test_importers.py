import httpx
import pytest
import respx

from importers import chatgpt, obsidian, readwise
from importers.client import MemoryClient

# --- ChatGPT -----------------------------------------------------------------


def _chatgpt_export():
    return [
        {
            "title": "Deploy notes",
            "mapping": {
                "n2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "create_time": 2,
                        "content": {"content_type": "text", "parts": ["Use CapRover."]},
                    }
                },
                "n1": {
                    "message": {
                        "author": {"role": "user"},
                        "create_time": 1,
                        "content": {"content_type": "text", "parts": ["How do we deploy?"]},
                    }
                },
                "root": {"message": None},
                "sys": {
                    "message": {
                        "author": {"role": "system"},
                        "create_time": 0,
                        "content": {"content_type": "text", "parts": ["You are helpful."]},
                    }
                },
            },
        },
        {"title": "Empty", "mapping": {}},
    ]


def test_chatgpt_parses_ordered_user_assistant_turns():
    records = list(chatgpt.parse_conversations(_chatgpt_export()))
    # The empty conversation yields nothing.
    assert len(records) == 1
    rec = records[0]
    # System turns dropped; user/assistant ordered by create_time.
    assert rec["messages"] == [
        {"role": "user", "content": "How do we deploy?"},
        {"role": "assistant", "content": "Use CapRover."},
    ]
    assert rec["agent_id"] == "import:chatgpt"
    assert rec["metadata"] == {"source": "chatgpt", "title": "Deploy notes"}


def test_chatgpt_accepts_wrapped_conversations_key():
    records = list(chatgpt.parse_conversations({"conversations": _chatgpt_export()}))
    assert len(records) == 1


def test_chatgpt_custom_source_tag():
    records = list(chatgpt.parse_conversations(_chatgpt_export(), source="gpt-archive"))
    assert records[0]["agent_id"] == "import:gpt-archive"
    assert records[0]["metadata"]["source"] == "gpt-archive"


# --- Obsidian ----------------------------------------------------------------


def test_obsidian_strips_frontmatter_and_skips_dotdirs(tmp_path):
    (tmp_path / "note.md").write_text(
        "---\ntags: [x]\n---\nThe actual body.\n", encoding="utf-8"
    )
    (tmp_path / "empty.md").write_text("---\nonly: frontmatter\n---\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not markdown", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.md").write_text("Nested note.", encoding="utf-8")
    skip = tmp_path / ".obsidian"
    skip.mkdir()
    (skip / "config.md").write_text("should be ignored", encoding="utf-8")

    records = list(obsidian.parse_vault(str(tmp_path)))
    titles = {r["metadata"]["title"] for r in records}

    assert titles == {"note", "deep"}  # empty note, .txt, and .obsidian/* excluded
    note = next(r for r in records if r["metadata"]["title"] == "note")
    assert note["content"] == "The actual body."
    assert note["agent_id"] == "import:obsidian"
    assert note["metadata"]["path"] == "note.md"


def test_obsidian_strip_frontmatter_only_at_top():
    text = "Body first.\n---\nnot-frontmatter\n---\n"
    assert obsidian.strip_frontmatter(text) == text


# --- Readwise ----------------------------------------------------------------


def test_readwise_parses_highlight_with_note_and_metadata():
    rows = [
        {
            "Highlight": "Memory should be shared.",
            "Note": "key idea",
            "Book Title": "On Memory",
            "Book Author": "A. Author",
        },
        {"Highlight": "", "Book Title": "Skipped"},  # no text → skipped
    ]
    records = list(readwise.parse_highlights(rows))
    assert len(records) == 1
    rec = records[0]
    assert rec["content"] == "Memory should be shared.\n\nNote: key idea"
    assert rec["metadata"] == {
        "source": "readwise",
        "book": "On Memory",
        "author": "A. Author",
    }
    assert rec["agent_id"] == "import:readwise"


# --- MemoryClient ------------------------------------------------------------


def test_client_dry_run_does_not_call_network():
    client = MemoryClient("https://mem0.test", "k", dry_run=True)
    out = client.add(content="hi", agent_id="import:chatgpt", metadata={"source": "chatgpt"})
    assert out["dry_run"] is True
    assert out["payload"] == {
        "content": "hi",
        "agent_id": "import:chatgpt",
        "metadata": {"source": "chatgpt"},
    }


def test_client_requires_content_or_messages():
    client = MemoryClient("https://mem0.test", "k", dry_run=True)
    with pytest.raises(ValueError):
        client.add(metadata={"source": "x"})


@respx.mock
def test_client_posts_with_bearer():
    route = respx.post("https://mem0.test/api/v1/memories").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "1"}]})
    )
    client = MemoryClient("https://mem0.test/", "secret-token")
    out = client.add(content="hello")
    assert out == {"results": [{"id": "1"}]}
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret-token"


@respx.mock
def test_client_does_not_retry_4xx():
    route = respx.post("https://mem0.test/api/v1/memories").mock(
        return_value=httpx.Response(422, json={"detail": "bad"})
    )
    client = MemoryClient("https://mem0.test", "k", max_retries=4, sleep=lambda _s: None)
    with pytest.raises(httpx.HTTPStatusError):
        client.add(content="x")
    assert route.call_count == 1  # 4xx is not retried


@respx.mock
def test_client_retries_5xx_then_succeeds():
    route = respx.post("https://mem0.test/api/v1/memories").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    slept = []
    client = MemoryClient("https://mem0.test", "k", max_retries=4, sleep=slept.append)
    out = client.add(content="x")
    assert out == {"ok": True}
    assert route.call_count == 2
    assert slept == [2.0]  # backed off once before the retry
