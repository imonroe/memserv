from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.memory import (
    _build_config,
    _existing_fingerprint_id,
    add_memory,
    content_fingerprint,
    drop_expired,
    keyword_search,
    list_paginated,
)

EXPIRY_NOW = datetime(2026, 6, 7, tzinfo=UTC)


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
    # Indexed prefilter finds nothing, so these tests exercise the scan path's
    # substring/ordering semantics; the indexed path has its own tests below.
    fake.vector_store.client.scroll.return_value = ([], None)
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


def test_keyword_search_passes_extra_filters(monkeypatch):
    fake = _patch_keyword(monkeypatch, [_point("1", "Philips hub")])
    keyword_search("philips", user_id="ian", extra_filters={"review_status": "approved"})
    _, kwargs = fake.vector_store.list.call_args
    assert kwargs["filters"] == {"user_id": "ian", "review_status": "approved"}


# --- drop_expired ------------------------------------------------------------


def test_drop_expired_removes_past_keeps_future_and_missing():
    results = {
        "results": [
            {"id": "past", "metadata": {"expires_at": "2020-01-01T00:00:00+00:00"}},
            {"id": "future", "metadata": {"expires_at": "2030-01-01T00:00:00Z"}},
            {"id": "no_expiry", "metadata": {}},
            {"id": "toplevel_past", "expires_at": "2019-01-01T00:00:00+00:00"},
        ]
    }
    out = drop_expired(results, now=EXPIRY_NOW)
    assert [i["id"] for i in out["results"]] == ["future", "no_expiry"]


def test_drop_expired_passes_through_non_results():
    assert drop_expired([], now=EXPIRY_NOW) == []
    assert drop_expired({"results": "x"}, now=EXPIRY_NOW) == {"results": "x"}


def test_keyword_search_fails_open(monkeypatch):
    import app.memory as m

    fake = MagicMock()
    fake.vector_store.list.side_effect = RuntimeError("qdrant down")
    monkeypatch.setattr(m, "get_memory", lambda: fake)
    assert keyword_search("x", user_id="ian") == {"results": []}


# --- keyword_search indexed path ----------------------------------------------


def _patch_indexed(monkeypatch, indexed_points, scanned_points=()):
    import app.memory as m

    fake = MagicMock()
    fake.vector_store.collection_name = "test_memories"
    fake.vector_store.client.scroll.return_value = (list(indexed_points), None)
    fake.vector_store.list.return_value = (list(scanned_points), None)
    monkeypatch.setattr(m, "get_memory", lambda: fake)
    return fake


# --- bulk_delete ---------------------------------------------------------------


def _patch_bulk(monkeypatch, points):
    import app.memory as m

    fake = MagicMock()
    fake.vector_store.list.return_value = (points, None)
    monkeypatch.setattr(m, "get_memory", lambda: fake)
    return fake


def test_bulk_delete_caps_and_reports_has_more(monkeypatch):
    from app.memory import bulk_delete

    points = [_point(f"m{i}", f"fact {i}") for i in range(5)]
    fake = _patch_bulk(monkeypatch, points)
    out = bulk_delete(filters={"agent_id": "a"}, confirm=True, max_delete=3)
    assert out["matched"] == 3
    assert out["deleted"] == 3
    assert out["has_more"] is True
    assert fake.delete.call_count == 3
    # One extra point is fetched purely as the has_more signal.
    _, kwargs = fake.vector_store.list.call_args
    assert kwargs["top_k"] == 4


def test_bulk_delete_sample_capped_at_ten(monkeypatch):
    from app.memory import bulk_delete

    points = [_point(f"m{i}", f"fact {i}") for i in range(15)]
    _patch_bulk(monkeypatch, points)
    out = bulk_delete(filters={"agent_id": "a"})
    assert out["matched"] == 15
    assert len(out["sample"]) == 10
    assert out["dry_run"] is True


def test_bulk_delete_goes_through_mem0_not_vector_store(monkeypatch):
    from app.memory import bulk_delete

    fake = _patch_bulk(monkeypatch, [_point("m1", "x")])
    bulk_delete(filters={"agent_id": "a"}, confirm=True)
    fake.delete.assert_called_once_with(memory_id="m1")
    fake.vector_store.delete.assert_not_called()


def test_bulk_delete_partial_failure_reports_progress(monkeypatch):
    from app.memory import bulk_delete

    fake = _patch_bulk(monkeypatch, [_point(f"m{i}", "x") for i in range(3)])
    fake.delete.side_effect = [None, RuntimeError("qdrant hiccup"), None]
    out = bulk_delete(filters={"agent_id": "a"}, confirm=True)
    assert out["deleted"] == 1
    assert out["error"] == "delete_failed_partway"
    assert out["has_more"] is True  # remainder not attempted; caller re-posts
    assert fake.delete.call_count == 2  # stopped at the failure


