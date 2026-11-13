"""Online brain: Gemini (or any OpenAI-compatible service) with the user's own key."""

import json

import pytest

from desktop_app import volt_online
from desktop_app.volt_claude import Stopped


class FakeSSE:
    def __init__(self, deltas, status=200):
        self.status_code = status
        self._lines = [f"data: {json.dumps({'choices': [{'delta': {'content': d}}]})}".encode() for d in deltas]
        self._lines += [b"", b"data: [DONE]"]

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)

    def iter_lines(self): yield from self._lines


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_picks_the_newest_plain_flash_model():
    ids = ["models/gemini-2.0-flash", "models/gemini-2.5-flash", "models/gemini-2.5-flash-lite",
           "models/gemini-2.5-flash-image", "models/gemini-2.5-pro", "models/text-embedding-004"]
    assert volt_online.pick_model(ids) == "gemini-2.5-flash"


def test_key_is_saved_privately_and_masked(tmp_path):
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-2.5-flash")
    path = volt_online.config_path()
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert volt_online.masked_key().endswith("cdef") and "TESTKEY" not in volt_online.masked_key()
    volt_online.clear()
    assert volt_online.load() is None


def test_reply_streams_text(tmp_path):
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-2.5-flash")
    seen, progress = {}, []

    def post(url, json=None, headers=None, **kw):
        seen.update(url=url, body=json, headers=headers)
        return FakeSSE(["Hel", "lo!"])

    answer = volt_online.reply([{"role": "user", "content": "hi"}], on_text=progress.append,
                               extra_system="\n\nextra", post=post)
    assert answer == "Hello!"
    assert progress == ["Hel", "Hello!"]
    assert seen["url"].endswith("/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer AIzaSyTESTKEY1234567890abcdef"
    assert seen["body"]["model"] == "gemini-2.5-flash" and seen["body"]["stream"] is True
    assert seen["body"]["messages"][0]["role"] == "system"
    assert seen["body"]["messages"][0]["content"].endswith("extra")


def test_stop_keeps_partial_text():
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-2.5-flash")
    with pytest.raises(Stopped) as info:
        volt_online.reply([{"role": "user", "content": "hi"}], should_stop=lambda: True,
                          post=lambda *a, **k: FakeSSE(["Hel", "lo"]))
    assert info.value.partial == "Hel"


def test_friendly_errors():
    import requests

    bad_key = requests.HTTPError(response=type("R", (), {"status_code": 400})())
    assert "key" in volt_online.friendly_error(bad_key).lower()
    limit = requests.HTTPError(response=type("R", (), {"status_code": 429})())
    assert "limit" in volt_online.friendly_error(limit).lower()


# --- Volt's web server: choosing and using the online brain -------------------------

import time as _time

from desktop_app.volt_store import VoltStore
from desktop_app.volt_web import VoltWebServer

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


class Bridge:
    def is_available(self): return True
    def submit(self, text): return True
    def cancel(self): pass
    def restore(self, messages): pass


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(volt_online, "check_key", lambda key, get=None: "gemini-test-flash")
    srv = VoltWebServer(VoltStore(tmp_path / "data"), Bridge(), port=0)
    srv._online_post = lambda *a, **k: FakeSSE(["Fast ", "answer."])
    client = srv.app.test_client()
    client.get(f"/?t={srv.desktop_token}", environ_base=LOCAL)
    return srv, client


def test_saving_a_key_switches_to_the_online_brain(web):
    srv, client = web
    resp = client.post("/api/online/key", json={"key": "AIzaSyTESTKEY1234567890abcdef"}, environ_base=LOCAL)
    assert resp.status_code == 200
    assert srv.brain() == "online"
    state = client.get("/api/state", environ_base=LOCAL).get_json()
    assert state["online"]["has_key"] and state["online"]["model"] == "gemini-test-flash"


def test_online_brain_answers_messages(web):
    srv, client = web
    client.post("/api/online/key", json={"key": "AIzaSyTESTKEY1234567890abcdef"}, environ_base=LOCAL)
    chat = client.post("/api/chats", json={}, environ_base=LOCAL).get_json()["chat"]
    client.post(f"/api/chats/{chat['id']}/messages", json={"text": "hi"}, environ_base=LOCAL)
    for _ in range(100):
        if srv.store.pending_message() is None:
            break
        _time.sleep(0.02)
    msgs = client.get(f"/api/chats/{chat['id']}", environ_base=LOCAL).get_json()["messages"]
    assert msgs[-1]["text"] == "Fast answer." and msgs[-1]["status"] == "done"


def test_brain_picker_switches_between_online_and_local(web):
    srv, client = web
    assert client.post("/api/brain", json={"brain": "online"}, environ_base=LOCAL).status_code == 400
    client.post("/api/online/key", json={"key": "AIzaSyTESTKEY1234567890abcdef"}, environ_base=LOCAL)
    client.post("/api/brain", json={"brain": "fast"}, environ_base=LOCAL)
    assert srv.brain() == "fast"
    client.post("/api/brain", json={"brain": "online"}, environ_base=LOCAL)
    assert srv.brain() == "online"


def test_removing_the_key_goes_back_to_local(web):
    srv, client = web
    client.post("/api/online/key", json={"key": "AIzaSyTESTKEY1234567890abcdef"}, environ_base=LOCAL)
    client.post("/api/online/key", json={"key": ""}, environ_base=LOCAL)
    assert srv.brain() == "fast" and volt_online.load() is None


def test_busy_model_falls_back_to_the_next_one():
    volt_online.save("AIzaSyTESTKEY1234567890abcdef", "gemini-3.8-flash")
    tried = []

    class Busy(FakeSSE):
        def __init__(self): super().__init__([], status=503)

    def post(url, json=None, headers=None, **kw):
        tried.append(json["model"])
        return Busy() if json["model"] == "gemini-3.8-flash" else FakeSSE(["Hi!"])

    assert volt_online.reply([{"role": "user", "content": "hi"}], post=post) == "Hi!"
    assert tried == ["gemini-3.8-flash", volt_online.BACKUP_MODELS[0]]
