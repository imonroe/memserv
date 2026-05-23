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
    assert kwargs["user_id"] == "ian"


def test_add_memory_requires_content_or_messages(app_instance, mem, auth_header):
    c = _client(app_instance)
    resp = c.post("/api/v1/memories", json={}, headers=auth_header)
    assert resp.status_code == 422


def test_search(app_instance, mem, auth_header):
    mem.search.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.post(
        "/api/v1/memories/search", json={"query": "where", "limit": 5}, headers=auth_header
    )
    assert resp.status_code == 200
    _, kwargs = mem.search.call_args
    assert kwargs["limit"] == 5
    assert kwargs["user_id"] == "ian"


def test_list(app_instance, mem, auth_header):
    mem.get_all.return_value = {"results": []}
    c = _client(app_instance)
    resp = c.get("/api/v1/memories?agent_id=n8n", headers=auth_header)
    assert resp.status_code == 200
    _, kwargs = mem.get_all.call_args
    assert kwargs["agent_id"] == "n8n"


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


def test_update(app_instance, mem, auth_header):
    mem.update.return_value = {"id": "abc", "memory": "updated"}
    c = _client(app_instance)
    resp = c.put("/api/v1/memories/abc", json={"content": "updated"}, headers=auth_header)
    assert resp.status_code == 200
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
