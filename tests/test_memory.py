from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.memory import (
    _build_config,
    _existing_fingerprint_id,
    add_memory,
    content_fingerprint,
)


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
