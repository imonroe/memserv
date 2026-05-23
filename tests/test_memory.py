from app.config import Settings
from app.memory import _build_config


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
