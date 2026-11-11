"""Artifact mode: Volt answers with one self-contained HTML file to preview."""

import json
import time

import pytest

from desktop_app import volt_fast
from desktop_app.volt_store import VoltStore
from desktop_app.volt_web import VoltWebServer

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class Bridge:
    def __init__(self): self.sent = []
    def is_available(self): return True
    def submit(self, text): self.sent.append(text); return True
    def cancel(self): pass
    def restore(self, messages): pass


class FakeResponse:
    def __init__(self, text): self._lines = [json.dumps({"message": {"content": text}, "done": True}).encode()]
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def raise_for_status(self): pass
    def iter_lines(self): yield from self._lines


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    bridge = Bridge()
    srv = VoltWebServer(VoltStore(tmp_path), bridge, port=0)
    seen = []

    def post(url, json=None, **kwargs):
        seen.append(json)
        return FakeResponse("Here it is.\n```html\n<h1>Hi</h1>\n```")

    srv._fast_post = post
    client = srv.app.test_client()
    client.get(f"/?t={srv.desktop_token}", environ_base=LOCAL)
    chat = client.post("/api/chats", json={}, environ_base=LOCAL).get_json()
    chat_id = (chat.get("chat") or chat)["id"]
    return srv, client, chat_id, seen, bridge


def send(client, chat_id, text, **extra):
    resp = client.post(f"/api/chats/{chat_id}/messages", json={"text": text, **extra}, environ_base=LOCAL)
    assert resp.status_code == 200, resp.get_json()
    return resp


def wait_done(srv):
    for _ in range(100):
        if srv.store.pending_message() is None:
            return
        time.sleep(0.02)
    raise AssertionError("reply never finished")


def instructions(body):
    return " ".join(m["content"] for m in body["messages"] if m["role"] == "system")


def test_artifact_message_asks_for_one_html_file(setup):
    srv, client, chat_id, seen, _ = setup
    send(client, chat_id, "make a stopwatch", artifact=True)
    wait_done(srv)
    assert "Artifact mode" in instructions(seen[-1])


def test_normal_message_has_no_artifact_instructions(setup):
    srv, client, chat_id, seen, _ = setup
    send(client, chat_id, "hello")
    wait_done(srv)
    assert "Artifact mode" not in instructions(seen[-1])


def test_artifacts_use_the_direct_brain_even_in_smart_mode(setup):
    srv, client, chat_id, seen, bridge = setup
    srv.update_settings(local_mode="smart")
    send(client, chat_id, "make a stopwatch", artifact=True)
    wait_done(srv)
    assert seen and not bridge.sent
