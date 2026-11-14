"""Retry an answer and edit-and-resend a message, as in Claude."""

import json
import time

import pytest

from desktop_app.volt_store import VoltStore
from desktop_app.volt_web import VoltWebServer

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class Bridge:
    def is_available(self): return True
    def submit(self, text): return True
    def cancel(self): pass
    def restore(self, messages): pass


class Lines:
    def __init__(self, text): self._l = [json.dumps({"message": {"content": text}, "done": True}).encode()]
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def raise_for_status(self): pass
    def iter_lines(self): yield from self._l


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    srv = VoltWebServer(VoltStore(tmp_path / "data"), Bridge(), port=0)
    asked = []

    def post(url, json=None, **k):
        asked.append([m["content"] for m in json["messages"] if m["role"] == "user"])
        return Lines(f"answer {len(asked)}")

    srv._fast_post = post
    c = srv.app.test_client()
    c.get(f"/?t={srv.desktop_token}", environ_base=LOCAL)
    chat_id = c.post("/api/chats", json={}, environ_base=LOCAL).get_json()["chat"]["id"]
    return srv, c, chat_id, asked


def say(srv, c, chat_id, text):
    c.post(f"/api/chats/{chat_id}/messages", json={"text": text}, environ_base=LOCAL)
    wait(srv)


def wait(srv):
    for _ in range(200):
        if srv.store.pending_message() is None:
            return
        time.sleep(0.02)
    raise AssertionError("reply never finished")


def texts(srv, chat_id):
    return [(m["role"], m["text"]) for m in srv.store.messages(chat_id)]


def test_retry_replaces_the_last_answer(web):
    srv, c, chat_id, asked = web
    say(srv, c, chat_id, "hi")
    answer = srv.store.messages(chat_id)[-1]
    assert c.post(f"/api/messages/{answer['id']}/retry", environ_base=LOCAL).status_code == 200
    wait(srv)
    assert texts(srv, chat_id) == [("user", "hi"), ("assistant", "answer 2")]
    assert asked[-1] == ["hi"]  # the same question was asked again


def test_edit_changes_the_message_and_forgets_what_came_after(web):
    srv, c, chat_id, asked = web
    say(srv, c, chat_id, "first")
    say(srv, c, chat_id, "second")
    first = srv.store.messages(chat_id)[0]
    r = c.post(f"/api/messages/{first['id']}/edit", json={"text": "first, but better"}, environ_base=LOCAL)
    assert r.status_code == 200
    wait(srv)
    assert texts(srv, chat_id) == [("user", "first, but better"), ("assistant", "answer 3")]
    assert asked[-1] == ["first, but better"]


def test_only_answers_can_be_retried_and_only_your_messages_edited(web):
    srv, c, chat_id, asked = web
    say(srv, c, chat_id, "hi")
    user, answer = srv.store.messages(chat_id)
    assert c.post(f"/api/messages/{user['id']}/retry", environ_base=LOCAL).status_code == 400
    assert c.post(f"/api/messages/{answer['id']}/edit", json={"text": "x"}, environ_base=LOCAL).status_code == 400
    assert c.post(f"/api/messages/{user['id']}/edit", json={"text": ""}, environ_base=LOCAL).status_code == 400
