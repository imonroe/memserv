from functools import lru_cache

from app.config import Settings, get_settings


def _build_config(s: Settings) -> dict:
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": s.mem0_collection,
                "host": s.qdrant_host,
                "port": s.qdrant_port,
                "https": s.qdrant_https,
                "api_key": s.qdrant_api_key,
                "embedding_model_dims": s.mem0_embed_dims,
            },
        },
        "llm": {
            "provider": s.mem0_llm_provider,
            "config": {"model": s.mem0_llm_model},
        },
        "embedder": {
            "provider": s.mem0_embed_provider,
            "config": {"model": s.mem0_embed_model},
        },
        "version": "v1.1",
    }


@lru_cache
def get_memory():
    # Imported lazily so tests can mock without a real mem0/Qdrant install.
    from mem0 import Memory

    return Memory.from_config(_build_config(get_settings()))
