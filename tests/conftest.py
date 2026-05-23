import os
from unittest.mock import MagicMock

import pytest

os.environ.update(
    {
        "QDRANT_HOST": "qdrant.test",
        "QDRANT_PORT": "443",
        "QDRANT_HTTPS": "true",
        "QDRANT_API_KEY": "test-qdrant-key",
        "MEM0_COLLECTION": "test_memories",
        "MEM0_DEFAULT_USER_ID": "ian",
        "ANTHROPIC_API_KEY": "test-anthropic",
        "OPENAI_API_KEY": "test-openai",
        "MEM0_API_KEY": "test-bearer-token",
        "PUBLIC_BASE_URL": "https://mem0.test",
        "LOG_LEVEL": "INFO",
    }
)

# Shared fake mem0 Memory instance. app.rest and app.mcp_server both resolve
# the store through `app.memory.get_memory()` at call time, so patching that one
# attribute is the single, refactor-proof seam. Done before app.main is imported
# (build_mcp calls get_memory() at import time).
FAKE_MEMORY = MagicMock(name="Memory")

import app.memory as memory_mod  # noqa: E402

memory_mod.get_memory.cache_clear()
memory_mod.get_memory = lambda: FAKE_MEMORY


@pytest.fixture
def mem():
    FAKE_MEMORY.reset_mock()
    return FAKE_MEMORY


@pytest.fixture
def app_instance():
    import app.main as main_mod

    return main_mod.app


@pytest.fixture
def auth_header():
    return {"Authorization": "Bearer test-bearer-token"}
