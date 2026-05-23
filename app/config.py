from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Qdrant
    qdrant_host: str
    qdrant_port: int = 443
    qdrant_https: bool = True
    qdrant_api_key: str

    # mem0 core
    mem0_collection: str = "ian_memories"
    mem0_default_user_id: str

    # LLM (fact extraction)
    mem0_llm_provider: str = "anthropic"
    mem0_llm_model: str = "claude-haiku-4-5-20251001"
    anthropic_api_key: str | None = None

    # Embeddings
    mem0_embed_provider: str = "openai"
    mem0_embed_model: str = "text-embedding-3-small"
    mem0_embed_dims: int = 1536
    openai_api_key: str | None = None

    # Auth
    mem0_api_key: str
    public_base_url: str

    # OAuth (Phase 2)
    oauth_signing_key: str | None = None
    oauth_allowed_redirect_uris: str = (
        "https://claude.ai/api/mcp/auth_callback,"
        "https://cowork.com/api/mcp/auth_callback"
    )

    # Misc
    log_level: str = "INFO"

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.oauth_signing_key)

    @property
    def allowed_redirect_uris_list(self) -> list[str]:
        return [u.strip() for u in self.oauth_allowed_redirect_uris.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
