from app.config import Settings
from app.memory import _build_config


def test_build_config_shape():
    cfg = _build_config(Settings())

    assert cfg["vector_store"]["provider"] == "qdrant"
    vs = cfg["vector_store"]["config"]
    assert vs["collection_name"] == "test_memories"
    assert vs["host"] == "qdrant.test"
    assert vs["embedding_model_dims"] == 1536
    assert vs["api_key"] == "test-qdrant-key"

    assert cfg["llm"]["provider"] == "anthropic"
    assert cfg["embedder"]["provider"] == "openai"
    assert cfg["version"] == "v1.1"
