"""Tests for Volt AI's fast mode (one direct, streamed local model call)."""

import json

import pytest

from desktop_app import volt_fast
from desktop_app.volt_claude import Stopped


class FakeResponse:
    def __init__(self, chunks):
        self._lines = [json.dumps(c).encode() for c in chunks]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self._lines


def fake_post(chunks, seen):
    def post(url, json=None, **kwargs):
        seen.update(url=url, body=json)
        return FakeResponse(chunks)
    return post


CHUNKS = [
    {"message": {"content": "Hello"}},
    {"message": {"content": " there"}},
    {"message": {"content": "!"}, "done": True},
]


def test_streams_text_in_order(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    seen, progress = {}, []
    answer = volt_fast.reply([{"role": "user", "content": "hi"}],
                             on_text=progress.append, post=fake_post(CHUNKS, seen))
    assert answer == "Hello there!"
    assert progress == ["Hello", "Hello there", "Hello there!"]
    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["stream"] is True
    assert seen["body"]["model"] == volt_fast.DEFAULT_MODEL


def test_uses_configured_model_and_url(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ollama_chat_model": "tiny:1b", "ollama_base_url": "http://box:1234/"}))
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg))
    seen = {}
    volt_fast.reply([{"role": "user", "content": "hi"}], post=fake_post(CHUNKS, seen))
    assert seen["url"] == "http://box:1234/api/chat"
    assert seen["body"]["model"] == "tiny:1b"


def test_history_is_trimmed_and_starts_with_user(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    turns = []
    for i in range(20):
        turns += [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": f"a{i}"}]
    turns.append({"role": "user", "content": "last"})
    seen = {}
    volt_fast.reply(turns, post=fake_post(CHUNKS, seen))
    msgs = seen["body"]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[-1]["content"] == "last"
    assert len(msgs) - 1 <= volt_fast.MAX_TURNS


def test_stop_keeps_partial_text(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(Stopped) as info:
        volt_fast.reply([{"role": "user", "content": "hi"}],
                        should_stop=lambda: True, post=fake_post(CHUNKS, {}))
    assert info.value.partial == "Hello"


def test_model_error_is_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError):
        volt_fast.reply([{"role": "user", "content": "hi"}],
                        post=fake_post([{"error": "model 'x' not found"}], {}))
    assert "isn't installed" in volt_fast.friendly_error(RuntimeError("model 'x' not found"))
