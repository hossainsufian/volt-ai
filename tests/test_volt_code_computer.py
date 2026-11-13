"""Volt Code's screen control: Claude's computer toolset run on this machine.

The screen, mouse and keyboard are fakes, so nothing on the real desktop moves.
"""

import math

import pytest
from PIL import Image

from volt_code import computer as comp
from volt_code.agent import run_turn
from volt_code.brains import ClaudeBrain, ToolCall
from volt_code.computer import FAILED_EARLIER, Computer, ComputerError, parse_combo, scale_for
from volt_code.tools import Toolbox


class FakeMouse:
    def __init__(self): self.position, self.log = (0, 0), []
    def click(self, button, count): self.log.append(("click", button, count, self.position))
    def press(self, button): self.log.append(("press", button, self.position))
    def release(self, button): self.log.append(("release", button, self.position))
    def scroll(self, dx, dy): self.log.append(("scroll", dx, dy, self.position))


class FakeKeyboard:
    def __init__(self): self.log = []
    def press(self, k): self.log.append(("down", k))
    def release(self, k): self.log.append(("up", k))
    def type(self, text): self.log.append(("type", text))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(comp, "SETTLE_SEC", 0)


def make(width=3840, height=2160):
    image = Image.new("RGB", (width, height), "white")
    grab = lambda bbox=None: image.crop(bbox) if bbox else image  # noqa: E731
    return Computer(grab=grab, mouse=FakeMouse(), keyboard=FakeKeyboard())


def test_screenshots_fit_the_models_image_limits():
    for w, h in [(3840, 2160), (1920, 1080), (1366, 768), (2560, 1600)]:
        s = scale_for(w, h)
        sw, sh = w * s, h * s
        assert max(sw, sh) <= comp.MAX_LONG_EDGE
        assert math.ceil(sw / 28) * math.ceil(sh / 28) <= comp.MAX_VISUAL_TOKENS


def test_screenshot_is_a_scaled_compact_image():
    pc = make()
    [block] = pc.run("screenshot", {})
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"
    import base64, io
    shot = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert shot.size == (int(3840 * pc.scale), int(2160 * pc.scale))


def test_clicks_land_on_the_real_screen_position():
    pc = make()
    x, y = int(1000 * pc.scale), int(500 * pc.scale)
    pc.run("double_click", {"coordinate": [x, y]})
    action, button, count, pos = pc.mouse.log[-1]
    assert (action, button, count) == ("click", "left", 2)
    assert abs(pos[0] - 1000) <= 2 and abs(pos[1] - 500) <= 2


def test_click_with_modifier_holds_and_releases_it():
    pc = make()
    pc.run("left_click", {"coordinate": [10, 10], "text": "ctrl"})
    assert pc.keyboard.log == [("down", "ctrl"), ("up", "ctrl")]


def test_key_combos_and_typing():
    pc = make()
    pc.run("key", {"text": "ctrl+s"})
    pc.run("type", {"text": "hello"})
    assert pc.keyboard.log == [("down", "ctrl"), ("down", "s"), ("up", "s"), ("up", "ctrl"), ("type", "hello")]
    assert parse_combo("Return") == ["enter"] and parse_combo("super+e") == ["cmd", "e"]


def test_bad_actions_are_errors_not_crashes():
    pc = make()
    with pytest.raises(ComputerError):
        pc.run("left_click", {"coordinate": [99999, 5]})
    with pytest.raises(ComputerError):
        pc.run("key", {"text": "NotAKey"})
    with pytest.raises(ComputerError):
        pc.run("format_disk", {})


def test_zoom_returns_an_image_of_the_region():
    pc = make()
    [block] = pc.run("zoom", {"region": [0, 0, 200, 100]})
    assert block["type"] == "image"


# --- Claude and the agent loop ------------------------------------------------------------

class Block:
    def __init__(self, **kw): self.__dict__.update(kw)


class FakeStream:
    def __init__(self, message): self.message = message
    def __enter__(self): return self
    def __exit__(self, *e): return False
    text_stream = property(lambda self: iter([]))
    def get_final_message(self): return self.message


class FakeClaude:
    def __init__(self, replies): self.replies, self.sent = list(replies), []; self.beta = self.messages = self
    def stream(self, **kw):
        self.sent.append(kw)
        return FakeStream(self.replies.pop(0))


def test_claude_gets_the_toolset_and_results_echo_it(tmp_path):
    fake = FakeClaude([
        Block(stop_reason="tool_use", content=[
            Block(type="tool_use", id="t1", name="screenshot", input={}, toolset_name="computer"),
            Block(type="tool_use", id="t2", name="left_click", input={"coordinate": [5, 5]}, toolset_name="computer"),
        ]),
        Block(stop_reason="end_turn", content=[]),
    ])
    brain = ClaudeBrain("k", "claude-opus-5-5", "sys", client=fake, computer=True)
    run_turn(brain, Toolbox(tmp_path, approve=lambda *_: True), "open notepad", QuietUI(), computer=make())
    assert {"type": "computer_toolset_20260801"} in fake.sent[0]["tools"]
    results = brain.messages[2]["content"]
    assert [r["toolset_name"] for r in results] == ["computer", "computer"]
    assert results[0]["content"][0]["type"] == "image" and not results[0]["is_error"]


def test_after_a_failed_action_the_rest_of_the_batch_is_skipped(tmp_path):
    brain = Recorder([ToolCall("1", "left_click", {"coordinate": [99999, 1]}, toolset="computer"),
                      ToolCall("2", "type", {"text": "secret"}, toolset="computer")])
    pc = make()
    run_turn(brain, Toolbox(tmp_path, approve=lambda *_: True), "go", QuietUI(), computer=pc)
    (_, first, f1), (_, second, f2) = brain.results
    assert f1 and f2 and second == FAILED_EARLIER
    assert ("type", "secret") not in pc.keyboard.log


def test_screen_actions_need_permission(tmp_path):
    brain = Recorder([ToolCall("1", "type", {"text": "hi"}, toolset="computer")])
    pc = make()
    run_turn(brain, Toolbox(tmp_path, approve=lambda *_: False), "go", QuietUI(), computer=pc)
    assert brain.results[0][2] and pc.keyboard.log == []


def test_no_screen_control_unless_turned_on(tmp_path):
    brain = Recorder([ToolCall("1", "screenshot", {}, toolset="computer")])
    run_turn(brain, Toolbox(tmp_path, approve=lambda *_: True), "go", QuietUI(), computer=None)
    assert brain.results[0][2] and "off" in brain.results[0][1]


class Recorder:
    def __init__(self, calls): self.calls, self.results = calls, None
    def add_user(self, text): pass
    def step(self, on_text):
        calls, self.calls = self.calls, []
        return calls
    def add_results(self, results): self.results = [(c.name, out, failed) for c, out, failed in results]


class QuietUI:
    def text_chunk(self, t): pass
    def end_text(self): pass
    def tool_start(self, call): pass
    def tool_done(self, call, output, failed): pass
    def notice(self, m): pass
