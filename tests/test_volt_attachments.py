"""Pictures and files attached to a chat message (paste, drag and drop, 📎)."""

import base64
import io
import json
import time

import pytest
from PIL import Image

from desktop_app import volt_online
from desktop_app.volt_store import VoltStore
from desktop_app.volt_web import VoltWebServer

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class Bridge:
    def is_available(self): return True
    def submit(self, text): return True
    def cancel(self): pass
    def restore(self, messages): pass


class SSE:
    def __init__(self, text):
        self._lines = [f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}".encode(), b"data: [DONE]"]
        self.status_code = 200
    def __enter__(self): return self
    def __exit__(self, *e): return False
    def raise_for_status(self): pass
    def iter_lines(self): yield from self._lines


def png_b64(colour="red"):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), colour).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    srv = VoltWebServer(VoltStore(tmp_path / "data"), Bridge(), port=0)
    sent = []
    srv._online_post = lambda url, json=None, **k: (sent.append(json), SSE("A red square."))[1]
    srv._fast_post = None
    client = srv.app.test_client()
    client.get(f"/?t={srv.desktop_token}", environ_base=LOCAL)
    chat = client.post("/api/chats", json={}, environ_base=LOCAL).get_json()["chat"]
    return srv, client, chat["id"], sent


def done(srv):
    for _ in range(200):
        if srv.store.pending_message() is None:
            return
        time.sleep(0.02)
    raise AssertionError("reply never finished")


def test_a_pasted_picture_reaches_a_brain_that_can_see(web):
    srv, client, chat_id, sent = web
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-test")
    r = client.post(f"/api/chats/{chat_id}/messages", environ_base=LOCAL, json={
        "text": "what is this?", "attachments": [{"name": "shot.png", "type": "image/png", "data": png_b64()}]})
    assert r.status_code == 200
    done(srv)
    last_user = [m for m in sent[-1]["messages"] if m["role"] == "user"][-1]
    parts = last_user["content"]
    assert any(p.get("type") == "text" and "what is this?" in p["text"] for p in parts)
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,")
               for p in parts)
    user_msg = [m for m in srv.store.messages(chat_id) if m["role"] == "user"][-1]
    assert user_msg["meta"]["attachments"][0]["name"] == "shot.png"
    assert srv.store.image_path(user_msg["meta"]["attachments"][0]["image"]).exists()


def test_without_a_seeing_brain_volt_says_how_to_get_one(web):
    srv, client, chat_id, sent = web
    r = client.post(f"/api/chats/{chat_id}/messages", environ_base=LOCAL, json={
        "text": "what is this?", "attachments": [{"name": "a.png", "type": "image/png", "data": png_b64()}]})
    assert r.status_code == 400 and "Gemini" in r.get_json()["error"]


def test_a_text_file_is_read_by_any_brain(web):
    srv, client, chat_id, sent = web
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-test")
    srv.update_settings(online_enabled=True)
    data = base64.b64encode(b"def add(a, b):\n    return a - b\n").decode()
    client.post(f"/api/chats/{chat_id}/messages", environ_base=LOCAL, json={
        "text": "find the bug", "attachments": [{"name": "calc.py", "type": "text/x-python", "data": data}]})
    done(srv)
    flat = json.dumps(sent[-1]["messages"])
    assert "return a - b" in flat and "calc.py" in flat


def test_too_many_or_too_big_attachments_are_refused(web):
    srv, client, chat_id, sent = web
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-test")
    many = [{"name": f"{i}.png", "type": "image/png", "data": png_b64()} for i in range(7)]
    assert client.post(f"/api/chats/{chat_id}/messages", environ_base=LOCAL,
                       json={"text": "x", "attachments": many}).status_code == 400
    huge = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()
    assert client.post(f"/api/chats/{chat_id}/messages", environ_base=LOCAL, json={
        "text": "x", "attachments": [{"name": "big.png", "type": "image/png", "data": huge}]}).status_code == 400