def test_keyword_search_uses_index_and_skips_full_scan(monkeypatch):
    from qdrant_client import models

    fake = _patch_indexed(
        monkeypatch, [_point("1", "Ian uses Philips Hue lights", user_id="ian")]
    )
    out = keyword_search("philips", user_id="ian")
    assert [r["id"] for r in out["results"]] == ["1"]
    fake.vector_store.list.assert_not_called()  # no full-store transfer

    # The index is created once, on the data field, as a full-text index.
    fake.vector_store.client.create_payload_index.assert_called_once()
    _, idx_kwargs = fake.vector_store.client.create_payload_index.call_args
    assert idx_kwargs["field_name"] == "data"
    assert idx_kwargs["field_schema"].tokenizer == models.TokenizerType.WORD

    # The scroll filter combines exact-match scoping with the MatchText clause.
    _, kwargs = fake.vector_store.client.scroll.call_args
    conditions = kwargs["scroll_filter"].must
    matches = {c.key: c.match for c in conditions}
    assert matches["user_id"] == models.MatchValue(value="ian")
    assert matches["data"] == models.MatchText(text="philips")


def test_keyword_index_created_once_across_searches(monkeypatch):
    fake = _patch_indexed(monkeypatch, [_point("1", "alpha", user_id="ian")])
    keyword_search("alpha", user_id="ian")
    keyword_search("alpha", user_id="ian")
    assert fake.vector_store.client.create_payload_index.call_count == 1


def test_keyword_indexed_results_still_substring_verified(monkeypatch):
    # MatchText is token-based: both tokens present but not contiguous must not
    # match the substring query.
    fake = _patch_indexed(
        monkeypatch, [_point("1", "oat in my milk", user_id="ian")]
    )
    out = keyword_search("oat milk", user_id="ian")
    assert out["results"] == []
    fake.vector_store.list.assert_called_once()  # fell back to the scan


def test_keyword_falls_back_to_scan_for_mid_token_fragment(monkeypatch):
    # "hil" never token-matches, but the scan still finds it in "Philips".
    fake = _patch_indexed(
        monkeypatch,
        indexed_points=[],
        scanned_points=[_point("1", "Ian uses Philips Hue lights", user_id="ian")],
    )
    out = keyword_search("hil", user_id="ian")
    assert [r["id"] for r in out["results"]] == ["1"]
    fake.vector_store.list.assert_called_once()


def test_keyword_falls_back_when_index_creation_fails(monkeypatch):
    fake = _patch_indexed(
        monkeypatch,
        indexed_points=[],
        scanned_points=[_point("1", "alpha", user_id="ian")],
    )
    fake.vector_store.client.create_payload_index.side_effect = RuntimeError("old qdrant")
    out = keyword_search("alpha", user_id="ian")
    assert [r["id"] for r in out["results"]] == ["1"]
    fake.vector_store.client.scroll.assert_not_called()
    # The failed creation is cached: the next search doesn't retry it.
    keyword_search("alpha", user_id="ian")
    assert fake.vector_store.client.create_payload_index.call_count == 1


def test_keyword_falls_back_when_indexed_query_raises(monkeypatch):
    fake = _patch_indexed(
        monkeypatch,
        indexed_points=[],
        scanned_points=[_point("1", "alpha", user_id="ian")],
    )
    fake.vector_store.client.scroll.side_effect = RuntimeError("scroll broke")
    out = keyword_search("alpha", user_id="ian")
    assert [r["id"] for r in out["results"]] == ["1"]


def test_keyword_empty_query_touches_nothing(monkeypatch):
    fake = _patch_indexed(monkeypatch, [])
    assert keyword_search("   ", user_id="ian") == {"results": []}
    fake.vector_store.client.create_payload_index.assert_not_called()
    fake.vector_store.list.assert_not_called()


# --- list_paginated -------------------------------------------------------------


def test_list_paginated_shapes_and_slices(mem):
    mem.get_all.return_value = {
        "results": [{"id": f"m{i}"} for i in range(7)]
    }
    out = list_paginated(filters={"user_id": "u"}, limit=3, offset=3)
    assert [i["id"] for i in out["results"]] == ["m3", "m4", "m5"]
    assert out["pagination"] == {"limit": 3, "offset": 3, "has_more": True}
    _, kwargs = mem.get_all.call_args
    assert kwargs == {"filters": {"user_id": "u"}, "top_k": 7}


def test_list_paginated_tolerates_bare_list_return(mem):
    # Some mem0 versions/stores return a bare list instead of {"results": [...]}.
    mem.get_all.return_value = [{"id": "a"}, {"id": "b"}]
    out = list_paginated(filters={"user_id": "u"}, limit=10, offset=0)
    assert [i["id"] for i in out["results"]] == ["a", "b"]
    assert out["pagination"]["has_more"] is False


def test_list_paginated_tolerates_none_results(mem):
    mem.get_all.return_value = {"results": None}
    out = list_paginated(filters={"user_id": "u"}, limit=10, offset=0)
    assert out["results"] == []
    assert out["pagination"]["has_more"] is False
