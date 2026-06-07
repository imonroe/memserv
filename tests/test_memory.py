from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.memory import (
    _build_config,
    _existing_fingerprint_id,
    add_memory,
    content_fingerprint,
    keyword_search,
)


def _point(id, data, created_at="2026-06-01T00:00:00+00:00", **extra):
    return SimpleNamespace(id=id, payload={"data": data, "created_at": created_at, **extra})


def test_build_config_shape():
    cfg = _build_config(Settings())

    assert cfg["vector_store"]["provider"] == "qdrant"
    vs = cfg["vector_store"]["config"]
    assert vs["collection_name"] == "test_memories"
    assert vs["url"] == "https://qdrant.test:443"
    assert "https" not in vs  # mem0 v2 Qdrant has no https flag; scheme is in url
    assert vs["embedding_model_dims"] == 1536
    assert vs["api_key"] == "test-qdrant-key"

    assert cfg["llm"]["provider"] == "anthropic"
    assert cfg["llm"]["config"]["api_key"] == "test-anthropic"
    assert cfg["embedder"]["provider"] == "openai"
    assert cfg["embedder"]["config"]["api_key"] == "test-openai"
    assert cfg["version"] == "v1.1"


def test_build_config_accepted_by_mem0_schema():
    # Non-mocked check: validate the config against the real (pinned) mem0
    # MemoryConfig so method/config drift is caught in CI rather than at runtime.
    from mem0.configs.base import MemoryConfig

    MemoryConfig(**_build_config(Settings()))


# --- content fingerprint -----------------------------------------------------


def test_content_fingerprint_normalizes_whitespace_and_case():
    assert content_fingerprint("Hello  World") == content_fingerprint("  hello world  ")
    assert content_fingerprint("a\nb") == content_fingerprint("a b")


def test_content_fingerprint_differs_for_different_content():
    assert content_fingerprint("apples") != content_fingerprint("oranges")


def test_content_fingerprint_handles_message_lists():
    base = [{"role": "user", "content": "Hello   World"}]
    # Case + whitespace (incl. newlines) inside message text are normalized.
    equivalent = [{"role": "user", "content": "hello world"}]
    newlined = [{"role": "user", "content": "hello\nworld"}]
    assert content_fingerprint(base) == content_fingerprint(equivalent)
    assert content_fingerprint(base) == content_fingerprint(newlined)
    assert len(content_fingerprint(base)) == 64
    # A different role or different text fingerprints differently.
    assert content_fingerprint(base) != content_fingerprint(
        [{"role": "assistant", "content": "hello world"}]
    )
    assert content_fingerprint(base) != content_fingerprint(
        [{"role": "user", "content": "goodbye world"}]
    )


# --- _existing_fingerprint_id ------------------------------------------------


def test_existing_fingerprint_id_found():
    mem = MagicMock()
    mem.vector_store.list.return_value = ([SimpleNamespace(id="m-1")], None)
    assert _existing_fingerprint_id(mem, "fp", "ian") == "m-1"
    _, kwargs = mem.vector_store.list.call_args
    assert kwargs["filters"] == {"content_fp": "fp", "user_id": "ian"}


def test_existing_fingerprint_id_none_when_empty():
    mem = MagicMock()
    mem.vector_store.list.return_value = ([], None)
    assert _existing_fingerprint_id(mem, "fp", "ian") is None


def test_existing_fingerprint_id_fails_open_on_error():
    mem = MagicMock()
    mem.vector_store.list.side_effect = RuntimeError("qdrant down")
    # A dedup-check failure must never block a write.
    assert _existing_fingerprint_id(mem, "fp", "ian") is None


# --- add_memory wrapper ------------------------------------------------------


def _patch_memory(monkeypatch, *, existing):
    """Patch app.memory.get_memory to a fake whose dedup lookup returns `existing`."""
    import app.memory as m

    fake = MagicMock()
    points = [SimpleNamespace(id=existing)] if existing else []
    fake.vector_store.list.return_value = (points, None)
    fake.add.return_value = {"results": [{"id": "new"}]}
    monkeypatch.setattr(m, "get_memory", lambda: fake)
    return fake


def test_add_memory_stores_fingerprint_when_new(monkeypatch):
    fake = _patch_memory(monkeypatch, existing=None)
    out = add_memory("remember this", user_id="ian", agent_id="cli")
    assert out == {"results": [{"id": "new"}]}
    args, kwargs = fake.add.call_args
    assert args[0] == "remember this"
    assert kwargs["user_id"] == "ian"
    assert kwargs["agent_id"] == "cli"
    assert "content_fp" in kwargs["metadata"]


