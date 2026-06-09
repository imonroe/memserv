import respx
from fastapi.testclient import TestClient
from httpx import Response


def _client(app_instance):
    return TestClient(app_instance)


def test_requires_bearer(app_instance):
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={"content": "x"})
    assert resp.status_code == 401


def test_add_memory(app_instance, mem, auth_header):
    mem.add.return_value = {"results": [{"id": "1", "memory": "hi"}]}
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={"content": "hi"}, headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == {"results": [{"id": "1", "memory": "hi"}]}
    mem.add.assert_called_once()
    args, kwargs = mem.add.call_args
    assert args[0] == "hi"
    assert kwargs["user_id"] == "default-user"


def test_add_memory_requires_content_or_messages(app_instance, mem, auth_header):
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={}, headers=auth_header)
    assert resp.status_code == 422


def test_add_memory_stores_fingerprint(app_instance, mem, auth_header):
    mem.add.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={"content": "hi"}, headers=auth_header)
    assert resp.status_code == 200
    _, kwargs = mem.add.call_args
    assert "content_fp" in kwargs["metadata"]  # fingerprint stored for dedup


def test_add_memory_deduplicates_exact_repeat(app_instance, mem, auth_header):
    from types import SimpleNamespace

    mem.vector_store.list.return_value = ([SimpleNamespace(id="dup-1")], None)
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={"content": "hi"}, headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == {"results": [], "deduplicated": True, "memory_id": "dup-1"}
    mem.add.assert_not_called()  # no LLM extraction on a duplicate


def test_add_memory_dedup_false_bypasses_check(app_instance, mem, auth_header):
    from types import SimpleNamespace

    mem.vector_store.list.return_value = ([SimpleNamespace(id="dup-1")], None)
    mem.add.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={"content": "hi", "dedup": False}, headers=auth_header)
    assert resp.status_code == 200
    mem.add.assert_called_once()
    mem.vector_store.list.assert_not_called()


def test_search(app_instance, mem, auth_header):
    mem.search.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search", json={"query": "where", "limit": 5}, headers=auth_header
    )
    assert resp.status_code == 200
    _, kwargs = mem.search.call_args
    assert kwargs["top_k"] == 5
    assert kwargs["filters"]["user_id"] == "default-user"


def test_list(app_instance, mem, auth_header):
    mem.get_all.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.get("/api/v1/memories?agent_id=n8n", headers=auth_header)
    assert resp.status_code == 200
    _, kwargs = mem.get_all.call_args
    assert kwargs["filters"]["agent_id"] == "n8n"
    assert kwargs["filters"]["user_id"] == "default-user"
    assert kwargs["top_k"] == 50  # default list limit must reach mem0 as top_k


def test_search_default_does_not_rerank(app_instance, mem, auth_header):
    mem.search.return_value = {
        "results": [
            {"id": "a", "score": 0.9, "created_at": "2000-01-01T00:00:00Z"},
            {"id": "b", "score": 0.5, "created_at": "2026-06-03T00:00:00Z"},
        ]
    }
    c = _client(app_instance)
    resp = c.post("/api/v1/memories/search", json={"query": "x"}, headers=auth_header)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["id"] for r in results] == ["a", "b"]
    assert "rerank_score" not in results[0]


def test_search_recency_weight_reranks(app_instance, mem, auth_header):
    mem.search.return_value = {
        "results": [
            {"id": "old", "score": 0.9, "created_at": "2000-01-01T00:00:00Z"},
            {"id": "new", "score": 0.5, "created_at": "2026-06-03T00:00:00Z"},
        ]
    }
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search",
        json={"query": "x", "recency_weight": 0.9},
        headers=auth_header,
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["id"] == "new"


def test_search_recency_weight_out_of_range_rejected(app_instance, mem, auth_header):
    c = _client(app_instance)
    assert (
        c.post(
            "/api/v1/memories/search",
            json={"query": "x", "recency_weight": 1.5},
            headers=auth_header,
        ).status_code
        == 422
    )


