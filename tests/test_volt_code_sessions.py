"""Volt Code keeps each chat so it can be continued later."""

import pytest

from volt_code import sessions
from volt_code.brains import LocalBrain


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def brain_with(*texts):
    brain = LocalBrain("m", "http://x", "system prompt")
    for i, text in enumerate(texts):
        brain.messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": text})
    return brain


def test_saved_chat_comes_back_in_the_same_folder(tmp_path):
    project = tmp_path / "proj"
    session = sessions.Session.new(project, "Local")
    session.save(brain_with("make a quiz", "Done, wrote index.html"))
    latest = sessions.latest(project)
    assert latest is not None and latest.title == "make a quiz"
    fresh = LocalBrain("m", "http://x", "new system prompt")
    assert latest.restore_into(fresh)
    assert fresh.messages[0]["content"] == "new system prompt"  # today's instructions kept
    assert [m["content"] for m in fresh.messages[1:]] == ["make a quiz", "Done, wrote index.html"]


def test_chats_from_other_folders_are_not_offered(tmp_path):
    sessions.Session.new(tmp_path / "a", "Local").save(brain_with("in a"))
    assert sessions.latest(tmp_path / "b") is None
    assert [s.title for s in sessions.list_for(tmp_path / "a")] == ["in a"]


def test_newest_chat_is_listed_first(tmp_path):
    project = tmp_path / "proj"
    sessions.Session.new(project, "Local").save(brain_with("first"))
    sessions.Session.new(project, "Local").save(brain_with("second"))
    assert [s.title for s in sessions.list_for(project)] == ["second", "first"]


def test_a_chat_only_continues_on_the_same_kind_of_brain(tmp_path):
    project = tmp_path / "proj"
    sessions.Session.new(project, "Claude").save(brain_with("hi"))
    assert not sessions.latest(project).restore_into(LocalBrain("m", "http://x", "sys"))


def test_empty_chats_are_not_saved(tmp_path):
    project = tmp_path / "proj"
    sessions.Session.new(project, "Local").save(LocalBrain("m", "http://x", "sys"))
    assert sessions.latest(project) is None
