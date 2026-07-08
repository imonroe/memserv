from functools import lru_cache

from pydantic import model_validator
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
    mem0_collection: str = "memories"
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

    # Ollama (local, opt-in). Used when mem0_llm_provider and/or
    # mem0_embed_provider is "ollama"; both talk to the same Ollama server, so a
    # single base URL covers LLM and embedder. No API key: it's a local daemon.
    ollama_base_url: str = "http://localhost:11434"

    # Auth
    mem0_api_key: str
    public_base_url: str

    # OAuth (Phase 2)
    oauth_signing_key: str | None = None
    oauth_allowed_redirect_uris: str = (
        "https://claude.ai/api/mcp/auth_callback,"
        "https://cowork.com/api/mcp/auth_callback,"
        "https://chatgpt.com/connector/oauth/*"
    )

    # Rate limiting (brute-force protection on auth surfaces). Only *failed*
    # attempts count; an IP over the limit is rejected with 429 until the
    # window expires. Limits are per uvicorn worker. Set a *_failures value
    # below 1 to disable limiting on that surface.
    trust_forwarded_for: bool = True
    rate_limit_auth_failures: int = 10
    rate_limit_auth_window_seconds: float = 60.0
    rate_limit_consent_failures: int = 5
    rate_limit_consent_window_seconds: float = 300.0
    rate_limit_token_failures: int = 10
    rate_limit_token_window_seconds: float = 60.0

    # Misc
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _require_provider_keys(self) -> "Settings":
        def _missing(key: str | None) -> bool:
            return not (key and key.strip())

        # A provider's key is required whenever *either* role (LLM or embedder)
        # selects it — e.g. an OpenAI LLM needs OPENAI_API_KEY even if the
        # embedder is Ollama. mem0 reads keys only from the config we build, not
        # os.environ, so a missing key would fail silently at first request.
        providers = {
            self.mem0_llm_provider.strip().lower(),
            self.mem0_embed_provider.strip().lower(),
        }
        if "anthropic" in providers and _missing(self.anthropic_api_key):
            raise ValueError(
                "ANTHROPIC_API_KEY is required when MEM0_LLM_PROVIDER or "
                "MEM0_EMBED_PROVIDER is anthropic"
            )
        if "openai" in providers and _missing(self.openai_api_key):
            raise ValueError(
                "OPENAI_API_KEY is required when MEM0_LLM_PROVIDER or "
                "MEM0_EMBED_PROVIDER is openai"
            )
        return self

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.oauth_signing_key)

    @property
    def allowed_redirect_uris_list(self) -> list[str]:
        return [u.strip() for u in self.oauth_allowed_redirect_uris.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