def test_search_keyword_mode(app_instance, mem, auth_header):
    from types import SimpleNamespace

    point = SimpleNamespace(id="1", payload={"data": "the Philips hub", "created_at": "2026-06-01T00:00:00+00:00"})  # noqa: E501
    mem.vector_store.list.return_value = ([point], None)
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search",
        json={"query": "philips", "mode": "keyword"},
        headers=auth_header,
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["id"] == "1"
    mem.search.assert_not_called()  # keyword mode bypasses vector search
    _, kwargs = mem.vector_store.list.call_args
    assert kwargs["filters"] == {"user_id": "default-user"}


def test_search_invalid_mode_rejected(app_instance, mem, auth_header):
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search", json={"query": "x", "mode": "fuzzy"}, headers=auth_header
    )
    assert resp.status_code == 422


def test_list_filters_by_provenance_metadata(app_instance, mem, auth_header):
    mem.get_all.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.get(
        "/api/v1/memories?source=import:chatgpt&confidence=high&review_status=approved",
        headers=auth_header,
    )
    assert resp.status_code == 200
    _, kwargs = mem.get_all.call_args
    f = kwargs["filters"]
    assert f["user_id"] == "default-user"
    assert f["source"] == "import:chatgpt"
    assert f["confidence"] == "high"
    assert f["review_status"] == "approved"


def test_list_exclude_expired(app_instance, mem, auth_header):
    mem.get_all.return_value = {
        "results": [
            {"id": "stale", "metadata": {"expires_at": "2000-01-01T00:00:00+00:00"}},
            {"id": "fresh", "metadata": {}},
        ]
    }
    c = _client(app_instance)
    resp = c.get("/api/v1/memories?exclude_expired=true", headers=auth_header)
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["results"]] == ["fresh"]


def test_search_filters_by_provenance_metadata(app_instance, mem, auth_header):
    mem.search.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search",
        json={"query": "x", "review_status": "approved"},
        headers=auth_header,
    )
    assert resp.status_code == 200
    _, kwargs = mem.search.call_args
    assert kwargs["filters"]["review_status"] == "approved"
    assert kwargs["filters"]["user_id"] == "default-user"


def test_search_exclude_expired(app_instance, mem, auth_header):
    mem.search.return_value = {
        "results": [
            {"id": "stale", "metadata": {"expires_at": "2000-01-01T00:00:00+00:00"}},
            {"id": "fresh", "metadata": {}},
        ]
    }
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search",
        json={"query": "x", "exclude_expired": True},
        headers=auth_header,
    )
    assert resp.status_code == 200
    assert [i["id"] for i in resp.json()["results"]] == ["fresh"]


def test_search_scoped_by_run_id(app_instance, mem, auth_header):
    mem.search.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search",
        json={"query": "x", "run_id": "r1"},
        headers=auth_header,
    )
    assert resp.status_code == 200
    _, kwargs = mem.search.call_args
    assert kwargs["filters"]["run_id"] == "r1"


def test_list_scoped_by_run_id(app_instance, mem, auth_header):
    mem.get_all.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.get("/api/v1/memories?run_id=r1", headers=auth_header)
    assert resp.status_code == 200
    _, kwargs = mem.get_all.call_args
    assert kwargs["filters"]["run_id"] == "r1"


def test_list_limit_out_of_range_rejected(app_instance, mem, auth_header):
    c = _client(app_instance)
    assert c.get("/api/v1/memories?limit=0", headers=auth_header).status_code == 422
    assert c.get("/api/v1/memories?limit=1000", headers=auth_header).status_code == 422


def test_delete(app_instance, mem, auth_header):
    c = _client(app_instance)
    resp = c.delete("/api/v1/memories/abc", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "memory_id": "abc"}
    mem.delete.assert_called_once_with(memory_id="abc")


