"""Volt AI image creation: styles, shapes, prompt help, Pollinations with or without a key."""

import base64
import json
import sqlite3

import pytest

from desktop_app import volt_images as vi


class Resp:
    def __init__(self, status=200, ctype="image/jpeg", content=b"\xff\xd8jpeg", body=None):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = content if body is None else json.dumps(body).encode()
        self._body = body

    def json(self): return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_style_and_shape_shape_the_request():
    prompt = vi.styled_prompt("a fox", "anime")
    assert prompt.startswith("a fox") and "anime" in prompt
    assert vi.styled_prompt("a fox", "auto") == "a fox"
    assert vi.SHAPES["portrait"][1] < vi.SHAPES["portrait"][2]  # taller than wide


def test_without_a_key_the_free_service_is_used():
    seen = {}

    def get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers or {})
        return Resp()

    data, suffix = vi.generate("a fox", "landscape", seed=5, get=get)
    assert data.startswith(b"\xff\xd8") and suffix == ".jpg"
    assert seen["url"].startswith(vi.CLASSIC_URL) and "seed=5" in seen["url"]
    assert "width=1344" in seen["url"] and "Authorization" not in seen["headers"]


def test_with_a_key_the_good_models_are_used():
    vi.save_key("sk_test_123456789")
    seen = {}

    def get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers or {})
        return Resp(ctype="image/png", content=b"\x89PNG")

    data, suffix = vi.generate("a fox", "square", seed=9, model="black-forest-labs/flux.1-schnell", get=get)
    assert suffix == ".png"
    assert seen["url"].startswith(vi.GEN_URL + "/image/")
    assert "model=black-forest-labs%2Fflux.1-schnell" in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer sk_test_123456789"


def test_free_limit_says_how_long_to_wait():
    body = {"error": {"code": "FREE_LIMIT", "message": "Free limit reached: 1 image every 45 seconds per IP. Retry after 28s, or get unlimited images"}}
    with pytest.raises(vi.RateLimited) as info:
        vi.generate("x", "square", seed=1, get=lambda *a, **k: Resp(402, "application/json", body=body))
    assert info.value.seconds == 28


def test_bad_key_is_explained():
    vi.save_key("sk_wrong_000000000")
    body = {"error": {"message": "A valid API key is required."}}
    with pytest.raises(vi.ImageError) as info:
        vi.generate("x", "square", seed=1, get=lambda *a, **k: Resp(401, "application/json", body=body))
    assert "key" in str(info.value).lower()


def test_edit_sends_the_picture_and_the_change():
    vi.save_key("sk_test_123456789")
    seen = {}
    out = base64.b64encode(b"\xff\xd8edited").decode()

    def post(url, headers=None, files=None, data=None, timeout=None):
        seen.update(url=url, headers=headers, files=files, data=data)
        return Resp(ctype="application/json", body={"data": [{"b64_json": out}]})

    result = vi.edit(b"\xff\xd8original", "make it night", post=post)
    assert result == b"\xff\xd8edited"
    assert seen["url"] == vi.GEN_URL + "/v1/images/edits"
    assert seen["data"]["prompt"] == "make it night" and seen["files"]["image"][1] == b"\xff\xd8original"


def test_improved_prompt_is_cleaned_and_falls_back():
    assert vi.clean_enhanced('"A red fox in fresh snow, golden light."\n\nHope this helps!', "fox") == \
        "fox. A red fox in fresh snow, golden light."
    # The user's own words always lead, so the subject can't drift away.
    assert vi.clean_enhanced("A humanoid girl in blue reading by a lamp, warm light", "cute robot reading a book") \
        .startswith("cute robot reading a book. ")
    assert vi.clean_enhanced("", "fox") == "fox"
    assert vi.clean_enhanced("ok", "a fox in snow") == "a fox in snow"  # too short to be a real prompt


def test_key_is_private(tmp_path):
    vi.save_key("sk_test_123456789")
    assert oct(vi.key_path().stat().st_mode)[-3:] == "600"
    assert "123456789" not in vi.masked_key()


def test_old_chat_databases_get_the_settings_column(tmp_path):
    db = sqlite3.connect(tmp_path / "chats.db")
    db.executescript("""CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '', image TEXT, status TEXT NOT NULL DEFAULT 'done', created REAL NOT NULL);""")
    db.close()
    from desktop_app.volt_store import VoltStore
    store = VoltStore(tmp_path)
    chat = store.create_chat("x")
    msg = store.add_message(chat["id"], "assistant", "", meta={"style": "anime", "seed": 3})
    assert store.get_message(msg["id"])["meta"] == {"style": "anime", "seed": 3}


