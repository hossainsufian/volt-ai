"""Volt Code's agent loop: a brain asks for tools, the results go back to it."""

import json

import pytest

from volt_code.agent import run_turn
from volt_code.brains import LocalBrain, ToolCall
from volt_code.tools import Toolbox


class ScriptedBrain:
    """Plays back a fixed list of turns: each is (text, [ToolCall, ...])."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.users, self.results = [], []

    def add_user(self, text):
        self.users.append(text)

    def step(self, on_text):
        text, calls = self.turns.pop(0)
        if text:
            on_text(text)
        return calls

    def add_results(self, results):
        self.results.append([(c.name, out, failed) for c, out, failed in results])


class QuietUI:
    def __init__(self):
        self.text = ""

    def text_chunk(self, t): self.text += t
    def end_text(self): pass
    def tool_start(self, call): pass
    def tool_done(self, call, output, failed): pass
    def notice(self, message): pass


def project(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    return tmp_path


def test_tools_run_and_results_go_back_until_the_brain_is_done(tmp_path):
    brain = ScriptedBrain([
        ("Let me look.", [ToolCall("1", "read_file", {"path": "a.txt"})]),
        ("It says alpha.", []),
    ])
    ui = QuietUI()
    run_turn(brain, Toolbox(project(tmp_path), approve=lambda *_: True), "what's in a.txt?", ui)
    assert brain.users == ["what's in a.txt?"]
    assert brain.results[0][0][0] == "read_file" and "alpha" in brain.results[0][0][1]
    assert ui.text == "Let me look.It says alpha."


def test_declined_and_invalid_calls_come_back_as_errors(tmp_path):
    brain = ScriptedBrain([
        ("", [ToolCall("1", "write_file", {"path": "b.txt", "content": "x"}),
              ToolCall("2", "read_file", {}, error="Missing 'path'.")]),
        ("Okay.", []),
    ])
    run_turn(brain, Toolbox(project(tmp_path), approve=lambda *_: False), "write b", QuietUI())
    (w_name, w_out, w_failed), (r_name, r_out, r_failed) = brain.results[0]
    assert w_failed and "did not allow" in w_out
    assert r_failed and "Missing" in r_out
    assert not (tmp_path / "b.txt").exists()


def test_stopping_mid_turn_still_answers_every_tool_call(tmp_path):
    def approve(*_):
        raise KeyboardInterrupt

    brain = ScriptedBrain([("", [ToolCall("1", "run_command", {"command": "ls"}),
                                 ToolCall("2", "read_file", {"path": "a.txt"})])])
    with pytest.raises(KeyboardInterrupt):
        run_turn(brain, Toolbox(project(tmp_path), approve=approve), "go", QuietUI())
    names = [name for name, _, failed in brain.results[0] if failed]
    assert names == ["run_command", "read_file"]


class FakeResponse:
    def __init__(self, chunks): self._lines = [json.dumps(c).encode() for c in chunks]
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def raise_for_status(self): pass
    def iter_lines(self): yield from self._lines


def test_local_brain_understands_a_tool_call_written_as_text():
    reply = '{"name": "list_files", "arguments": {"pattern": "**/*.py"}}'
    brain = LocalBrain("m", "http://x", "sys",
                       post=lambda *a, **k: FakeResponse([{"message": {"content": reply}, "done": True}]))
    brain.add_user("list python files")
    calls = brain.step(lambda t: None)
    assert [(c.name, c.input, c.error) for c in calls] == [("list_files", {"pattern": "**/*.py"}, None)]


class FakeBlock:
    def __init__(self, **kw): self.__dict__.update(kw)


class FakeStream:
    def __init__(self, message): self.message = message
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    @property
    def text_stream(self):
        return iter([b.text for b in self.message.content if b.type == "text"])
    def get_final_message(self): return self.message


class FakeClaude:
    def __init__(self, messages):
        self.queue, self.sent = list(messages), []
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        self.sent.append(kwargs)
        return FakeStream(self.queue.pop(0))


def claude_reply(stop_reason, *blocks):
    return FakeBlock(stop_reason=stop_reason, content=list(blocks))


def test_claude_brain_returns_checked_tool_calls():
    from volt_code.brains import ClaudeBrain

    fake = FakeClaude([claude_reply(
        "tool_use",
        FakeBlock(type="text", text="Reading."),
        FakeBlock(type="tool_use", id="t1", name="read_file", input={"path": "a.py"}),
        FakeBlock(type="tool_use", id="t2", name="write_file", input={"path": "b.py"}),
    )])
    brain = ClaudeBrain("key", "claude-opus-5-5", "sys", client=fake)
    brain.add_user("hi")
    seen = []
    calls = brain.step(seen.append)
    assert seen == ["Reading."]
    assert calls[0].error is None
    assert calls[1].error and "content" in calls[1].error  # missing content: never run
    assert all(t.get("eager_input_streaming") for t in fake.sent[0]["tools"])


def test_claude_brain_never_runs_tools_from_a_refused_reply():
    from volt_code.brains import ClaudeBrain

    fake = FakeClaude([claude_reply(
        "refusal", FakeBlock(type="tool_use", id="t1", name="run_command", input={"command": "ls"}))])
    brain = ClaudeBrain("key", "claude-opus-5-5", "sys", client=fake)
    brain.add_user("hi")
    [call] = brain.step(lambda t: None)
    assert call.error