def test_get_by_id_found(app_instance, mem, auth_header):
    mem.get.return_value = {"id": "abc", "memory": "hi"}
    c = _client(app_instance)
    resp = c.get("/api/v1/memories/abc", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == {"id": "abc", "memory": "hi"}
    mem.get.assert_called_once_with(memory_id="abc")


def test_get_by_id_not_found(app_instance, mem, auth_header):
    mem.get.return_value = None
    c = _client(app_instance)
    resp = c.get("/api/v1/memories/missing", headers=auth_header)
    assert resp.status_code == 404
    mem.get.assert_called_once_with(memory_id="missing")
    assert resp.json()["detail"] == "Memory not found"


def test_update(app_instance, mem, auth_header):
    mem.update.return_value = {"id": "abc", "memory": "updated"}
    c = _client(app_instance)
    resp = c.put("/api/v1/memories/abc", json={"content": "updated"}, headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == {"id": "abc", "memory": "updated"}
    mem.update.assert_called_once_with(memory_id="abc", data="updated")


def test_history(app_instance, mem, auth_header):
    mem.history.return_value = [{"event": "ADD"}]
    c = _client(app_instance)
    resp = c.get("/api/v1/memories/abc/history", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json() == {"history": [{"event": "ADD"}]}
    mem.history.assert_called_once_with(memory_id="abc")


@respx.mock
def test_healthz_ok(app_instance):
    respx.get("https://qdrant.test:443/collections").mock(
        return_value=Response(200, json={"result": {}})
    )
    c = _client(app_instance)
    resp = c.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@respx.mock
def test_healthz_unreachable(app_instance):
    respx.get("https://qdrant.test:443/collections").mock(
        return_value=Response(500)
    )
    c = _client(app_instance)
    resp = c.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def _bulk(c, auth_header, body):
    return c.post("/api/v1/memories/delete_bulk", json=body, headers=auth_header)


def test_bulk_delete_requires_a_filter(app_instance, mem, auth_header):
    c = _client(app_instance)
    # No filter at all, and user_id alone, must both be rejected.
    assert _bulk(c, auth_header, {}).status_code == 422
    assert _bulk(c, auth_header, {"user_id": "default-user"}).status_code == 422
    assert _bulk(c, auth_header, {"confirm": True}).status_code == 422
    mem.vector_store.list.assert_not_called()
    mem.delete.assert_not_called()


def test_bulk_delete_dry_run_by_default(app_instance, mem, auth_header):
    from types import SimpleNamespace

    points = [
        SimpleNamespace(id=f"m{i}", payload={"data": f"fact {i}"}) for i in range(3)
    ]
    mem.vector_store.list.return_value = (points, None)
    c = _client(app_instance)
    resp = _bulk(c, auth_header, {"agent_id": "capture:telegram"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["matched"] == 3
    assert body["deleted"] == 0
    assert body["has_more"] is False
    assert body["sample"][0] == {"id": "m0", "memory": "fact 0"}
    mem.delete.assert_not_called()
    _, kwargs = mem.vector_store.list.call_args
    assert kwargs["filters"] == {
        "user_id": "default-user",
        "agent_id": "capture:telegram",
    }


def test_bulk_delete_confirm_deletes_each_id(app_instance, mem, auth_header):
    from types import SimpleNamespace

    points = [SimpleNamespace(id=f"m{i}", payload={"data": "x"}) for i in range(3)]
    mem.vector_store.list.return_value = (points, None)
    c = _client(app_instance)
    body = _bulk(
        c, auth_header, {"source": "import:chatgpt", "confirm": True}
    ).json()
    assert body["dry_run"] is False
    assert body["deleted"] == 3
    assert [c.kwargs["memory_id"] for c in mem.delete.call_args_list] == [
        "m0",
        "m1",
        "m2",
    ]


def test_bulk_delete_composes_provenance_and_agent_filters(
    app_instance, mem, auth_header
):
    mem.vector_store.list.return_value = ([], None)
    c = _client(app_instance)
    resp = _bulk(
        c,
        auth_header,
        {"agent_id": "n8n", "review_status": "rejected", "confidence": "low"},
    )
    assert resp.status_code == 200
    _, kwargs = mem.vector_store.list.call_args
    assert kwargs["filters"] == {
        "user_id": "default-user",
        "agent_id": "n8n",
        "review_status": "rejected",
        "confidence": "low",
    }