# --- Through Volt's server ---------------------------------------------------------------

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
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    srv = VoltWebServer(VoltStore(tmp_path / "data"), Bridge(), port=0)
    srv.update_settings(images_enabled=True)
    calls = {"get": [], "post": []}

    def get(url, headers=None, timeout=None):
        calls["get"].append(url)
        return Resp(content=b"\xff\xd8" + str(len(calls["get"])).encode())

    def post(url, headers=None, files=None, data=None, timeout=None):
        calls["post"].append(data)
        return Resp(ctype="application/json", body={"data": [{"b64_json": base64.b64encode(b"\xff\xd8edit").decode()}]})

    srv._image_get, srv._image_post = get, post
    srv._enhance = lambda idea, style: f"{idea}, golden hour, wide shot, very detailed scene"
    client = srv.app.test_client()
    client.get(f"/?t={srv.desktop_token}", environ_base=LOCAL)
    chat = client.post("/api/chats", json={}, environ_base=LOCAL).get_json()["chat"]
    return srv, client, chat["id"], calls


def wait_images(srv, chat_id):
    for _ in range(200):
        if not any(m["status"] == "pending_image" for m in srv.store.messages(chat_id)):
            return [m for m in srv.store.messages(chat_id) if m["role"] == "assistant"]
        _time.sleep(0.02)
    raise AssertionError("images never finished")


def test_variations_use_style_shape_and_different_seeds(web):
    srv, client, chat_id, calls = web
    r = client.post(f"/api/chats/{chat_id}/images", json={"prompt": "a fox", "style": "anime",
                    "shape": "portrait", "count": 3, "enhance": True}, environ_base=LOCAL)
    assert r.status_code == 200
    done = wait_images(srv, chat_id)
    assert len(done) == 3 and all(m["image"] for m in done)
    seeds = {m["meta"]["seed"] for m in done}
    assert len(seeds) == 3
    assert all("anime" in u and "width=768" in u and "golden" in u for u in calls["get"])
    assert done[0]["meta"]["final_prompt"].startswith("a fox")


def test_again_makes_a_new_version_of_the_same_picture(web):
    srv, client, chat_id, calls = web
    client.post(f"/api/chats/{chat_id}/images", json={"prompt": "a fox", "enhance": False}, environ_base=LOCAL)
    [first] = wait_images(srv, chat_id)
    client.post(f"/api/images/{first['id']}/again", environ_base=LOCAL)
    done = wait_images(srv, chat_id)
    assert len(done) == 2 and done[1]["meta"]["seed"] != first["meta"]["seed"]
    assert done[1]["meta"]["final_prompt"] == first["meta"]["final_prompt"]


def test_change_edits_the_picture_with_a_key(web):
    srv, client, chat_id, calls = web
    save_key("sk_test_123456789")
    client.post(f"/api/chats/{chat_id}/images", json={"prompt": "a fox", "enhance": False}, environ_base=LOCAL)
    [first] = wait_images(srv, chat_id)
    client.post(f"/api/images/{first['id']}/change", json={"change": "make it night"}, environ_base=LOCAL)
    done = wait_images(srv, chat_id)
    assert calls["post"][0]["prompt"] == "make it night"
    assert srv.store.image_path(done[1]["image"]).read_bytes() == b"\xff\xd8edit"


def test_change_without_a_key_remakes_with_the_change_added(web):
    srv, client, chat_id, calls = web
    client.post(f"/api/chats/{chat_id}/images", json={"prompt": "a fox", "enhance": False}, environ_base=LOCAL)
    [first] = wait_images(srv, chat_id)
    client.post(f"/api/images/{first['id']}/change", json={"change": "make it night"}, environ_base=LOCAL)
    done = wait_images(srv, chat_id)
    assert not calls["post"] and "make%20it%20night" in calls["get"][-1]
    assert done[1]["meta"]["seed"] == first["meta"]["seed"]  # same seed keeps the layout close


def test_free_limit_waits_and_retries(web, monkeypatch):
    srv, client, chat_id, calls = web
    monkeypatch.setattr("desktop_app.volt_web.IMAGE_RETRY_CAP_SEC", 0)
    answers = [Resp(402, "application/json", body={"error": {"message": "Retry after 1s"}}), Resp()]
    srv._image_get = lambda *a, **k: answers.pop(0)
    client.post(f"/api/chats/{chat_id}/images", json={"prompt": "a fox", "enhance": False}, environ_base=LOCAL)
    [done] = wait_images(srv, chat_id)
    assert done["status"] == "done" and done["image"]


def test_diagram_style_asks_the_text_brain_for_an_svg(web):
    srv, client, chat_id, calls = web
    seen = []

    class Lines:
        def __init__(self): self._l = [json.dumps({"message": {"content": "Here.\n```svg\n<svg></svg>\n```"}, "done": True}).encode()]
        def __enter__(self): return self
        def __exit__(self, *e): return False
        def raise_for_status(self): pass
        def iter_lines(self): yield from self._l

    srv._fast_post = lambda url, json=None, **k: (seen.append(json), Lines())[1]
    r = client.post(f"/api/chats/{chat_id}/images", json={"prompt": "an LED with a resistor on a 9V battery",
                    "style": "diagram"}, environ_base=LOCAL)
    assert r.status_code == 200
    for _ in range(100):
        if srv.store.pending_message() is None:
            break
        _time.sleep(0.02)
    msgs = srv.store.messages(chat_id)
    assert "```svg" in msgs[-1]["text"] and not calls["get"]
    assert any("SVG" in m["content"] for m in seen[0]["messages"] if m["role"] == "system")


def save_key(key):
    vi.save_key(key)
