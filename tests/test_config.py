from app.config import Settings


def test_settings_load_from_env():
    s = Settings()
    assert s.qdrant_host == "qdrant.test"
    assert s.mem0_default_user_id == "ian"
    assert s.mem0_api_key == "test-bearer-token"


def test_oauth_disabled_by_default():
    s = Settings()
    assert s.oauth_enabled is False


def test_allowed_redirect_uris_list():
    s = Settings()
    uris = s.allowed_redirect_uris_list
    assert "https://claude.ai/api/mcp/auth_callback" in uris
    assert all(u.strip() == u for u in uris)
