import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_load_from_env():
    s = Settings()
    assert s.qdrant_host == "qdrant.test"
    assert s.mem0_default_user_id == "default-user"
    assert s.mem0_api_key == "test-bearer-token"


def test_oauth_disabled_by_default():
    s = Settings()
    assert s.oauth_enabled is False


def test_allowed_redirect_uris_list():
    s = Settings()
    uris = s.allowed_redirect_uris_list
    assert "https://claude.ai/api/mcp/auth_callback" in uris
    assert all(u.strip() == u for u in uris)


def test_missing_anthropic_key_rejected():
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(mem0_llm_provider="anthropic", anthropic_api_key=None)


def test_missing_openai_key_rejected():
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(mem0_embed_provider="openai", openai_api_key=None)


def test_whitespace_only_key_treated_as_missing():
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(mem0_llm_provider="anthropic", anthropic_api_key="   ")


def test_provider_match_is_case_insensitive():
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(mem0_llm_provider="Anthropic", anthropic_api_key=None)


def test_missing_openai_key_rejected_for_llm_provider():
    # OPENAI_API_KEY is required when OpenAI is the LLM provider too, even if the
    # embedder is a keyless local provider like Ollama.
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(
            mem0_llm_provider="openai",
            mem0_embed_provider="ollama",
            openai_api_key=None,
            anthropic_api_key=None,
        )


def test_non_default_provider_skips_key_check():
    # Providers other than the key-backed ones should not require those keys.
    # Set both non-default and clear both keys so the test doesn't depend on
    # env-provided keys unrelated to the behavior under test.
    s = Settings(
        mem0_llm_provider="ollama",
        mem0_embed_provider="ollama",
        anthropic_api_key=None,
        openai_api_key=None,
    )
    assert s.mem0_llm_provider == "ollama"
    assert s.mem0_embed_provider == "ollama"


def test_ollama_base_url_default_and_override():
    assert Settings().ollama_base_url == "http://localhost:11434"
    assert (
        Settings(ollama_base_url="http://ollama:11434").ollama_base_url
        == "http://ollama:11434"
    )