def test_add_memory_skips_when_duplicate(monkeypatch):
    fake = _patch_memory(monkeypatch, existing="dup-1")
    out = add_memory("remember this", user_id="ian")
    assert out == {"results": [], "deduplicated": True, "memory_id": "dup-1"}
    fake.add.assert_not_called()  # no LLM extraction for an exact repeat


def test_add_memory_dedup_false_skips_check(monkeypatch):
    fake = _patch_memory(monkeypatch, existing="dup-1")
    add_memory("remember this", dedup=False, user_id="ian")
    fake.vector_store.list.assert_not_called()  # no dedup lookup at all
    fake.add.assert_called_once()
    _, kwargs = fake.add.call_args
    assert "content_fp" not in (kwargs.get("metadata") or {})  # no fingerprint added


def test_add_memory_merges_existing_metadata(monkeypatch):
    fake = _patch_memory(monkeypatch, existing=None)
    add_memory("x", user_id="ian", metadata={"source": "import"})
    _, kwargs = fake.add.call_args
    assert kwargs["metadata"]["source"] == "import"
    assert "content_fp" in kwargs["metadata"]


# --- keyword_search ----------------------------------------------------------


def _patch_keyword(monkeypatch, points):
    import app.memory as m

    fake = MagicMock()
    fake.vector_store.list.return_value = (points, None)
    monkeypatch.setattr(m, "get_memory", lambda: fake)
    return fake


def test_keyword_search_matches_case_insensitive_substring(monkeypatch):
    fake = _patch_keyword(
        monkeypatch,
        [
            _point("1", "Ian uses Philips Hue lights", user_id="ian"),
            _point("2", "Prefers oat milk in coffee"),
        ],
    )
    out = keyword_search("philips", user_id="ian")
    assert [r["id"] for r in out["results"]] == ["1"]
    assert out["results"][0]["memory"] == "Ian uses Philips Hue lights"
    _, kwargs = fake.vector_store.list.call_args
    assert kwargs["filters"] == {"user_id": "ian"}  # scoped to the user


def test_keyword_search_sorts_recent_first_and_limits(monkeypatch):
    _patch_keyword(
        monkeypatch,
        [
            _point("old", "alpha one", created_at="2020-01-01T00:00:00+00:00"),
            _point("new", "alpha two", created_at="2026-06-01T00:00:00+00:00"),
            _point("mid", "alpha three", created_at="2023-01-01T00:00:00+00:00"),
        ],
    )
    out = keyword_search("alpha", user_id="ian", limit=2)
    assert [r["id"] for r in out["results"]] == ["new", "mid"]  # newest first, capped at 2


def test_keyword_search_prefers_updated_at_for_ordering(monkeypatch):
    # "old" was created later but "new" was updated more recently → "new" first.
    _patch_keyword(
        monkeypatch,
        [
            _point("old", "alpha one", created_at="2026-06-05T00:00:00+00:00"),
            _point(
                "new",
                "alpha two",
                created_at="2020-01-01T00:00:00+00:00",
                updated_at="2026-06-06T00:00:00Z",  # note: Zulu form, different tz repr
            ),
        ],
    )
    out = keyword_search("alpha", user_id="ian")
    assert [r["id"] for r in out["results"]] == ["new", "old"]


def test_keyword_search_drops_internal_fields_keeps_metadata(monkeypatch):
    _patch_keyword(
        monkeypatch,
        [
            _point(
                "1",
                "match me",
                agent_id="cli",
                text_lemmatized="match me",
                content_fp="deadbeef",
            )
        ],
    )
    result = keyword_search("match", user_id="ian")["results"][0]
    assert result["memory"] == "match me"
    assert result["agent_id"] == "cli"
    assert result["created_at"]
    # Internal plumbing must not leak into results.
    assert "data" not in result
    assert "text_lemmatized" not in result
    assert "content_fp" not in result


def test_keyword_search_no_match_returns_empty(monkeypatch):
    _patch_keyword(monkeypatch, [_point("1", "nothing relevant")])
    assert keyword_search("zzz", user_id="ian") == {"results": []}


def test_keyword_search_empty_query_matches_nothing(monkeypatch):
    fake = _patch_keyword(monkeypatch, [_point("1", "anything")])
    assert keyword_search("   ", user_id="ian") == {"results": []}
    fake.vector_store.list.assert_not_called()  # short-circuits before scanning


def test_keyword_search_fails_open(monkeypatch):
    import app.memory as m

    fake = MagicMock()
    fake.vector_store.list.side_effect = RuntimeError("qdrant down")
    monkeypatch.setattr(m, "get_memory", lambda: fake)
    assert keyword_search("x", user_id="ian") == {"results": []}
