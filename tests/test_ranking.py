from datetime import UTC, datetime

from app.ranking import rerank_by_recency

NOW = datetime(2026, 6, 4, tzinfo=UTC)


def _res(items):
    return {"results": items}


def test_weight_zero_is_noop():
    items = [{"id": "a", "score": 0.1}, {"id": "b", "score": 0.9}]
    out = rerank_by_recency(_res(items), 0.0)
    assert [i["id"] for i in out["results"]] == ["a", "b"]
    assert "rerank_score" not in items[0]


def test_recency_boost_promotes_recent_over_more_similar():
    items = [
        {"id": "old_high_sim", "score": 0.9, "created_at": "2020-01-01T00:00:00Z"},
        {"id": "new_low_sim", "score": 0.5, "created_at": "2026-06-03T00:00:00Z"},
    ]
    out = rerank_by_recency(_res(items), 0.8, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "new_low_sim"
    assert all("rerank_score" in it for it in out["results"])


def test_low_weight_keeps_similarity_order():
    items = [
        {"id": "old_high_sim", "score": 0.9, "created_at": "2020-01-01T00:00:00Z"},
        {"id": "new_low_sim", "score": 0.5, "created_at": "2026-06-03T00:00:00Z"},
    ]
    out = rerank_by_recency(_res(items), 0.1, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "old_high_sim"


def test_item_without_timestamp_is_not_boosted():
    items = [
        {"id": "no_ts", "score": 0.9},
        {"id": "recent", "score": 0.6, "updated_at": "2026-06-03T00:00:00Z"},
    ]
    out = rerank_by_recency(_res(items), 0.7, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "recent"


def test_timestamp_read_from_metadata():
    items = [
        {"id": "a", "score": 0.9, "created_at": "2020-01-01T00:00:00Z"},
        {"id": "b", "score": 0.5, "metadata": {"created_at": "2026-06-03T00:00:00Z"}},
    ]
    out = rerank_by_recency(_res(items), 0.9, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "b"


def test_updated_at_preferred_over_created_at():
    # A memory created long ago but updated yesterday should read as recent.
    items = [
        {"id": "fresh", "score": 0.5, "created_at": "2019-01-01T00:00:00Z",
         "updated_at": "2026-06-03T00:00:00Z"},
        {"id": "stale", "score": 0.9, "created_at": "2026-05-01T00:00:00Z",
         "updated_at": "2020-01-01T00:00:00Z"},
    ]
    out = rerank_by_recency(_res(items), 0.8, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "fresh"


def test_naive_and_zulu_timestamps_parse():
    items = [
        {"id": "naive_old", "score": 0.9, "created_at": "2000-01-01T00:00:00"},
        {"id": "zulu_new", "score": 0.5, "created_at": "2026-06-03T00:00:00Z"},
    ]
    out = rerank_by_recency(_res(items), 0.9, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "zulu_new"


def test_unparseable_timestamp_treated_as_no_timestamp():
    items = [
        {"id": "bad_ts", "score": 0.9, "created_at": "not-a-date"},
        {"id": "recent", "score": 0.6, "created_at": "2026-06-03T00:00:00Z"},
    ]
    out = rerank_by_recency(_res(items), 0.7, half_life_days=30, now=NOW)
    assert out["results"][0]["id"] == "recent"


def test_single_item_is_unchanged():
    items = [{"id": "a", "score": 0.5, "created_at": "2020-01-01T00:00:00Z"}]
    out = rerank_by_recency(_res(items), 0.9, now=NOW)
    assert out["results"] == items
    assert "rerank_score" not in items[0]


def test_non_dict_payloads_pass_through():
    assert rerank_by_recency([], 0.5) == []
    assert rerank_by_recency({"results": "weird"}, 0.5) == {"results": "weird"}
    assert rerank_by_recency({"results": [1, 2]}, 0.5) == {"results": [1, 2]}
