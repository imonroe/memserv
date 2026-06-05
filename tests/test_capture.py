import json

import httpx
import pytest
import respx

from capture import capture as c

TG = c.TELEGRAM_API
MEM = "https://mem0.test/api/v1/memories"
SEND = f"{TG}/botBOTTOKEN/sendMessage"
CFG = c.Config(
    base_url="https://mem0.test",
    api_key="tok",
    token="BOTTOKEN",
    allowed_ids={111},
    agent_id="capture:telegram",
)


def _ok(body=None):
    return httpx.Response(200, json=body or {})


def _routes():
    """Mock the memory POST and Telegram sendMessage; return (mem, send) routes."""
    mem = respx.post(MEM).mock(return_value=_ok())
    send = respx.post(SEND).mock(return_value=_ok())
    return mem, send


def _update(text, chat_id=111, user_id=111, update_id=1):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text},
    }


# --- pure helpers ------------------------------------------------------------


def test_parse_allowed_chat_ids():
    assert c.parse_allowed_chat_ids("111, 222 ,bad,333") == {111, 222, 333}
    assert c.parse_allowed_chat_ids("") == set()
    assert c.parse_allowed_chat_ids(None) == set()


def test_extract_message_variants():
    assert c.extract_message(_update("hi")) == (111, 111, "hi")
    only_chat = {"message": {"chat": {"id": 5}, "text": "  spaced  "}}
    assert c.extract_message(only_chat) == (5, None, "spaced")
    assert c.extract_message({"message": {"chat": {"id": 5}}}) is None  # no text
    assert c.extract_message({"message": {"text": "x"}}) is None  # no chat id
    assert c.extract_message({}) is None
    assert c.extract_message({"edited_message": {"chat": {"id": 9}, "text": "e"}}) == (9, None, "e")


def test_classify():
    assert c.classify("plain text") == ("note", "plain text")
    assert c.classify("/note buy milk") == ("note", "buy milk")
    assert c.classify("/save@MyBot something") == ("note", "something")
    assert c.classify("/remember") == ("empty", "")
    assert c.classify("/start") == ("help", "")
    assert c.classify("/help") == ("help", "")
    assert c.classify("/unknown blah") == ("help", "")


# --- network helpers (respx) -------------------------------------------------


@respx.mock
def test_get_updates_returns_result_list():
    respx.get(f"{TG}/botBOTTOKEN/getUpdates").mock(
        return_value=_ok({"ok": True, "result": [{"update_id": 7}]})
    )
    with httpx.Client() as client:
        assert c.get_updates("BOTTOKEN", 5, 30, client=client) == [{"update_id": 7}]


@respx.mock
def test_get_updates_raises_on_not_ok():
    # Telegram returns HTTP 200 with ok:false on errors (e.g. bad token); the
    # bot must treat that as an error, not silently idle.
    respx.get(f"{TG}/botBOTTOKEN/getUpdates").mock(
        return_value=_ok({"ok": False, "description": "Unauthorized"})
    )
    with httpx.Client() as client, pytest.raises(RuntimeError):
        c.get_updates("BOTTOKEN", None, 30, client=client)


def test_int_env_validates(monkeypatch):
    monkeypatch.delenv("X_T", raising=False)
    assert c._int_env("X_T", 30) == 30  # unset -> default
    monkeypatch.setenv("X_T", "5")
    assert c._int_env("X_T", 30) == 5
    monkeypatch.setenv("X_T", "0")  # below minimum
    with pytest.raises(SystemExit):
        c._int_env("X_T", 30)
    monkeypatch.setenv("X_T", "abc")  # unparseable
    with pytest.raises(SystemExit):
        c._int_env("X_T", 30)


@respx.mock
def test_post_memory_sends_bearer_and_provenance():
    route = respx.post(MEM).mock(return_value=_ok({"results": [{"id": "1"}]}))
    with httpx.Client() as client:
        c.post_memory(
            "https://mem0.test/", "tok", "remember this", "capture:telegram", client=client
        )
    req = route.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body["content"] == "remember this"
    assert body["agent_id"] == "capture:telegram"
    assert body["metadata"] == {"source": "capture:telegram"}


# --- process_update wiring ---------------------------------------------------


@respx.mock
def test_process_update_saves_for_authorized_chat():
    mem, send = _routes()
    with httpx.Client() as client:
        c.process_update(_update("buy milk"), CFG, client=client)
    assert mem.called
    assert json.loads(mem.calls.last.request.content)["content"] == "buy milk"
    assert b"Saved" in send.calls.last.request.content


@respx.mock
def test_process_update_save_failure_sends_generic_message():
    respx.post(MEM).mock(return_value=httpx.Response(500))
    send = respx.post(SEND).mock(return_value=_ok())
    with httpx.Client() as client:
        c.process_update(_update("note"), CFG, client=client)
    body = send.calls.last.request.content
    assert b"try again" in body
    assert b"mem0.test" not in body  # no internal URL/detail leaked to the user


@respx.mock
def test_process_update_rejects_unauthorized_chat():
    mem, send = _routes()
    with httpx.Client() as client:
        c.process_update(_update("secret", chat_id=999), CFG, client=client)
    assert not mem.called  # never stores for a non-allowlisted chat
    assert b"not authorized" in send.calls.last.request.content


@respx.mock
def test_process_update_discovery_mode_when_no_allowlist():
    cfg = CFG._replace(allowed_ids=set())
    mem, send = _routes()
    with httpx.Client() as client:
        c.process_update(_update("hello", chat_id=42), cfg, client=client)
    assert not mem.called  # discovery mode never stores
    body = send.calls.last.request.content
    assert b"42" in body and b"TELEGRAM_ALLOWED_CHAT_IDS" in body


@respx.mock
def test_process_update_help_does_not_store():
    mem, send = _routes()
    with httpx.Client() as client:
        c.process_update(_update("/start"), CFG, client=client)
    assert not mem.called
    assert send.called


@respx.mock
def test_process_update_empty_note_prompts():
    mem, send = _routes()
    with httpx.Client() as client:
        c.process_update(_update("/note"), CFG, client=client)
    assert not mem.called
    assert b"Send some text" in send.calls.last.request.content


def test_process_update_ignores_non_text_update():
    # A non-message update (e.g. a chat-member change) is silently ignored.
    with httpx.Client() as client:
        c.process_update({"update_id": 1, "my_chat_member": {}}, CFG, client=client)
