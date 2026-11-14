"""Flashing ESP32/Arduino boards from Volt Code (arduino-cli is faked)."""

import json
import subprocess
from pathlib import Path

import pytest

from volt_code import boards
from volt_code.tools import TOOLS, Toolbox, ToolError

LISTED = {"detected_ports": [
    {"port": {"address": "/dev/ttyS0", "protocol": "serial"}},
    {"port": {"address": "/dev/ttyUSB0", "protocol": "serial", "label": "CP2102"}},
]}


def fake_run(listing=LISTED, upload_code=0, upload_out="Hard resetting via RTS pin...", calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if "board" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(listing), "")
        return subprocess.CompletedProcess(cmd, upload_code, upload_out, "")
    return run


@pytest.fixture(autouse=True)
def cli(monkeypatch):
    monkeypatch.setattr(boards, "find_cli", lambda: "/usr/bin/arduino-cli")


@pytest.mark.parametrize("text", ["flash the blink code", "upload blink to my esp32", "Flash blink",
                                  "put the blinking led code on the board"])
def test_blink_requests_are_recognised(text):
    assert boards.wants_blink(text)


@pytest.mark.parametrize("text", ["why do my eyes blink so much", "!arduino-cli board list", "flash the firmware",
                                  "/rc"])
def test_other_messages_go_to_the_brain(text):
    assert not boards.wants_blink(text)


def test_the_usb_board_is_found_and_a_plain_esp32_is_assumed():
    found = boards.find_boards(run=fake_run())
    assert [b["port"] for b in found] == ["/dev/ttyUSB0"]
    assert boards.pick_board(found) == {"port": "/dev/ttyUSB0", "fqbn": "esp32:esp32:esp32"}


def test_no_board_says_what_to_do():
    with pytest.raises(boards.BoardError, match="USB data cable"):
        boards.pick_board(boards.find_boards(run=fake_run({"detected_ports": []})))


def test_missing_arduino_cli_is_explained(monkeypatch):
    monkeypatch.setattr(boards, "find_cli", lambda: None)
    with pytest.raises(boards.BoardError, match="arduino-cli"):
        boards.find_boards(run=fake_run())


def test_flash_builds_and_uploads_in_one_go(tmp_path):
    calls = []
    worked, _ = boards.flash(tmp_path, "/dev/ttyUSB0", "esp32:esp32:esp32", run=fake_run(calls=calls))
    assert worked
    assert calls[-1][1:] == ["compile", "--fqbn", "esp32:esp32:esp32", "--upload", "-p", "/dev/ttyUSB0", str(tmp_path)]


def test_a_connect_failure_mentions_the_boot_button(tmp_path):
    worked, output = boards.flash(tmp_path, "/dev/ttyUSB0", "esp32:esp32:esp32",
                                  run=fake_run(upload_code=2, upload_out="A fatal error occurred: Failed to connect to ESP32"))
    assert not worked and "BOOT" in output


def test_the_blink_sketch_is_written_where_arduino_cli_expects_it(tmp_path, monkeypatch):
    monkeypatch.setenv("VOLT_DATA_DIR", str(tmp_path))
    folder = boards.blink_folder()
    assert (folder / f"{folder.name}.ino").read_text() == boards.BLINK_SKETCH
    assert "LED_BUILTIN 2" in boards.BLINK_SKETCH


def test_flash_board_tool_asks_first_and_flashes(tmp_path, monkeypatch):
    assert any(t["name"] == "flash_board" for t in TOOLS)
    (tmp_path / "led").mkdir()
    (tmp_path / "led" / "led.ino").write_text("void setup(){} void loop(){}")
    monkeypatch.setattr(boards, "find_boards", lambda: [{"port": "/dev/ttyUSB0", "fqbn": "", "name": ""}])
    flashed = []
    monkeypatch.setattr(boards, "flash", lambda s, p, f: (flashed.append((Path(s).name, p, f)), (True, "ok"))[1])
    asked = []
    box = Toolbox(tmp_path, lambda title, detail: (asked.append(title), True)[1])
    assert "Flashed successfully" in box.run("flash_board", {"sketch": "led"})
    assert asked == ["Flash the board"] and flashed == [("led", "/dev/ttyUSB0", "esp32:esp32:esp32")]
    with pytest.raises(ToolError):
        Toolbox(tmp_path, lambda t, d: False).run("flash_board", {"sketch": "led"})


def test_flashing_always_asks_even_when_edits_are_accepted():
    from volt_code import features
    assert features.mode_decides("accept_edits", "Flash the board") is None
    assert features.mode_decides("plan", "Flash the board") is False
